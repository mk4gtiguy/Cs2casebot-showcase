# ============================================================
# routes/sync_slots.py
# CS2CaseBot | Sync-Spin Slots
#
# A social/lockstep variant of the existing solo Classic Slots
# machine (routes/games_easy.py). Players lock in a bet during a
# short countdown; when it ends everyone's reels spin at the same
# broadcasted moment -- but each player still gets their OWN
# independent weighted-RNG result via the exact solo reel logic
# (spin_classic_reel / evaluate_classic). There is no shared result
# to evaluate against, unlike Live Roulette/Keno -- the room only
# synchronizes timing for the social "watch your friends spin" feel.
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
    HOUSE_EDGE,
)
from routes.games_easy import spin_classic_reel, evaluate_classic, log_game

router = APIRouter(prefix="/api/games/sync-slots", tags=["sync-spin-slots"])

MIN_BET     = 50
MAX_BET     = 750_000
MAX_PLAYERS = 8
LOCK_SECS   = 8   # short "quick round" countdown, shorter than Roulette/Keno


def clamp_bet(amount: float) -> float:
    return shared_clamp_bet(amount, MIN_BET, MAX_BET)


# ============================================================
# TABLE SETUP
# ============================================================

async def init_sync_slots_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_slots_rounds (
                id            SERIAL PRIMARY KEY,
                status        TEXT DEFAULT 'locking'
                              CHECK (status IN ('locking','spinning','settled','cancelled')),
                created_at    TIMESTAMP DEFAULT NOW(),
                resolved_at   TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_slots_spins (
                id            SERIAL PRIMARY KEY,
                round_id      INTEGER NOT NULL REFERENCES sync_slots_rounds(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
                amount        DECIMAL(15,2) NOT NULL,
                reel_result   JSONB,
                payout        DECIMAL(15,2),
                created_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_slots_spins_round ON sync_slots_spins(round_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_slots_spins_user ON sync_slots_spins(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_slots_rounds_status ON sync_slots_rounds(status)")
    logger.info("✅ Sync-Spin Slots tables ready")


async def recover_stale_sync_slots_rounds():
    """Startup crash-recovery: a round left mid-flight (locking/spinning) from a
    crashed previous process can't be trusted -- refund every locked-in bet and
    mark the round cancelled."""
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id FROM sync_slots_rounds WHERE status IN ('locking','spinning') FOR UPDATE"
            )
            for row in stale:
                round_id = row['id']
                spins = await conn.fetch(
                    "SELECT user_id, amount FROM sync_slots_spins WHERE round_id=$1 AND payout IS NULL",
                    round_id
                )
                for s in spins:
                    await shared.add_balance(s['user_id'], float(s['amount']), conn)
                await conn.execute(
                    "UPDATE sync_slots_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                    round_id
                )
            if stale:
                logger.info(f"🎰 Recovered {len(stale)} stale Sync-Spin Slots round(s), refunded all locked-in bets")


# ============================================================
# ROOM
# ============================================================

class SyncSlotsRoom:
    def __init__(self, round_id: int):
        self.round_id = round_id
        self.status = 'locking'   # locking | spinning | settled | cancelled
        # user_id -> {username, amount}
        self.players: Dict[int, Dict] = {}
        self.ws_set: Set[WebSocket] = set()
        self.ws_map: Dict[int, WebSocket] = {}
        self.task: Optional[asyncio.Task] = None
        self.lock_deadline: Optional[float] = None
        self.lock = asyncio.Lock()
        self.created_at = time.time()

    async def broadcast(self, msg: dict):
        dead = await broadcast_to_set(self.ws_set, convert_decimals(msg))
        self.ws_set -= dead

    def snapshot(self) -> dict:
        return {
            'round_id': self.round_id,
            'status': self.status,
            'lock_deadline': self.lock_deadline,
            'players': [
                {'user_id': uid, **p} for uid, p in self.players.items()
            ],
        }

    async def run_lock_in(self):
        for sec in range(LOCK_SECS, 0, -1):
            await asyncio.sleep(1)
            async with self.lock:
                if self.status != 'locking':
                    return
            await self.broadcast({'type': 'lock_tick', 'round_id': self.round_id, 'seconds': sec - 1})
        await self.resolve()

    async def resolve(self):
        async with self.lock:
            if self.status != 'locking':
                return   # guards double-invocation
            self.status = 'spinning'
            await self.broadcast({'type': 'spin_now', 'round_id': self.round_id})

            if not self.players:
                pool = await get_db()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE sync_slots_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )
                self.status = 'cancelled'
                async with _sync_slots_registry_lock:
                    _sync_slots_rooms.pop(self.round_id, None)
                return

            results = []
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for uid, p in self.players.items():
                        symbols = [spin_classic_reel() for _ in range(3)]
                        mult, combo = evaluate_classic(symbols)
                        win = shared.apply_house(p['amount'] * mult, HOUSE_EDGE) if mult else 0
                        if win:
                            win = await credit_win(uid, win, conn)
                        await log_game(conn, uid, 'sync_slots', p['amount'], win,
                                       {'round': self.round_id, 'symbols': symbols, 'combo': combo})
                        await conn.execute("""
                            UPDATE sync_slots_spins SET reel_result=$1, payout=$2
                            WHERE round_id=$3 AND user_id=$4
                        """, {'symbols': symbols, 'combo': combo, 'mult': mult}, win, self.round_id, uid)
                        results.append({
                            'user_id': uid, 'username': p['username'], 'amount': p['amount'],
                            'symbols': symbols, 'combo': combo, 'mult': mult, 'win': win,
                        })
                    await conn.execute(
                        "UPDATE sync_slots_rounds SET status='settled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )

            self.status = 'settled'
            await self.broadcast({'type': 'results', 'round_id': self.round_id, 'players': results})
            async with _sync_slots_registry_lock:
                _sync_slots_rooms.pop(self.round_id, None)


# ============================================================
# REGISTRY
# ============================================================

_sync_slots_rooms: Dict[int, SyncSlotsRoom] = {}
_sync_slots_registry_lock = asyncio.Lock()


async def _get_or_create_open_room() -> SyncSlotsRoom:
    async with _sync_slots_registry_lock:
        for room in _sync_slots_rooms.values():
            if room.status == 'locking' and len(room.players) < MAX_PLAYERS:
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO sync_slots_rounds (status) VALUES ('locking') RETURNING id"
            )
        room = SyncSlotsRoom(row['id'])
        room.lock_deadline = time.time() + LOCK_SECS
        _sync_slots_rooms[room.round_id] = room
        return room


async def create_private_room(participant_user_ids: list, amount: float) -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py). Scoped to exactly 2 players -- stakes both
    atomically in one transaction and starts the round directly,
    skipping the open-lobby /join flow (_get_or_create_open_room)."""
    if len(participant_user_ids) != 2:
        raise HTTPException(400, "Sync-Spin Slots friend challenges need exactly 2 players")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO sync_slots_rounds (status) VALUES ('locking') RETURNING id"
            )
            round_id = row['id']

            players: Dict[int, Dict] = {}
            for uid in participant_user_ids:
                if not await deduct_balance(uid, amount, conn):
                    raise HTTPException(400, f"Player {uid} has insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                username = user_row['username'] if user_row else f'Player {uid}'
                await conn.execute(
                    "INSERT INTO sync_slots_spins (round_id, user_id, amount) VALUES ($1,$2,$3)",
                    round_id, uid, amount
                )
                players[uid] = {'username': username, 'amount': amount}

    room = SyncSlotsRoom(round_id)
    room.players = players
    room.lock_deadline = time.time() + LOCK_SECS
    async with _sync_slots_registry_lock:
        _sync_slots_rooms[round_id] = room
    room.task = asyncio.create_task(room.run_lock_in())
    await room.broadcast({'type': 'locking_start', 'round_id': round_id, 'round': room.snapshot()})
    return round_id


# ============================================================
# REST ROUTES
# ============================================================

class LockInRequest(BaseModel):
    amount: float
    round_id: Optional[int] = None


@router.post("/lock-in")
async def lock_in(req: LockInRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("sync-slots")
    bet = clamp_bet(req.amount)
    await ensure_user_exists(user_id)

    if req.round_id is not None:
        async with _sync_slots_registry_lock:
            room = _sync_slots_rooms.get(req.round_id)
        if not room:
            raise HTTPException(404, "Round not found or already closed")
    else:
        room = await _get_or_create_open_room()

    async with room.lock:
        if room.status != 'locking':
            raise HTTPException(400, "This round is no longer accepting bets")
        if len(room.players) >= MAX_PLAYERS:
            raise HTTPException(400, "This round is full")
        if user_id in room.players:
            raise HTTPException(400, "You're already locked in for this round")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'
                await conn.execute(
                    "INSERT INTO sync_slots_spins (round_id, user_id, amount) VALUES ($1,$2,$3)",
                    room.round_id, user_id, bet
                )

        room.players[user_id] = {'username': username, 'amount': bet}

        if room.task is None:
            room.task = asyncio.create_task(room.run_lock_in())

        await room.broadcast({'type': 'player_locked_in', 'round_id': room.round_id, 'round': room.snapshot()})
        result = {"success": True, "round_id": room.round_id, "round": room.snapshot()}

    return convert_decimals(result)


@router.get("/rounds")
async def list_rounds():
    async with _sync_slots_registry_lock:
        rooms = list(_sync_slots_rooms.values())
    result = {"rounds": [
        {
            'round_id': r.round_id, 'status': r.status,
            'player_count': len(r.players), 'lock_deadline': r.lock_deadline,
        }
        for r in rooms if r.status == 'locking'
    ]}
    return convert_decimals(result)


@router.get("/rounds/{round_id}")
async def get_round(round_id: int):
    async with _sync_slots_registry_lock:
        room = _sync_slots_rooms.get(round_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        rnd = await conn.fetchrow("SELECT * FROM sync_slots_rounds WHERE id=$1", round_id)
        if not rnd:
            raise HTTPException(404, "Round not found")
        spins = await conn.fetch("""
            SELECT s.*, u.username FROM sync_slots_spins s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.round_id=$1 ORDER BY s.created_at
        """, round_id)
    result = {
        'round_id': round_id, 'status': rnd['status'],
        'players': [
            {
                'user_id': s['user_id'], 'username': s['username'], 'amount': s['amount'],
                'reel_result': s['reel_result'], 'payout': s['payout'],
            }
            for s in spins
        ],
    }
    return convert_decimals(result)


@router.get("/history")
async def spin_history(limit: int = 20):
    limit = max(1, min(limit, 50))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT s.user_id, u.username, s.amount, s.payout, s.reel_result, s.created_at
            FROM sync_slots_spins s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.payout IS NOT NULL
            ORDER BY s.created_at DESC
            LIMIT $1
        """, limit)
    result = {"history": [
        {
            'user_id': r['user_id'], 'username': r['username'], 'amount': r['amount'],
            'payout': r['payout'], 'reel_result': r['reel_result'],
            'created_at': r['created_at'].isoformat() if r['created_at'] else None,
        }
        for r in rows
    ]}
    return convert_decimals(result)


# ============================================================
# WEBSOCKET
# ============================================================

@router.websocket("/ws/{round_id}")
async def sync_slots_ws(websocket: WebSocket, round_id: int):
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

    async with _sync_slots_registry_lock:
        room = _sync_slots_rooms.get(round_id)
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
