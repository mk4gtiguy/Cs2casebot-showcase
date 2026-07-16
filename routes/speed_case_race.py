# ============================================================
# routes/speed_case_race.py
# CS2CaseBot | Speed Case Race
#
# Case-opening is confirmed instant server-side (routes/case_draft_duel.py,
# routes/case_battles.py both call shared.get_random_item() with zero
# delay), so there's no natural "speed" to race unless one is invented.
# This is a TIMED VALUE RACE: everyone pays a separate entry fee into a
# bonus pot, then can open any case from the catalog as many times as
# they want during a fixed countdown -- each open is its own normal
# transaction (case price deducted, item lands in the opener's own
# inventory regardless of race outcome, same convention as Case
# Battles/Case Draft Duel). Whoever accumulates the highest total
# opened-value when the timer ends wins the bonus pot.
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
    get_random_item, CASES, log_game,
)

router = APIRouter(prefix="/api/games/speed-case-race", tags=["speed-case-race"])

MIN_ENTRY    = 50
MAX_ENTRY    = 750_000
MAX_PLAYERS  = 8
RACE_SECS    = 30


def clamp_entry(amount: float) -> float:
    return shared_clamp_bet(amount, MIN_ENTRY, MAX_ENTRY)


# ============================================================
# TABLE SETUP
# ============================================================

