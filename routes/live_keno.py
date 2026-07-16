# ============================================================
# routes/live_keno.py
# CS2CaseBot | Live Keno Draw
#
# Lottery-style variant of the existing solo Keno (routes/games_easy.py).
# Players privately submit their own 1-10 number picks + wager during a
# picking countdown; at countdown end ONE shared draw of 20 numbers from
# 1-80 (secure_shuffle) resolves everyone's picks against the exact same
# KENO_PAYOUTS table used by the solo game. Shared draw, private picks --
# picks are locked after first submit to avoid balance-adjustment edge cases.
# ============================================================

import asyncio
import time
from typing import Dict, Set, Optional, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from pydantic import BaseModel

import shared
from shared import (
    logger, get_db, require_auth, ensure_user_exists,
    deduct_balance, broadcast_to_set, convert_decimals, secure_shuffle,
    credit_win, require_game_enabled, clamp_bet as shared_clamp_bet,
    HOUSE_EDGE,
)
from routes.games_easy import KENO_PAYOUTS, log_game

router = APIRouter(prefix="/api/games/live-keno", tags=["live-keno"])

MIN_BET      = 50
MAX_BET      = 750_000
PICKING_SECS = 20


def clamp_bet(amount: float) -> float:
    return shared_clamp_bet(amount, MIN_BET, MAX_BET)


# ============================================================
# TABLE SETUP
# ============================================================

