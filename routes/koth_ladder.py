# ============================================================
# routes/koth_ladder.py
# CS2CaseBot | King of the Hill Ladder
#
# Multiplayer variant of the solo Ladder Climb game. Each player
# places their OWN stake and gets their OWN solo-style cashout --
# exact same LADDER_RUNGS fail-chance/multiplier table and payout
# math as solo Ladder Climb (routes/games_medium.py). The PvP twist
# is a bonus pot layered ON TOP, funded by a flat 5% cut of every
# player's stake (same "toll on top of the real stake" idea used by
# Session 4's Friends challenge system): whoever holds the single
# highest rung among non-busted players when the round ends wins
# that bonus pot too, on top of their own normal cashout.
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
    HOUSE_EDGE, secure_random,
)
from routes.games_medium import LADDER_RUNGS, log_game

router = APIRouter(prefix="/api/games/koth-ladder", tags=["koth-ladder"])

MIN_BET      = 50
MAX_BET      = 750_000
MAX_PLAYERS  = 8
CLIMB_SECS   = 25
TOLL_RATE    = 0.05   # flat cut of each stake funding the bonus pot


def clamp_bet(amount: float) -> float:
    return shared_clamp_bet(amount, MIN_BET, MAX_BET)


# ============================================================
# TABLE SETUP
# ============================================================