async def init_speed_case_race_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS speed_case_race_rounds (
                id            SERIAL PRIMARY KEY,
                status        TEXT DEFAULT 'racing'
                              CHECK (status IN ('racing','settled','cancelled')),
                bonus_pot     DECIMAL(15,2) DEFAULT 0,
                created_at    TIMESTAMP DEFAULT NOW(),
                resolved_at   TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS speed_case_race_players (
                id            SERIAL PRIMARY KEY,
                round_id      INTEGER NOT NULL REFERENCES speed_case_race_rounds(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
                entry_fee     DECIMAL(15,2) NOT NULL,
                total_value   DECIMAL(15,2) DEFAULT 0,
                cases_opened  INTEGER DEFAULT 0,
                bonus_won     DECIMAL(15,2) DEFAULT 0,
                created_at    TIMESTAMP DEFAULT NOW(),
                UNIQUE(round_id, user_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS speed_case_race_opens (
                id            SERIAL PRIMARY KEY,
                round_id      INTEGER NOT NULL REFERENCES speed_case_race_rounds(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
                case_id       TEXT NOT NULL,
                item_name     TEXT,
                value         DECIMAL(15,2),
                created_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_speed_case_race_players_round ON speed_case_race_players(round_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_speed_case_race_players_user ON speed_case_race_players(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_speed_case_race_opens_round ON speed_case_race_opens(round_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_speed_case_race_opens_user ON speed_case_race_opens(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_speed_case_race_rounds_status ON speed_case_race_rounds(status)")
    logger.info("✅ Speed Case Race tables ready")


async def recover_stale_speed_case_race_rounds():
    """Only the bonus-pot entry fee is at risk on a stuck round -- individual
    case purchases are already-settled transactions (item + price already
    exchanged), nothing to refund there."""
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id FROM speed_case_race_rounds WHERE status='racing' FOR UPDATE"
            )
            for row in stale:
                round_id = row['id']
                players = await conn.fetch(
                    "SELECT user_id, entry_fee FROM speed_case_race_players WHERE round_id=$1",
                    round_id
                )
                for p in players:
                    await shared.add_balance(p['user_id'], float(p['entry_fee']), conn)
                await conn.execute(
                    "UPDATE speed_case_race_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                    round_id
                )
            if stale:
                logger.info(f"📦 Recovered {len(stale)} stale Speed Case Race round(s), refunded all entry fees")


# ============================================================
# ROOM
# ============================================================

class CaseRaceRoom:
    def __init__(self, round_id: int):
        self.round_id = round_id
        self.status = 'racing'
        self.bonus_pot = 0.0
        # user_id -> {username, entry_fee, total_value, cases_opened}
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
            'bonus_pot': self.bonus_pot,
            'deadline': self.deadline,
            'players': [{'user_id': uid, **p} for uid, p in self.players.items()],
        }

    async def run_racing(self):
        for sec in range(RACE_SECS, 0, -1):
            await asyncio.sleep(1)
            async with self.lock:
                if self.status != 'racing':
                    return
            await self.broadcast({'type': 'race_tick', 'round_id': self.round_id, 'seconds': sec - 1})
        await self.resolve()

    async def resolve(self):
        async with self.lock:
            if self.status != 'racing':
                return
            self.status = 'settled'
            await self.broadcast({'type': 'round_ending', 'round_id': self.round_id})

            if not self.players:
                pool = await get_db()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE speed_case_race_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )
                self.status = 'cancelled'
                async with _speed_case_race_registry_lock:
                    _speed_case_race_rooms.pop(self.round_id, None)
                return

            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    if self.bonus_pot > 0:
                        top_value = max(p['total_value'] for p in self.players.values())
                        winners = [uid for uid, p in self.players.items() if p['total_value'] == top_value]
                        share = round(self.bonus_pot / len(winners), 2)
                        for uid in winners:
                            share_final = await credit_win(uid, share, conn)
                            self.players[uid]['bonus_won'] = share_final
                            await conn.execute(
                                "UPDATE speed_case_race_players SET bonus_won=$1 WHERE round_id=$2 AND user_id=$3",
                                share_final, self.round_id, uid
                            )
                    await conn.execute(
                        "UPDATE speed_case_race_rounds SET status='settled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )
                    for uid, p in self.players.items():
                        await log_game(conn, uid, 'speed_case_race', p['entry_fee'],
                                       p.get('bonus_won', 0) or 0, {'round_id': self.round_id})

            await self.broadcast({'type': 'result', 'round_id': self.round_id, **self.snapshot()})
            async with _speed_case_race_registry_lock:
                _speed_case_race_rooms.pop(self.round_id, None)


# ============================================================
# REGISTRY
# ============================================================

_speed_case_race_rooms: Dict[int, CaseRaceRoom] = {}
_speed_case_race_registry_lock = asyncio.Lock()


async def _get_or_create_open_room() -> CaseRaceRoom:
    async with _speed_case_race_registry_lock:
        for room in _speed_case_race_rooms.values():
            if room.status == 'racing' and len(room.players) < MAX_PLAYERS:
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO speed_case_race_rounds (status) VALUES ('racing') RETURNING id"
            )
        room = CaseRaceRoom(row['id'])
        room.deadline = time.time() + RACE_SECS
        _speed_case_race_rooms[room.round_id] = room
        return room


async def create_private_room(participant_user_ids: list, amount: float) -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py). Scoped to exactly 2 players -- both pay the
    entry fee into the bonus pot atomically in one transaction and
    the race starts directly, skipping the open-lobby /join flow
    (_get_or_create_open_room). Case opens still happen through the
    normal /open endpoint once the race has started."""
    if len(participant_user_ids) != 2:
        raise HTTPException(400, "Speed Case Race friend challenges need exactly 2 players")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO speed_case_race_rounds (status) VALUES ('racing') RETURNING id"
            )
            round_id = row['id']

            players: Dict[int, Dict] = {}
            for uid in participant_user_ids:
                if not await deduct_balance(uid, amount, conn):
                    raise HTTPException(400, f"Player {uid} has insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                username = user_row['username'] if user_row else f'Player {uid}'
                await conn.execute(
                    "INSERT INTO speed_case_race_players (round_id, user_id, entry_fee) VALUES ($1,$2,$3)",
                    round_id, uid, amount
                )
                players[uid] = {
                    'username': username, 'entry_fee': amount, 'total_value': 0.0,
                    'cases_opened': 0, 'bonus_won': 0,
                }
            await conn.execute(
                "UPDATE speed_case_race_rounds SET bonus_pot = $1 WHERE id=$2",
                amount * len(participant_user_ids), round_id
            )

    room = CaseRaceRoom(round_id)
    room.players = players
    room.bonus_pot = round(amount * len(participant_user_ids), 2)
    room.deadline = time.time() + RACE_SECS
    async with _speed_case_race_registry_lock:
        _speed_case_race_rooms[round_id] = room
    room.task = asyncio.create_task(room.run_racing())
    await room.broadcast({'type': 'round_start', 'round_id': round_id, 'round': room.snapshot()})
    return round_id


# ============================================================
# REST ROUTES
# ============================================================

class JoinRequest(BaseModel):
    amount: float
    round_id: Optional[int] = None


class OpenRequest(BaseModel):
    round_id: int
    case_id: str


@router.post("/join")
async def join_round(req: JoinRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("speed-case-race")
    entry_fee = clamp_entry(req.amount)
    await ensure_user_exists(user_id)

    if req.round_id is not None:
        async with _speed_case_race_registry_lock:
            room = _speed_case_race_rooms.get(req.round_id)
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
            raise HTTPException(400, "You're already in this race")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await deduct_balance(user_id, entry_fee, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'
                await conn.execute(
                    "INSERT INTO speed_case_race_players (round_id, user_id, entry_fee) VALUES ($1,$2,$3)",
                    room.round_id, user_id, entry_fee
                )
                await conn.execute(
                    "UPDATE speed_case_race_rounds SET bonus_pot = bonus_pot + $1 WHERE id=$2",
                    entry_fee, room.round_id
                )

        room.bonus_pot = round(room.bonus_pot + entry_fee, 2)
        room.players[user_id] = {
            'username': username, 'entry_fee': entry_fee, 'total_value': 0.0, 'cases_opened': 0, 'bonus_won': 0,
        }

        if room.task is None:
            room.task = asyncio.create_task(room.run_racing())

        await room.broadcast({'type': 'player_joined', 'round_id': room.round_id, 'round': room.snapshot()})
        result = {"success": True, "round_id": room.round_id, "round": room.snapshot()}

    return convert_decimals(result)


@router.post("/open")
async def open_case(req: OpenRequest, request: Request):
    user_id = await require_auth(request)
    async with _speed_case_race_registry_lock:
        room = _speed_case_race_rooms.get(req.round_id)
    if not room:
        raise HTTPException(404, "Round not found or already closed")

    case = CASES.get(req.case_id)
    if not case:
        raise HTTPException(400, "Unknown case")
    case_price = float(case.get('price', 0))

    async with room.lock:
        if room.status != 'racing':
            raise HTTPException(400, "This round has already ended")
        p = room.players.get(user_id)
        if not p:
            raise HTTPException(400, "You're not in this race")

        item = get_random_item(req.case_id)
        if not item:
            raise HTTPException(400, "Could not open that case")

        skin_img_file = item.get('image_filename')
        skin_img_url = f"/static/images/skins/{skin_img_file}" if skin_img_file else None

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await deduct_balance(user_id, case_price, conn):
                    raise HTTPException(400, "Insufficient balance")
                # Same idiom as Case Battles/Case Draft Duel -- the opened
                # item goes straight into the opener's own inventory
                # regardless of who ends up winning the race's bonus pot.
                await conn.execute("""
                    INSERT INTO inventory
                        (user_id, item_name, item_type, rarity, price, condition, is_stattrak, status, float_value, image_url)
                    VALUES ($1,$2,'weapon',$3,$4,$5,$6,'kept',$7,$8)
                """, user_id, item['name'], item['rarity'], item['price'],
                    item.get('condition', 'Field-Tested'), item.get('is_stattrak', False),
                    item.get('float', 0.0), skin_img_url)
                await conn.execute("""
                    INSERT INTO speed_case_race_opens (round_id, user_id, case_id, item_name, value)
                    VALUES ($1,$2,$3,$4,$5)
                """, room.round_id, user_id, req.case_id, item['name'], item['price'])
                await conn.execute("""
                    UPDATE users SET total_opens = total_opens + 1,
                        total_golds = total_golds + $2
                    WHERE user_id = $1
                """, user_id, 1 if item['rarity'] == 'Gold' else 0)
                p['total_value'] = round(p['total_value'] + float(item['price']), 2)
                p['cases_opened'] += 1
                await conn.execute("""
                    UPDATE speed_case_race_players SET total_value=$1, cases_opened=$2
                    WHERE round_id=$3 AND user_id=$4
                """, p['total_value'], p['cases_opened'], room.round_id, user_id)

        await room.broadcast({'type': 'player_opened', 'round_id': room.round_id, 'user_id': user_id,
                               'item_name': item['name'], 'value': item['price'],
                               'total_value': p['total_value'], 'cases_opened': p['cases_opened']})
        result = {"success": True, "item": item, "total_value": p['total_value'], "cases_opened": p['cases_opened']}

    return convert_decimals(result)


@router.get("/rounds")
async def list_rounds():
    async with _speed_case_race_registry_lock:
        rooms = list(_speed_case_race_rooms.values())
    result = {"rounds": [
        {
            'round_id': r.round_id, 'status': r.status,
            'player_count': len(r.players), 'deadline': r.deadline, 'bonus_pot': r.bonus_pot,
        }
        for r in rooms if r.status == 'racing'
    ]}
    return convert_decimals(result)


@router.get("/rounds/{round_id}")
async def get_round(round_id: int):
    async with _speed_case_race_registry_lock:
        room = _speed_case_race_rooms.get(round_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        rnd = await conn.fetchrow("SELECT * FROM speed_case_race_rounds WHERE id=$1", round_id)
        if not rnd:
            raise HTTPException(404, "Round not found")
        players = await conn.fetch("""
            SELECT p.*, u.username FROM speed_case_race_players p
            JOIN users u ON u.user_id = p.user_id
            WHERE p.round_id=$1 ORDER BY p.created_at
        """, round_id)
    result = {
        'round_id': round_id, 'status': rnd['status'], 'bonus_pot': rnd['bonus_pot'],
        'players': [
            {
                'user_id': p['user_id'], 'username': p['username'], 'entry_fee': p['entry_fee'],
                'total_value': p['total_value'], 'cases_opened': p['cases_opened'], 'bonus_won': p['bonus_won'],
            }
            for p in players
        ],
    }
    return convert_decimals(result)


# ============================================================
# WEBSOCKET
# ============================================================

@router.websocket("/ws/{round_id}")
async def speed_case_race_ws(websocket: WebSocket, round_id: int):
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

    async with _speed_case_race_registry_lock:
        room = _speed_case_race_rooms.get(round_id)
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
