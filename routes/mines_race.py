# ============================================================
# routes/mines_race.py
# CS2CaseBot | PvP Mines Race
#
# Pure winner-take-all race, sibling to routes/ladder_race.py but
# using Mines instead of the ladder. Unlike Battle Royale Minefield
# (routes/battle_royale_mines.py, player-chosen mine count, solo-style
# payout), the board is FIXED for everyone here (25 tiles, 3 mines)
# so difficulty is identical across racers -- no separate solo
# economy, the entry fee IS the stake. First player to safely reveal
# 5 tiles takes the entire shared pot, no rake. Busting is a pure
# loss (no refund). If nobody reaches the target within a safety
# timeout, the round is cancelled and every entry fee refunded.
# ============================================================

import asyncio
import time
from typing import Dict, Set, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from pydantic import BaseModel

import shared
from shared import (
    logger, get_db, require_auth, ensure_user_exists,
    deduct_balance, broadcast_to_set, convert_decimals,
    credit_win, require_game_enabled, clamp_bet as shared_clamp_bet,
    secure_shuffle, log_game,
)

router = APIRouter(prefix="/api/games/mines-race", tags=["mines-race"])

MIN_BET       = 50
MAX_BET       = 750_000
MAX_PLAYERS   = 8
SAFETY_SECS   = 60
GRID_SIZE     = 25
FIXED_MINES   = 3
TARGET_SAFE   = 5


def clamp_bet(amount: float) -> float:
    return shared_clamp_bet(amount, MIN_BET, MAX_BET)


# ============================================================
# TABLE SETUP
# ============================================================

