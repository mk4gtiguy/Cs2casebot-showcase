# ============================================================
# routes/skin_bingo.py
# CS2CaseBot | Skin Bingo
#
# Standard 75-ball bingo (B:1-15 / I:16-30 / N:31-45 w/ free center /
# G:46-60 / O:61-75), not skin-named cells -- the skin data doesn't
# support a fixed callable pool the way plain numbers do. The room
# draws one shared ball every ~3s; any player can claim a win the
# instant their own card has a complete line (row/column/diagonal)
# against the numbers drawn so far. Entry fee funds a winner-take-all
# pot -- first valid claim wins it all, no rake, matching Session 6's
# race-game convention (Ladder Race/Mines Race). Winner-detection
# happens inside room.lock (same shape as mines_race.py's /reveal) so
# two simultaneous claims can't both win.
# ============================================================

import asyncio
import time
from typing import Dict, Set, Optional, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from pydantic import BaseModel

import shared
from shared import (
    logger, get_db, require_auth, ensure_user_exists,
    deduct_balance, broadcast_to_set, convert_decimals,
    credit_win, require_game_enabled, clamp_bet as shared_clamp_bet,
    secure_shuffle, log_game,
)

router = APIRouter(prefix="/api/games/skin-bingo", tags=["skin-bingo"])

MIN_ENTRY    = 50
MAX_ENTRY    = 750_000
MAX_PLAYERS  = 8
DRAW_SECS    = 3   # seconds between each ball draw
CARD_SIZE    = 5
FREE_ROW, FREE_COL = 2, 2   # center cell

# Column letter -> inclusive number range
COLUMN_RANGES = [(1, 15), (16, 30), (31, 45), (46, 60), (61, 75)]


def clamp_entry(amount: float) -> float:
    return shared_clamp_bet(amount, MIN_ENTRY, MAX_ENTRY)


def _generate_card() -> List[List[Optional[int]]]:
    """Standard 75-ball bingo card: each column draws 5 unique numbers from
    its own 15-number range; center cell is FREE (None)."""
    card = [[None] * CARD_SIZE for _ in range(CARD_SIZE)]
    for col, (lo, hi) in enumerate(COLUMN_RANGES):
        picks = secure_shuffle(list(range(lo, hi + 1)))[:CARD_SIZE]
        for row in range(CARD_SIZE):
            card[row][col] = picks[row]
    card[FREE_ROW][FREE_COL] = None   # FREE space
    return card


def _card_has_line(card: List[List[Optional[int]]], drawn: Set[int]) -> bool:
    """True if any row, column, or diagonal is fully covered by drawn numbers
    (a None cell is the free space and always counts as covered)."""
    def covered(n):
        return n is None or n in drawn

    for row in range(CARD_SIZE):
        if all(covered(card[row][c]) for c in range(CARD_SIZE)):
            return True
    for col in range(CARD_SIZE):
        if all(covered(card[r][col]) for r in range(CARD_SIZE)):
            return True
    if all(covered(card[i][i]) for i in range(CARD_SIZE)):
        return True
    if all(covered(card[i][CARD_SIZE - 1 - i]) for i in range(CARD_SIZE)):
        return True
    return False


# ============================================================
# TABLE SETUP
# ============================================================