async def init_koth_ladder_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS koth_ladder_rounds (
                id            SERIAL PRIMARY KEY,
                status        TEXT DEFAULT 'climbing'
                              CHECK (status IN ('climbing','settled','cancelled')),
                bonus_pot     DECIMAL(15,2) DEFAULT 0,
                created_at    TIMESTAMP DEFAULT NOW(),
                resolved_at   TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS koth_ladder_players (
                id            SERIAL PRIMARY KEY,
                round_id      INTEGER NOT NULL REFERENCES koth_ladder_rounds(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
                stake         DECIMAL(15,2) NOT NULL,
                rung          INTEGER DEFAULT 0,
                mult          DECIMAL(10,4) DEFAULT 1.0,
                busted        BOOLEAN DEFAULT FALSE,
                cashed_out    BOOLEAN DEFAULT FALSE,
                payout        DECIMAL(15,2),
                bonus_won     DECIMAL(15,2) DEFAULT 0,
                created_at    TIMESTAMP DEFAULT NOW(),
                UNIQUE(round_id, user_id)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_koth_ladder_players_round ON koth_ladder_players(round_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_koth_ladder_players_user ON koth_ladder_players(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_koth_ladder_rounds_status ON koth_ladder_rounds(status)")
    logger.info("✅ King of the Hill Ladder tables ready")


async def recover_stale_koth_ladder_rounds():
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id FROM koth_ladder_rounds WHERE status='climbing' FOR UPDATE"
            )
            for row in stale:
                round_id = row['id']
                players = await conn.fetch(
                    "SELECT user_id, stake FROM koth_ladder_players WHERE round_id=$1 AND payout IS NULL",
                    round_id
                )
                for p in players:
                    # Refund the full original stake -- the 5% toll was carved
                    # out of it into bonus_pot but never actually left the
                    # player's ownership in any recoverable sense, so refunding
                    # the full stake makes them whole again.
                    await shared.add_balance(p['user_id'], float(p['stake']), conn)
                await conn.execute(
                    "UPDATE koth_ladder_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                    round_id
                )
            if stale:
                logger.info(f"🪜 Recovered {len(stale)} stale KOTH Ladder round(s), refunded all stakes")


# ============================================================
# ROOM
# ============================================================

class KOTHRoom:
    def __init__(self, round_id: int):
        self.round_id = round_id
        self.status = 'climbing'
        # user_id -> {username, stake, rung, mult, busted, cashed_out}
        self.players: Dict[int, Dict] = {}
        self.bonus_pot = 0.0
        self.ws_set: Set[WebSocket] = set()
        self.ws_map: Dict[int, WebSocket] = {}
        self.task: Optional[asyncio.Task] = None
        self.climb_deadline: Optional[float] = None
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
            'climb_deadline': self.climb_deadline,
            'players': [
                {'user_id': uid, **{k: v for k, v in p.items() if k != 'positions'}}
                for uid, p in self.players.items()
            ],
        }

    async def run_climbing(self):
        for sec in range(CLIMB_SECS, 0, -1):
            await asyncio.sleep(1)
            async with self.lock:
                if self.status != 'climbing':
                    return
            await self.broadcast({'type': 'climb_tick', 'round_id': self.round_id, 'seconds': sec - 1})
        await self.resolve()

    async def resolve(self):
        async with self.lock:
            if self.status != 'climbing':
                return
            self.status = 'settled'
            await self.broadcast({'type': 'round_ending', 'round_id': self.round_id})

            if not self.players:
                pool = await get_db()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE koth_ladder_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )
                self.status = 'cancelled'
                async with _koth_ladder_registry_lock:
                    _koth_ladder_rooms.pop(self.round_id, None)
                return

            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Auto-cashout anyone still mid-climb (never busted, never
                    # manually cashed out) at their current rung/mult -- nobody's
                    # stake is left stuck just because the timer ran out.
                    for uid, p in self.players.items():
                        if not p['busted'] and not p['cashed_out']:
                            win = shared.apply_house(p['stake'] * p['mult'], HOUSE_EDGE)
                            win = await credit_win(uid, win, conn) if win else win
                            p['cashed_out'] = True
                            p['payout'] = win
                            await conn.execute(
                                "UPDATE koth_ladder_players SET cashed_out=TRUE, payout=$1 WHERE round_id=$2 AND user_id=$3",
                                win, self.round_id, uid
                            )
                            await log_game(conn, uid, 'koth_ladder', p['stake'], win, {
                                'round': self.round_id, 'rung': p['rung'], 'mult': p['mult'], 'auto_cashout': True,
                            })

                    # Bonus pot: highest rung among non-busted players wins (ties split)
                    survivors = [uid for uid, p in self.players.items() if not p['busted']]
                    if survivors and self.bonus_pot > 0:
                        top_rung = max(self.players[uid]['rung'] for uid in survivors)
                        winners = [uid for uid in survivors if self.players[uid]['rung'] == top_rung]
                        share = round(self.bonus_pot / len(winners), 2)
                        for uid in winners:
                            share_final = await credit_win(uid, share, conn)
                            self.players[uid]['bonus_won'] = share_final
                            await conn.execute(
                                "UPDATE koth_ladder_players SET bonus_won=$1 WHERE round_id=$2 AND user_id=$3",
                                share_final, self.round_id, uid
                            )

                    await conn.execute(
                        "UPDATE koth_ladder_rounds SET status='settled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )

            await self.broadcast({'type': 'result', 'round_id': self.round_id, **self.snapshot()})
            async with _koth_ladder_registry_lock:
                _koth_ladder_rooms.pop(self.round_id, None)


# ============================================================
# REGISTRY
# ============================================================

_koth_ladder_rooms: Dict[int, KOTHRoom] = {}
_koth_ladder_registry_lock = asyncio.Lock()


async def _get_or_create_open_room() -> KOTHRoom:
    async with _koth_ladder_registry_lock:
        for room in _koth_ladder_rooms.values():
            if room.status == 'climbing' and len(room.players) < MAX_PLAYERS:
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO koth_ladder_rounds (status) VALUES ('climbing') RETURNING id"
            )
        room = KOTHRoom(row['id'])
        room.climb_deadline = time.time() + CLIMB_SECS
        _koth_ladder_rooms[room.round_id] = room
        return room


async def create_private_room(participant_user_ids: list, stake: float) -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py). Scoped to exactly 2 players -- stakes both
    atomically in one transaction (including the same 5% bonus-pot
    toll the public /join charges) and starts the round directly,
    skipping the open-lobby /join flow (_get_or_create_open_room)."""
    if len(participant_user_ids) != 2:
        raise HTTPException(400, "King of the Hill Ladder friend challenges need exactly 2 players")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO koth_ladder_rounds (status) VALUES ('climbing') RETURNING id"
            )
            round_id = row['id']

            toll = round(stake * TOLL_RATE, 2)
            bonus_pot = 0.0
            players: Dict[int, Dict] = {}
            for uid in participant_user_ids:
                if not await deduct_balance(uid, stake, conn):
                    raise HTTPException(400, f"Player {uid} has insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                username = user_row['username'] if user_row else f'Player {uid}'
                await conn.execute(
                    "INSERT INTO koth_ladder_players (round_id, user_id, stake) VALUES ($1,$2,$3)",
                    round_id, uid, stake
                )
                players[uid] = {
                    'username': username, 'stake': stake, 'rung': 0, 'mult': 1.0,
                    'busted': False, 'cashed_out': False, 'payout': None, 'bonus_won': 0,
                }
                bonus_pot = round(bonus_pot + toll, 2)
            await conn.execute(
                "UPDATE koth_ladder_rounds SET bonus_pot = $1 WHERE id=$2", bonus_pot, round_id
            )

    room = KOTHRoom(round_id)
    room.players = players
    room.bonus_pot = bonus_pot
    room.climb_deadline = time.time() + CLIMB_SECS
    async with _koth_ladder_registry_lock:
        _koth_ladder_rooms[round_id] = room
    room.task = asyncio.create_task(room.run_climbing())
    await room.broadcast({'type': 'round_start', 'round_id': round_id, 'round': room.snapshot()})
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
    await require_game_enabled("koth-ladder")
    stake = clamp_bet(req.amount)
    await ensure_user_exists(user_id)

    if req.round_id is not None:
        async with _koth_ladder_registry_lock:
            room = _koth_ladder_rooms.get(req.round_id)
        if not room:
            raise HTTPException(404, "Round not found or already closed")
    else:
        room = await _get_or_create_open_room()

    async with room.lock:
        if room.status != 'climbing':
            raise HTTPException(400, "This round is no longer accepting players")
        if len(room.players) >= MAX_PLAYERS:
            raise HTTPException(400, "This round is full")
        if user_id in room.players:
            raise HTTPException(400, "You're already climbing in this round")

        toll = round(stake * TOLL_RATE, 2)
        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await deduct_balance(user_id, stake, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'
                await conn.execute(
                    "INSERT INTO koth_ladder_players (round_id, user_id, stake) VALUES ($1,$2,$3)",
                    room.round_id, user_id, stake
                )
                await conn.execute(
                    "UPDATE koth_ladder_rounds SET bonus_pot = bonus_pot + $1 WHERE id=$2",
                    toll, room.round_id
                )

        room.bonus_pot = round(room.bonus_pot + toll, 2)
        room.players[user_id] = {
            'username': username, 'stake': stake, 'rung': 0, 'mult': 1.0,
            'busted': False, 'cashed_out': False, 'payout': None, 'bonus_won': 0,
        }

        if room.task is None:
            room.task = asyncio.create_task(room.run_climbing())

        await room.broadcast({'type': 'player_joined', 'round_id': room.round_id, 'round': room.snapshot()})
        result = {"success": True, "round_id": room.round_id, "round": room.snapshot()}

    return convert_decimals(result)


@router.post("/climb")
async def climb(body: RoundIdBody, request: Request):
    user_id = await require_auth(request)
    async with _koth_ladder_registry_lock:
        room = _koth_ladder_rooms.get(body.round_id)
    if not room:
        raise HTTPException(404, "Round not found or already closed")

    async with room.lock:
        if room.status != 'climbing':
            raise HTTPException(400, "This round has already ended")
        p = room.players.get(user_id)
        if not p:
            raise HTTPException(400, "You're not climbing in this round")
        if p['busted'] or p['cashed_out']:
            raise HTTPException(400, "You can no longer act this round")
        if p['rung'] >= len(LADDER_RUNGS):
            raise HTTPException(400, "You've already reached the top")

        fail_chance, rung_mult = LADDER_RUNGS[p['rung']]
        failed = secure_random() < fail_chance
        if failed:
            p['busted'] = True
            async with (await get_db()).acquire() as conn:
                await conn.execute(
                    "UPDATE koth_ladder_players SET busted=TRUE, payout=0 WHERE round_id=$1 AND user_id=$2",
                    room.round_id, user_id
                )
            p['payout'] = 0
        else:
            p['mult'] = round(p['mult'] * rung_mult, 4)
            p['rung'] += 1
            async with (await get_db()).acquire() as conn:
                await conn.execute(
                    "UPDATE koth_ladder_players SET rung=$1, mult=$2 WHERE round_id=$3 AND user_id=$4",
                    p['rung'], p['mult'], room.round_id, user_id
                )

        await room.broadcast({'type': 'player_climbed', 'round_id': room.round_id, 'user_id': user_id,
                               'failed': failed, 'rung': p['rung'], 'mult': p['mult'], 'busted': p['busted']})
        result = {"success": True, "failed": failed, "rung": p['rung'], "mult": p['mult'], "busted": p['busted']}

    return convert_decimals(result)


@router.post("/cashout")
async def cashout(body: RoundIdBody, request: Request):
    user_id = await require_auth(request)
    async with _koth_ladder_registry_lock:
        room = _koth_ladder_rooms.get(body.round_id)
    if not room:
        raise HTTPException(404, "Round not found or already closed")

    async with room.lock:
        if room.status != 'climbing':
            raise HTTPException(400, "This round has already ended")
        p = room.players.get(user_id)
        if not p:
            raise HTTPException(400, "You're not climbing in this round")
        if p['busted'] or p['cashed_out']:
            raise HTTPException(400, "You can no longer act this round")

        win = shared.apply_house(p['stake'] * p['mult'], HOUSE_EDGE)
        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                win = await credit_win(user_id, win, conn) if win else win
                p['cashed_out'] = True
                p['payout'] = win
                await conn.execute(
                    "UPDATE koth_ladder_players SET cashed_out=TRUE, payout=$1 WHERE round_id=$2 AND user_id=$3",
                    win, room.round_id, user_id
                )
                await log_game(conn, user_id, 'koth_ladder', p['stake'], win, {
                    'round': room.round_id, 'rung': p['rung'], 'mult': p['mult'], 'auto_cashout': False,
                })

        await room.broadcast({'type': 'player_cashed_out', 'round_id': room.round_id, 'user_id': user_id, 'win': win})
        result = {"success": True, "win": win}

    return convert_decimals(result)


@router.get("/rounds")
async def list_rounds():
    async with _koth_ladder_registry_lock:
        rooms = list(_koth_ladder_rooms.values())
    result = {"rounds": [
        {
            'round_id': r.round_id, 'status': r.status,
            'player_count': len(r.players), 'climb_deadline': r.climb_deadline, 'bonus_pot': r.bonus_pot,
        }
        for r in rooms if r.status == 'climbing'
    ]}
    return convert_decimals(result)


@router.get("/rounds/{round_id}")
async def get_round(round_id: int):
    async with _koth_ladder_registry_lock:
        room = _koth_ladder_rooms.get(round_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        rnd = await conn.fetchrow("SELECT * FROM koth_ladder_rounds WHERE id=$1", round_id)
        if not rnd:
            raise HTTPException(404, "Round not found")
        players = await conn.fetch("""
            SELECT p.*, u.username FROM koth_ladder_players p
            JOIN users u ON u.user_id = p.user_id
            WHERE p.round_id=$1 ORDER BY p.created_at
        """, round_id)
    result = {
        'round_id': round_id, 'status': rnd['status'], 'bonus_pot': rnd['bonus_pot'],
        'players': [
            {
                'user_id': p['user_id'], 'username': p['username'], 'stake': p['stake'],
                'rung': p['rung'], 'mult': p['mult'], 'busted': p['busted'],
                'cashed_out': p['cashed_out'], 'payout': p['payout'], 'bonus_won': p['bonus_won'],
            }
            for p in players
        ],
    }
    return convert_decimals(result)


# ============================================================
# WEBSOCKET
# ============================================================

@router.websocket("/ws/{round_id}")
async def koth_ladder_ws(websocket: WebSocket, round_id: int):
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

    async with _koth_ladder_registry_lock:
        room = _koth_ladder_rooms.get(round_id)
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