async def init_mines_race_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mines_race_rounds (
                id            SERIAL PRIMARY KEY,
                status        TEXT DEFAULT 'racing'
                              CHECK (status IN ('racing','settled','cancelled')),
                pot           DECIMAL(15,2) DEFAULT 0,
                winner_id     BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                created_at    TIMESTAMP DEFAULT NOW(),
                resolved_at   TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mines_race_players (
                id            SERIAL PRIMARY KEY,
                round_id      INTEGER NOT NULL REFERENCES mines_race_rounds(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
                amount        DECIMAL(15,2) NOT NULL,
                positions     INTEGER[] NOT NULL,
                safe_count    INTEGER DEFAULT 0,
                busted        BOOLEAN DEFAULT FALSE,
                refunded      BOOLEAN DEFAULT FALSE,
                created_at    TIMESTAMP DEFAULT NOW(),
                UNIQUE(round_id, user_id)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_mines_race_players_round ON mines_race_players(round_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_mines_race_players_user ON mines_race_players(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_mines_race_rounds_status ON mines_race_rounds(status)")
    logger.info("✅ PvP Mines Race tables ready")


async def recover_stale_mines_race_rounds():
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id FROM mines_race_rounds WHERE status='racing' FOR UPDATE"
            )
            for row in stale:
                round_id = row['id']
                players = await conn.fetch(
                    "SELECT user_id, amount FROM mines_race_players WHERE round_id=$1 AND refunded=FALSE",
                    round_id
                )
                for p in players:
                    await shared.add_balance(p['user_id'], float(p['amount']), conn)
                await conn.execute(
                    "UPDATE mines_race_players SET refunded=TRUE WHERE round_id=$1", round_id
                )
                await conn.execute(
                    "UPDATE mines_race_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                    round_id
                )
            if stale:
                logger.info(f"⛏️ Recovered {len(stale)} stale Mines Race round(s), refunded all entry fees")


# ============================================================
# ROOM
# ============================================================

class MinesRaceRoom:
    def __init__(self, round_id: int):
        self.round_id = round_id
        self.status = 'racing'
        self.pot = 0.0
        # user_id -> {username, amount, positions, safe_count, busted}
        self.players: Dict[int, Dict] = {}
        self.ws_set: Set[WebSocket] = set()
        self.ws_map: Dict[int, WebSocket] = {}
        self.task: Optional[asyncio.Task] = None
        self.deadline: Optional[float] = None
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
            'deadline': self.deadline,
            'target': TARGET_SAFE,
            'players': [
                {'user_id': uid, **{k: v for k, v in p.items() if k != 'positions'}}
                for uid, p in self.players.items()
            ],
        }

    async def run_safety_timer(self):
        await asyncio.sleep(SAFETY_SECS)
        await self.cancel_if_unresolved()

    async def cancel_if_unresolved(self):
        async with self.lock:
            if self.status != 'racing':
                return
            self.status = 'cancelled'
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for uid, p in self.players.items():
                        if not p['busted']:
                            await shared.add_balance(uid, p['amount'], conn)
                            await conn.execute(
                                "UPDATE mines_race_players SET refunded=TRUE WHERE round_id=$1 AND user_id=$2",
                                self.round_id, uid
                            )
                    await conn.execute(
                        "UPDATE mines_race_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )
            await self.broadcast({'type': 'cancelled', 'round_id': self.round_id, 'reason': 'no_winner_in_time'})
            async with _mines_race_registry_lock:
                _mines_race_rooms.pop(self.round_id, None)

    async def declare_winner(self, winner_id: int):
        async with self.lock:
            if self.status != 'racing':
                return
            self.status = 'settled'
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    win = await credit_win(winner_id, self.pot, conn)
                    await conn.execute(
                        "UPDATE mines_race_rounds SET status='settled', winner_id=$1, resolved_at=NOW() WHERE id=$2",
                        winner_id, self.round_id
                    )
                    for uid, p in self.players.items():
                        await log_game(conn, uid, 'mines_race', p['amount'],
                                       win if uid == winner_id else 0.0,
                                       {'round_id': self.round_id})
            await self.broadcast({
                'type': 'race_won', 'round_id': self.round_id, 'winner_id': winner_id,
                'win': win, **self.snapshot(),
            })
            async with _mines_race_registry_lock:
                _mines_race_rooms.pop(self.round_id, None)


# ============================================================
# REGISTRY
# ============================================================

_mines_race_rooms: Dict[int, MinesRaceRoom] = {}
_mines_race_registry_lock = asyncio.Lock()


async def _get_or_create_open_room() -> MinesRaceRoom:
    async with _mines_race_registry_lock:
        for room in _mines_race_rooms.values():
            if room.status == 'racing' and len(room.players) < MAX_PLAYERS:
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO mines_race_rounds (status) VALUES ('racing') RETURNING id"
            )
        room = MinesRaceRoom(row['id'])
        room.deadline = time.time() + SAFETY_SECS
        _mines_race_rooms[room.round_id] = room
        return room


async def create_private_room(participant_user_ids: list, amount: float) -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py). Scoped to exactly 2 players, same as the duel
    games -- stakes both players atomically in one transaction and
    starts the race directly, skipping the open-lobby /join flow
    (_get_or_create_open_room) entirely. Each player still gets their
    own independently shuffled mine positions, matching the public
    /join flow's per-player randomization exactly."""
    if len(participant_user_ids) != 2:
        raise HTTPException(400, "Mines Race friend challenges need exactly 2 players")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO mines_race_rounds (status) VALUES ('racing') RETURNING id"
            )
            round_id = row['id']

            players: Dict[int, Dict] = {}
            for uid in participant_user_ids:
                if not await deduct_balance(uid, amount, conn):
                    raise HTTPException(400, f"Player {uid} has insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                username = user_row['username'] if user_row else f'Player {uid}'
                positions = secure_shuffle(list(range(GRID_SIZE)))[:FIXED_MINES]
                await conn.execute("""
                    INSERT INTO mines_race_players (round_id, user_id, amount, positions)
                    VALUES ($1,$2,$3,$4)
                """, round_id, uid, amount, positions)
                players[uid] = {
                    'username': username, 'amount': amount, 'positions': positions,
                    'safe_count': 0, 'busted': False,
                }
            await conn.execute(
                "UPDATE mines_race_rounds SET pot = $1 WHERE id=$2", amount * len(participant_user_ids), round_id
            )

    room = MinesRaceRoom(round_id)
    room.players = players
    room.pot = round(amount * len(participant_user_ids), 2)
    room.deadline = time.time() + SAFETY_SECS
    async with _mines_race_registry_lock:
        _mines_race_rooms[round_id] = room
    room.task = asyncio.create_task(room.run_safety_timer())
    await room.broadcast({'type': 'race_start', 'round_id': round_id, 'round': room.snapshot()})
    return round_id


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
    await require_game_enabled("mines-race")
    amount = clamp_bet(req.amount)
    await ensure_user_exists(user_id)

    if req.round_id is not None:
        async with _mines_race_registry_lock:
            room = _mines_race_rooms.get(req.round_id)
        if not room:
            raise HTTPException(404, "Round not found or already closed")
    else:
        room = await _get_or_create_open_room()

    async with room.lock:
        if room.status != 'racing':
            raise HTTPException(400, "This round is no longer accepting players")
        if len(room.players) >= MAX_PLAYERS:
            raise HTTPException(400, "This round is full")
        if user_id in room.players:
            raise HTTPException(400, "You're already racing in this round")

        positions = secure_shuffle(list(range(GRID_SIZE)))[:FIXED_MINES]
        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await deduct_balance(user_id, amount, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'
                await conn.execute("""
                    INSERT INTO mines_race_players (round_id, user_id, amount, positions)
                    VALUES ($1,$2,$3,$4)
                """, room.round_id, user_id, amount, positions)
                await conn.execute(
                    "UPDATE mines_race_rounds SET pot = pot + $1 WHERE id=$2", amount, room.round_id
                )

        room.pot = round(room.pot + amount, 2)
        room.players[user_id] = {
            'username': username, 'amount': amount, 'positions': positions, 'safe_count': 0, 'busted': False,
        }

        if room.task is None:
            room.task = asyncio.create_task(room.run_safety_timer())

        await room.broadcast({'type': 'player_joined', 'round_id': room.round_id, 'round': room.snapshot()})
        result = {"success": True, "round_id": room.round_id, "round": room.snapshot()}

    return convert_decimals(result)


@router.post("/reveal")
async def reveal(body: RoundIdBody, request: Request):
    user_id = await require_auth(request)
    async with _mines_race_registry_lock:
        room = _mines_race_rooms.get(body.round_id)
    if not room:
        raise HTTPException(404, "Round not found or already closed")

    winner_id = None
    async with room.lock:
        if room.status != 'racing':
            raise HTTPException(400, "This round has already ended")
        p = room.players.get(user_id)
        if not p:
            raise HTTPException(400, "You're not racing in this round")
        if p['busted']:
            raise HTTPException(400, "You've already busted out of this race")
        if p['safe_count'] >= TARGET_SAFE:
            raise HTTPException(400, "You've already reached the target")

        tile = p['safe_count']   # deterministic reveal order over the pre-shuffled positions list
        hit_mine = tile in p['positions']

        if hit_mine:
            p['busted'] = True
            async with (await get_db()).acquire() as conn:
                await conn.execute(
                    "UPDATE mines_race_players SET busted=TRUE WHERE round_id=$1 AND user_id=$2",
                    room.round_id, user_id
                )
        else:
            p['safe_count'] += 1
            async with (await get_db()).acquire() as conn:
                await conn.execute(
                    "UPDATE mines_race_players SET safe_count=$1 WHERE round_id=$2 AND user_id=$3",
                    p['safe_count'], room.round_id, user_id
                )
            if p['safe_count'] >= TARGET_SAFE:
                winner_id = user_id

        await room.broadcast({'type': 'player_revealed', 'round_id': room.round_id, 'user_id': user_id,
                               'hit_mine': hit_mine, 'safe_count': p['safe_count'], 'busted': p['busted']})
        result = {"success": True, "hit_mine": hit_mine, "safe_count": p['safe_count'],
                   "busted": p['busted'], "won": winner_id is not None}

    if winner_id is not None:
        await room.declare_winner(winner_id)

    return convert_decimals(result)


@router.get("/rounds")
async def list_rounds():
    async with _mines_race_registry_lock:
        rooms = list(_mines_race_rooms.values())
    result = {"rounds": [
        {
            'round_id': r.round_id, 'status': r.status,
            'player_count': len(r.players), 'pot': r.pot, 'deadline': r.deadline,
        }
        for r in rooms if r.status == 'racing'
    ]}
    return convert_decimals(result)


@router.get("/rounds/{round_id}")
async def get_round(round_id: int):
    async with _mines_race_registry_lock:
        room = _mines_race_rooms.get(round_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        rnd = await conn.fetchrow("SELECT * FROM mines_race_rounds WHERE id=$1", round_id)
        if not rnd:
            raise HTTPException(404, "Round not found")
        players = await conn.fetch("""
            SELECT p.*, u.username FROM mines_race_players p
            JOIN users u ON u.user_id = p.user_id
            WHERE p.round_id=$1 ORDER BY p.created_at
        """, round_id)
    result = {
        'round_id': round_id, 'status': rnd['status'], 'pot': rnd['pot'], 'winner_id': rnd['winner_id'],
        'players': [
            {
                'user_id': p['user_id'], 'username': p['username'], 'amount': p['amount'],
                'safe_count': p['safe_count'], 'busted': p['busted'],
            }
            for p in players
        ],
    }
    return convert_decimals(result)


# ============================================================
# WEBSOCKET
# ============================================================

@router.websocket("/ws/{round_id}")
async def mines_race_ws(websocket: WebSocket, round_id: int):
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

    async with _mines_race_registry_lock:
        room = _mines_race_rooms.get(round_id)
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