async def init_live_keno_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS live_keno_rounds (
                id            SERIAL PRIMARY KEY,
                status        TEXT DEFAULT 'picking'
                              CHECK (status IN ('picking','drawing','settled','cancelled')),
                draw_numbers  INTEGER[],
                created_at    TIMESTAMP DEFAULT NOW(),
                resolved_at   TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS live_keno_picks (
                id            SERIAL PRIMARY KEY,
                round_id      INTEGER NOT NULL REFERENCES live_keno_rounds(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
                picks         INTEGER[] NOT NULL,
                amount        DECIMAL(15,2) NOT NULL,
                hits          INTEGER,
                payout        DECIMAL(15,2),
                created_at    TIMESTAMP DEFAULT NOW(),
                UNIQUE(round_id, user_id)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_live_keno_picks_round ON live_keno_picks(round_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_live_keno_picks_user ON live_keno_picks(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_live_keno_rounds_status ON live_keno_rounds(status)")
    logger.info("✅ Live Keno Draw tables ready")


async def recover_stale_live_keno_rounds():
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id FROM live_keno_rounds WHERE status IN ('picking','drawing') FOR UPDATE"
            )
            for row in stale:
                round_id = row['id']
                picks = await conn.fetch(
                    "SELECT user_id, amount FROM live_keno_picks WHERE round_id=$1 AND payout IS NULL",
                    round_id
                )
                for p in picks:
                    await shared.add_balance(p['user_id'], float(p['amount']), conn)
                await conn.execute(
                    "UPDATE live_keno_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                    round_id
                )
            if stale:
                logger.info(f"🎱 Recovered {len(stale)} stale Live Keno round(s), refunded all picks")


# ============================================================
# ROOM
# ============================================================

class LiveKenoRoom:
    def __init__(self, round_id: int):
        self.round_id = round_id
        self.status = 'picking'   # picking | drawing | settled | cancelled
        # user_id -> {username, picks, amount}
        self.players: Dict[int, Dict] = {}
        self.ws_set: Set[WebSocket] = set()
        self.ws_map: Dict[int, WebSocket] = {}
        self.task: Optional[asyncio.Task] = None
        self.picking_deadline: Optional[float] = None
        self.lock = asyncio.Lock()
        self.created_at = time.time()

    async def broadcast(self, msg: dict):
        dead = await broadcast_to_set(self.ws_set, convert_decimals(msg))
        self.ws_set -= dead

    def snapshot(self) -> dict:
        return {
            'round_id': self.round_id,
            'status': self.status,
            'picking_deadline': self.picking_deadline,
            'players': [
                {'user_id': uid, 'username': p['username'], 'pick_count': len(p['picks']), 'amount': p['amount']}
                for uid, p in self.players.items()
            ],
        }

    async def run_picking(self):
        for sec in range(PICKING_SECS, 0, -1):
            await asyncio.sleep(1)
            async with self.lock:
                if self.status != 'picking':
                    return
            await self.broadcast({'type': 'picking_tick', 'round_id': self.round_id, 'seconds': sec - 1})
        await self.resolve()

    async def resolve(self):
        async with self.lock:
            if self.status != 'picking':
                return   # guards double-invocation
            self.status = 'drawing'
            await self.broadcast({'type': 'drawing', 'round_id': self.round_id})

            if not self.players:
                pool = await get_db()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE live_keno_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )
                self.status = 'cancelled'
                async with _live_keno_registry_lock:
                    _live_keno_rooms.pop(self.round_id, None)
                return

            drawn = secure_shuffle(list(range(1, 81)))[:20]
            results = []
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for uid, p in self.players.items():
                        hits = [n for n in p['picks'] if n in drawn]
                        n_hits = len(hits)
                        payout_table = KENO_PAYOUTS.get(len(p['picks']), {})
                        mult = payout_table.get(n_hits, 0)
                        win = shared.apply_house(p['amount'] * mult, HOUSE_EDGE) if mult else 0
                        if win:
                            win = await credit_win(uid, win, conn)
                        await log_game(conn, uid, 'live_keno', p['amount'], win, {
                            'round': self.round_id, 'picks': p['picks'], 'drawn': drawn,
                            'hits': hits, 'mult': mult,
                        })
                        await conn.execute("""
                            UPDATE live_keno_picks SET hits=$1, payout=$2
                            WHERE round_id=$3 AND user_id=$4
                        """, n_hits, win, self.round_id, uid)
                        results.append({
                            'user_id': uid, 'username': p['username'], 'picks': p['picks'],
                            'amount': p['amount'], 'hits': hits, 'n_hits': n_hits, 'mult': mult, 'win': win,
                        })
                    await conn.execute("""
                        UPDATE live_keno_rounds SET status='settled', draw_numbers=$1, resolved_at=NOW()
                        WHERE id=$2
                    """, drawn, self.round_id)

            self.status = 'settled'
            await self.broadcast({'type': 'result', 'round_id': self.round_id, 'draw': drawn, 'players': results})
            async with _live_keno_registry_lock:
                _live_keno_rooms.pop(self.round_id, None)


# ============================================================
# REGISTRY
# ============================================================

_live_keno_rooms: Dict[int, LiveKenoRoom] = {}
_live_keno_registry_lock = asyncio.Lock()


async def _get_or_create_open_room() -> LiveKenoRoom:
    async with _live_keno_registry_lock:
        for room in _live_keno_rooms.values():
            if room.status == 'picking':
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO live_keno_rounds (status) VALUES ('picking') RETURNING id"
            )
        room = LiveKenoRoom(row['id'])
        room.picking_deadline = time.time() + PICKING_SECS
        _live_keno_rooms[room.round_id] = room
        return room


async def create_private_room(participant_user_ids: list, amount: float) -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py). Scoped to exactly 2 players. Live Keno's
    public /pick conflates seating with submitting picks in one action
    (unlike the duel games) -- rather than building new challenge-
    creation UI to let the sender pre-select numbers, each player is
    auto-assigned a fresh random 5-number pick set (same secure_shuffle
    idiom used for the shared draw itself), so accepting the challenge
    stakes real money immediately like every other hook."""
    if len(participant_user_ids) != 2:
        raise HTTPException(400, "Live Keno Draw friend challenges need exactly 2 players")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO live_keno_rounds (status) VALUES ('picking') RETURNING id"
            )
            round_id = row['id']

            players: Dict[int, Dict] = {}
            for uid in participant_user_ids:
                if not await deduct_balance(uid, amount, conn):
                    raise HTTPException(400, f"Player {uid} has insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                username = user_row['username'] if user_row else f'Player {uid}'
                picks = sorted(secure_shuffle(list(range(1, 81)))[:5])
                await conn.execute(
                    "INSERT INTO live_keno_picks (round_id, user_id, picks, amount) VALUES ($1,$2,$3,$4)",
                    round_id, uid, picks, amount
                )
                players[uid] = {'username': username, 'picks': picks, 'amount': amount}

    room = LiveKenoRoom(round_id)
    room.players = players
    room.picking_deadline = time.time() + PICKING_SECS
    async with _live_keno_registry_lock:
        _live_keno_rooms[round_id] = room
    room.task = asyncio.create_task(room.run_picking())
    await room.broadcast({'type': 'round_start', 'round_id': round_id, 'round': room.snapshot()})
    return round_id


# ============================================================
# REST ROUTES
# ============================================================

class PickRequest(BaseModel):
    picks: List[int]
    amount: float
    round_id: Optional[int] = None


@router.post("/pick")
async def submit_picks(req: PickRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("live-keno")
    amount = clamp_bet(req.amount)
    picks = sorted(set(req.picks))
    if not 1 <= len(picks) <= 10:
        raise HTTPException(400, "Pick 1-10 numbers")
    if any(n < 1 or n > 80 for n in picks):
        raise HTTPException(400, "Numbers must be 1-80")
    await ensure_user_exists(user_id)

    if req.round_id is not None:
        async with _live_keno_registry_lock:
            room = _live_keno_rooms.get(req.round_id)
        if not room:
            raise HTTPException(404, "Round not found or already closed")
    else:
        room = await _get_or_create_open_room()

    async with room.lock:
        if room.status != 'picking':
            raise HTTPException(400, "This round is no longer accepting picks")
        if user_id in room.players:
            raise HTTPException(400, "You've already locked in picks for this round")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await deduct_balance(user_id, amount, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'
                await conn.execute(
                    "INSERT INTO live_keno_picks (round_id, user_id, picks, amount) VALUES ($1,$2,$3,$4)",
                    room.round_id, user_id, picks, amount
                )

        room.players[user_id] = {'username': username, 'picks': picks, 'amount': amount}

        if room.task is None:
            room.task = asyncio.create_task(room.run_picking())

        await room.broadcast({'type': 'player_picked', 'round_id': room.round_id, 'round': room.snapshot()})
        result = {"success": True, "round_id": room.round_id, "round": room.snapshot()}

    return convert_decimals(result)


@router.get("/rounds")
async def list_rounds():
    async with _live_keno_registry_lock:
        rooms = list(_live_keno_rooms.values())
    result = {"rounds": [
        {
            'round_id': r.round_id, 'status': r.status,
            'player_count': len(r.players), 'picking_deadline': r.picking_deadline,
        }
        for r in rooms if r.status == 'picking'
    ]}
    return convert_decimals(result)


@router.get("/rounds/{round_id}")
async def get_round(round_id: int):
    async with _live_keno_registry_lock:
        room = _live_keno_rooms.get(round_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        rnd = await conn.fetchrow("SELECT * FROM live_keno_rounds WHERE id=$1", round_id)
        if not rnd:
            raise HTTPException(404, "Round not found")
        picks = await conn.fetch("""
            SELECT p.*, u.username FROM live_keno_picks p
            JOIN users u ON u.user_id = p.user_id
            WHERE p.round_id=$1 ORDER BY p.created_at
        """, round_id)
    result = {
        'round_id': round_id, 'status': rnd['status'], 'draw_numbers': rnd['draw_numbers'],
        'players': [
            {
                'user_id': p['user_id'], 'username': p['username'], 'picks': p['picks'],
                'amount': p['amount'], 'hits': p['hits'], 'payout': p['payout'],
            }
            for p in picks
        ],
    }
    return convert_decimals(result)


@router.get("/history")
async def draw_history(limit: int = 20):
    limit = max(1, min(limit, 50))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, draw_numbers, resolved_at FROM live_keno_rounds
            WHERE status='settled' ORDER BY resolved_at DESC LIMIT $1
        """, limit)
    result = {"history": [
        {
            'round_id': r['id'], 'draw_numbers': r['draw_numbers'],
            'resolved_at': r['resolved_at'].isoformat() if r['resolved_at'] else None,
        }
        for r in rows
    ]}
    return convert_decimals(result)


# ============================================================
# WEBSOCKET
# ============================================================

@router.websocket("/ws/{round_id}")
async def live_keno_ws(websocket: WebSocket, round_id: int):
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

    async with _live_keno_registry_lock:
        room = _live_keno_rooms.get(round_id)
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