async def init_skin_bingo_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS skin_bingo_rounds (
                id            SERIAL PRIMARY KEY,
                status        TEXT DEFAULT 'drawing'
                              CHECK (status IN ('drawing','settled','cancelled')),
                pot           DECIMAL(15,2) DEFAULT 0,
                winner_id     BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                drawn_numbers INTEGER[] DEFAULT '{}',
                created_at    TIMESTAMP DEFAULT NOW(),
                resolved_at   TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS skin_bingo_players (
                id            SERIAL PRIMARY KEY,
                round_id      INTEGER NOT NULL REFERENCES skin_bingo_rounds(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
                entry_fee     DECIMAL(15,2) NOT NULL,
                card          JSONB NOT NULL,
                created_at    TIMESTAMP DEFAULT NOW(),
                UNIQUE(round_id, user_id)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_skin_bingo_players_round ON skin_bingo_players(round_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_skin_bingo_players_user ON skin_bingo_players(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_skin_bingo_rounds_status ON skin_bingo_rounds(status)")
    logger.info("✅ Skin Bingo tables ready")


async def recover_stale_skin_bingo_rounds():
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id FROM skin_bingo_rounds WHERE status='drawing' FOR UPDATE"
            )
            for row in stale:
                round_id = row['id']
                players = await conn.fetch(
                    "SELECT user_id, entry_fee FROM skin_bingo_players WHERE round_id=$1",
                    round_id
                )
                for p in players:
                    await shared.add_balance(p['user_id'], float(p['entry_fee']), conn)
                await conn.execute(
                    "UPDATE skin_bingo_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                    round_id
                )
            if stale:
                logger.info(f"🎫 Recovered {len(stale)} stale Skin Bingo round(s), refunded all entry fees")


# ============================================================
# ROOM
# ============================================================

class BingoRoom:
    def __init__(self, round_id: int):
        self.round_id = round_id
        self.status = 'drawing'
        self.pot = 0.0
        # user_id -> {username, entry_fee, card}
        self.players: Dict[int, Dict] = {}
        self.drawn_numbers: List[int] = []
        self.ws_set: Set[WebSocket] = set()
        self.ws_map: Dict[int, WebSocket] = {}
        self.task: Optional[asyncio.Task] = None
        self.lock = asyncio.Lock()
        self.created_at = time.time()

    async def broadcast(self, msg: dict):
        dead = await broadcast_to_set(self.ws_set, convert_decimals(msg))
        self.ws_set -= dead

    def snapshot(self) -> dict:
        return {
            'round_id': self.round_id,
            'status': self.status,
            'pot': self.pot,
            'drawn_numbers': self.drawn_numbers,
            'players': [
                {'user_id': uid, 'username': p['username'], 'entry_fee': p['entry_fee']}
                for uid, p in self.players.items()
            ],
        }

    async def run_drawing(self):
        undrawn = list(range(1, 76))
        undrawn = secure_shuffle(undrawn)
        idx = 0
        while idx < len(undrawn):
            await asyncio.sleep(DRAW_SECS)
            async with self.lock:
                if self.status != 'drawing':
                    return
                num = undrawn[idx]
                idx += 1
                self.drawn_numbers.append(num)
                pool = await get_db()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE skin_bingo_rounds SET drawn_numbers = drawn_numbers || $1 WHERE id=$2",
                        [num], self.round_id
                    )
            await self.broadcast({'type': 'ball_drawn', 'round_id': self.round_id, 'number': num,
                                   'drawn_numbers': self.drawn_numbers})
        # All 75 balls drawn with no winner -- statistically near-impossible
        # with a line-only win condition, but handled per the plan's safety cap.
        await self.cancel_if_unresolved()

    async def cancel_if_unresolved(self):
        async with self.lock:
            if self.status != 'drawing':
                return
            self.status = 'cancelled'
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for uid, p in self.players.items():
                        await shared.add_balance(uid, p['entry_fee'], conn)
                    await conn.execute(
                        "UPDATE skin_bingo_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )
            await self.broadcast({'type': 'cancelled', 'round_id': self.round_id, 'reason': 'no_winner_before_all_drawn'})
            async with _skin_bingo_registry_lock:
                _skin_bingo_rooms.pop(self.round_id, None)

    async def declare_winner(self, winner_id: int):
        async with self.lock:
            if self.status != 'drawing':
                return
            self.status = 'settled'
            p = self.players[winner_id]
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    win = await credit_win(winner_id, self.pot, conn)
                    await conn.execute(
                        "UPDATE skin_bingo_rounds SET status='settled', winner_id=$1, resolved_at=NOW() WHERE id=$2",
                        winner_id, self.round_id
                    )
                    await log_game(conn, winner_id, 'skin_bingo', p['entry_fee'], win, {
                        'round': self.round_id, 'pot': self.pot,
                    })
            await self.broadcast({
                'type': 'bingo', 'round_id': self.round_id, 'winner_id': winner_id,
                'win': win, **self.snapshot(),
            })
            async with _skin_bingo_registry_lock:
                _skin_bingo_rooms.pop(self.round_id, None)


# ============================================================
# REGISTRY
# ============================================================

_skin_bingo_rooms: Dict[int, BingoRoom] = {}
_skin_bingo_registry_lock = asyncio.Lock()


async def _get_or_create_open_room() -> BingoRoom:
    async with _skin_bingo_registry_lock:
        for room in _skin_bingo_rooms.values():
            if room.status == 'drawing' and not room.drawn_numbers and len(room.players) < MAX_PLAYERS:
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO skin_bingo_rounds (status) VALUES ('drawing') RETURNING id"
            )
        room = BingoRoom(row['id'])
        _skin_bingo_rooms[room.round_id] = room
        return room


# ============================================================
# REST ROUTES
# ============================================================

class JoinRequest(BaseModel):
    amount: float
    round_id: Optional[int] = None


class RoundIdBody(BaseModel):
    round_id: int


@router.post("/join")
async def join_round(req: JoinRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("skin-bingo")
    entry_fee = clamp_entry(req.amount)
    await ensure_user_exists(user_id)

    if req.round_id is not None:
        async with _skin_bingo_registry_lock:
            room = _skin_bingo_rooms.get(req.round_id)
        if not room:
            raise HTTPException(404, "Round not found or already closed")
    else:
        room = await _get_or_create_open_room()

    async with room.lock:
        if room.status != 'drawing':
            raise HTTPException(400, "This round is no longer accepting players")
        if room.drawn_numbers:
            raise HTTPException(400, "This round has already started drawing")
        if len(room.players) >= MAX_PLAYERS:
            raise HTTPException(400, "This round is full")
        if user_id in room.players:
            raise HTTPException(400, "You're already in this round")

        card = _generate_card()
        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await deduct_balance(user_id, entry_fee, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'
                await conn.execute(
                    "INSERT INTO skin_bingo_players (round_id, user_id, entry_fee, card) VALUES ($1,$2,$3,$4)",
                    room.round_id, user_id, entry_fee, card
                )
                await conn.execute(
                    "UPDATE skin_bingo_rounds SET pot = pot + $1 WHERE id=$2", entry_fee, room.round_id
                )

        room.pot = round(room.pot + entry_fee, 2)
        room.players[user_id] = {'username': username, 'entry_fee': entry_fee, 'card': card}

        if room.task is None:
            room.task = asyncio.create_task(room.run_drawing())

        await room.broadcast({'type': 'player_joined', 'round_id': room.round_id, 'round': room.snapshot()})
        result = {"success": True, "round_id": room.round_id, "card": card, "round": room.snapshot()}

    return convert_decimals(result)


@router.post("/bingo")
async def claim_bingo(body: RoundIdBody, request: Request):
    user_id = await require_auth(request)
    async with _skin_bingo_registry_lock:
        room = _skin_bingo_rooms.get(body.round_id)
    if not room:
        raise HTTPException(404, "Round not found or already closed")

    winner_id = None
    async with room.lock:
        if room.status != 'drawing':
            raise HTTPException(400, "This round has already ended")
        p = room.players.get(user_id)
        if not p:
            raise HTTPException(400, "You're not in this round")

        drawn_set = set(room.drawn_numbers)
        if not _card_has_line(p['card'], drawn_set):
            raise HTTPException(400, "No completed line yet -- not a valid Bingo claim")

        winner_id = user_id

    if winner_id is not None:
        await room.declare_winner(winner_id)

    return {"success": True, "won": True}


@router.get("/rounds")
async def list_rounds():
    async with _skin_bingo_registry_lock:
        rooms = list(_skin_bingo_rooms.values())
    result = {"rounds": [
        {
            'round_id': r.round_id, 'status': r.status,
            'player_count': len(r.players), 'pot': r.pot,
        }
        for r in rooms if r.status == 'drawing' and not r.drawn_numbers
    ]}
    return convert_decimals(result)


@router.get("/rounds/{round_id}")
async def get_round(round_id: int):
    async with _skin_bingo_registry_lock:
        room = _skin_bingo_rooms.get(round_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        rnd = await conn.fetchrow("SELECT * FROM skin_bingo_rounds WHERE id=$1", round_id)
        if not rnd:
            raise HTTPException(404, "Round not found")
        players = await conn.fetch("""
            SELECT p.user_id, p.entry_fee, u.username FROM skin_bingo_players p
            JOIN users u ON u.user_id = p.user_id
            WHERE p.round_id=$1 ORDER BY p.created_at
        """, round_id)
    result = {
        'round_id': round_id, 'status': rnd['status'], 'pot': rnd['pot'],
        'winner_id': rnd['winner_id'], 'drawn_numbers': rnd['drawn_numbers'],
        'players': [{'user_id': p['user_id'], 'username': p['username'], 'entry_fee': p['entry_fee']} for p in players],
    }
    return convert_decimals(result)


@router.get("/my-card/{round_id}")
async def get_my_card(round_id: int, request: Request):
    user_id = await require_auth(request)
    async with _skin_bingo_registry_lock:
        room = _skin_bingo_rooms.get(round_id)
    if room:
        p = room.players.get(user_id)
        if not p:
            raise HTTPException(404, "You're not in this round")
        return convert_decimals({"card": p['card']})

    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT card FROM skin_bingo_players WHERE round_id=$1 AND user_id=$2", round_id, user_id
        )
    if not row:
        raise HTTPException(404, "You're not in this round")
    return convert_decimals({"card": row['card']})


# ============================================================
# WEBSOCKET
# ============================================================

@router.websocket("/ws/{round_id}")
async def skin_bingo_ws(websocket: WebSocket, round_id: int):
    await websocket.accept()

    token = websocket.cookies.get("session_token")
    session = shared.get_session(token) if token else None
    if not session:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    user_id = session.get("user_id")
    if not user_id:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    async with _skin_bingo_registry_lock:
        room = _skin_bingo_rooms.get(round_id)
    if not room:
        try:
            await websocket.send_json({'type': 'no_room', 'round_id': round_id})
        except Exception:
            pass
        await websocket.close()
        return

    is_player = user_id in room.players
    if is_player:
        room.ws_map[user_id] = websocket
    room.ws_set.add(websocket)

    try:
        await websocket.send_json(convert_decimals({'type': 'room_state', **room.snapshot()}))
    except Exception:
        pass

    try:
        while True:
            data = await websocket.receive_json()
            if data.get('type') == 'ping':
                await websocket.send_json({'type': 'pong'})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        room.ws_set.discard(websocket)
        if is_player:
            room.ws_map.pop(user_id, None)
