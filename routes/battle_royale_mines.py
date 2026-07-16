# ============================================================
# routes/battle_royale_mines.py
# CS2CaseBot | Battle Royale Minefield
#
# Multiplayer variant of solo Mines. Each player has their OWN
# independent 5x5 board (own secure_shuffle()-placed mines) and gets
# their OWN solo-style cashout -- exact same hypergeometric
# mines_multiplier() formula and payout math as solo Mines
# (routes/games_medium.py). The PvP twist: hitting a mine redirects
# that player's stake into a shared bonus pot instead of it vanishing
# to pure house edge -- losing to a mine now visibly benefits a
# fellow player. Whoever survives with the single highest multiplier
# when the round ends wins that bonus pot too, on top of their own
# normal cashout.
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
    HOUSE_EDGE, secure_shuffle,
)
from routes.games_medium import mines_multiplier, log_game

router = APIRouter(prefix="/api/games/battle-royale-mines", tags=["battle-royale-mines"])

MIN_BET      = 50
MAX_BET      = 750_000
MAX_PLAYERS  = 8
GRID_SIZE    = 25
REVEAL_SECS  = 30


def clamp_bet(amount: float) -> float:
    return shared_clamp_bet(amount, MIN_BET, MAX_BET)


# ============================================================
# TABLE SETUP
# ============================================================

async def init_battle_royale_mines_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS battle_royale_mines_rounds (
                id            SERIAL PRIMARY KEY,
                status        TEXT DEFAULT 'revealing'
                              CHECK (status IN ('revealing','settled','cancelled')),
                bonus_pot     DECIMAL(15,2) DEFAULT 0,
                created_at    TIMESTAMP DEFAULT NOW(),
                resolved_at   TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS battle_royale_mines_players (
                id            SERIAL PRIMARY KEY,
                round_id      INTEGER NOT NULL REFERENCES battle_royale_mines_rounds(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
                stake         DECIMAL(15,2) NOT NULL,
                mines         INTEGER NOT NULL,
                positions     INTEGER[] NOT NULL,
                revealed      INTEGER DEFAULT 0,
                mult          DECIMAL(10,4) DEFAULT 1.0,
                busted        BOOLEAN DEFAULT FALSE,
                cashed_out    BOOLEAN DEFAULT FALSE,
                payout        DECIMAL(15,2),
                bonus_won     DECIMAL(15,2) DEFAULT 0,
                created_at    TIMESTAMP DEFAULT NOW(),
                UNIQUE(round_id, user_id)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_battle_royale_mines_players_round ON battle_royale_mines_players(round_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_battle_royale_mines_players_user ON battle_royale_mines_players(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_battle_royale_mines_rounds_status ON battle_royale_mines_rounds(status)")
    logger.info("✅ Battle Royale Minefield tables ready")


async def recover_stale_battle_royale_mines_rounds():
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id FROM battle_royale_mines_rounds WHERE status='revealing' FOR UPDATE"
            )
            for row in stale:
                round_id = row['id']
                players = await conn.fetch(
                    "SELECT user_id, stake FROM battle_royale_mines_players WHERE round_id=$1 AND payout IS NULL",
                    round_id
                )
                for p in players:
                    await shared.add_balance(p['user_id'], float(p['stake']), conn)
                await conn.execute(
                    "UPDATE battle_royale_mines_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                    round_id
                )
            if stale:
                logger.info(f"💣 Recovered {len(stale)} stale Battle Royale Minefield round(s), refunded all stakes")


# ============================================================
# ROOM
# ============================================================

class MinefieldRoom:
    def __init__(self, round_id: int):
        self.round_id = round_id
        self.status = 'revealing'
        # user_id -> {username, stake, mines, positions, revealed, mult, busted, cashed_out}
        self.players: Dict[int, Dict] = {}
        self.bonus_pot = 0.0
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
            'players': [
                {'user_id': uid, **{k: v for k, v in p.items() if k != 'positions'}}
                for uid, p in self.players.items()
            ],
        }

    async def _all_settled(self) -> bool:
        return all(p['busted'] or p['cashed_out'] for p in self.players.values())

    async def run_revealing(self):
        for sec in range(REVEAL_SECS, 0, -1):
            await asyncio.sleep(1)
            async with self.lock:
                if self.status != 'revealing':
                    return
                if self.players and await self._all_settled():
                    break
            await self.broadcast({'type': 'reveal_tick', 'round_id': self.round_id, 'seconds': sec - 1})
        await self.resolve()

    async def resolve(self):
        async with self.lock:
            if self.status != 'revealing':
                return
            self.status = 'settled'
            await self.broadcast({'type': 'round_ending', 'round_id': self.round_id})

            if not self.players:
                pool = await get_db()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE battle_royale_mines_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )
                self.status = 'cancelled'
                async with _battle_royale_mines_registry_lock:
                    _battle_royale_mines_rooms.pop(self.round_id, None)
                return

            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for uid, p in self.players.items():
                        if not p['busted'] and not p['cashed_out']:
                            win = shared.apply_house(p['stake'] * p['mult'], HOUSE_EDGE)
                            win = await credit_win(uid, win, conn) if win else win
                            p['cashed_out'] = True
                            p['payout'] = win
                            await conn.execute(
                                "UPDATE battle_royale_mines_players SET cashed_out=TRUE, payout=$1 WHERE round_id=$2 AND user_id=$3",
                                win, self.round_id, uid
                            )
                            await log_game(conn, uid, 'battle_royale_mines', p['stake'], win, {
                                'round': self.round_id, 'revealed': p['revealed'], 'mult': p['mult'], 'auto_cashout': True,
                            })

                    survivors = [uid for uid, p in self.players.items() if not p['busted']]
                    if survivors and self.bonus_pot > 0:
                        top_mult = max(self.players[uid]['mult'] for uid in survivors)
                        winners = [uid for uid in survivors if self.players[uid]['mult'] == top_mult]
                        share = round(self.bonus_pot / len(winners), 2)
                        for uid in winners:
                            share_final = await credit_win(uid, share, conn)
                            self.players[uid]['bonus_won'] = share_final
                            await conn.execute(
                                "UPDATE battle_royale_mines_players SET bonus_won=$1 WHERE round_id=$2 AND user_id=$3",
                                share_final, self.round_id, uid
                            )

                    await conn.execute(
                        "UPDATE battle_royale_mines_rounds SET status='settled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )

            await self.broadcast({'type': 'result', 'round_id': self.round_id, **self.snapshot()})
            async with _battle_royale_mines_registry_lock:
                _battle_royale_mines_rooms.pop(self.round_id, None)


# ============================================================
# REGISTRY
# ============================================================

_battle_royale_mines_rooms: Dict[int, MinefieldRoom] = {}
_battle_royale_mines_registry_lock = asyncio.Lock()


async def _get_or_create_open_room() -> MinefieldRoom:
    async with _battle_royale_mines_registry_lock:
        for room in _battle_royale_mines_rooms.values():
            if room.status == 'revealing' and len(room.players) < MAX_PLAYERS:
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO battle_royale_mines_rounds (status) VALUES ('revealing') RETURNING id"
            )
        room = MinefieldRoom(row['id'])
        room.deadline = time.time() + REVEAL_SECS
        _battle_royale_mines_rooms[room.round_id] = room
        return room


async def create_private_room(participant_user_ids: list, stake: float, mines: int = 3) -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py). Scoped to exactly 2 players -- stakes both
    atomically in one transaction and starts the round directly,
    skipping the open-lobby /join flow (_get_or_create_open_room).
    Each player still gets their own independently shuffled mine
    positions, matching the public /join flow's per-player
    randomization exactly. `mines` defaults to 3 (matches Mines
    Race's fixed count) since the Friends challenge flow doesn't
    expose a per-player mine-count picker."""
    if len(participant_user_ids) != 2:
        raise HTTPException(400, "Battle Royale Minefield friend challenges need exactly 2 players")
    mines = max(1, min(24, int(mines)))

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO battle_royale_mines_rounds (status) VALUES ('revealing') RETURNING id"
            )
            round_id = row['id']

            players: Dict[int, Dict] = {}
            for uid in participant_user_ids:
                if not await deduct_balance(uid, stake, conn):
                    raise HTTPException(400, f"Player {uid} has insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                username = user_row['username'] if user_row else f'Player {uid}'
                positions = secure_shuffle(list(range(GRID_SIZE)))[:mines]
                await conn.execute("""
                    INSERT INTO battle_royale_mines_players (round_id, user_id, stake, mines, positions)
                    VALUES ($1,$2,$3,$4,$5)
                """, round_id, uid, stake, mines, positions)
                players[uid] = {
                    'username': username, 'stake': stake, 'mines': mines, 'positions': positions,
                    'revealed': 0, 'mult': 1.0, 'busted': False, 'cashed_out': False,
                    'payout': None, 'bonus_won': 0,
                }

    room = MinefieldRoom(round_id)
    room.players = players
    room.deadline = time.time() + REVEAL_SECS
    async with _battle_royale_mines_registry_lock:
        _battle_royale_mines_rooms[round_id] = room
    room.task = asyncio.create_task(room.run_revealing())
    await room.broadcast({'type': 'round_start', 'round_id': round_id, 'round': room.snapshot()})
    return round_id


# ============================================================
# REST ROUTES
# ============================================================

class JoinRequest(BaseModel):
    amount: float
    mines: int
    round_id: Optional[int] = None


class RoundIdBody(BaseModel):
    round_id: int


@router.post("/join")
async def join_round(req: JoinRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("battle-royale-mines")
    stake = clamp_bet(req.amount)
    mines = max(1, min(24, int(req.mines)))
    await ensure_user_exists(user_id)

    if req.round_id is not None:
        async with _battle_royale_mines_registry_lock:
            room = _battle_royale_mines_rooms.get(req.round_id)
        if not room:
            raise HTTPException(404, "Round not found or already closed")
    else:
        room = await _get_or_create_open_room()

    async with room.lock:
        if room.status != 'revealing':
            raise HTTPException(400, "This round is no longer accepting players")
        if len(room.players) >= MAX_PLAYERS:
            raise HTTPException(400, "This round is full")
        if user_id in room.players:
            raise HTTPException(400, "You're already in this round")

        positions = secure_shuffle(list(range(GRID_SIZE)))[:mines]
        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await deduct_balance(user_id, stake, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'
                await conn.execute("""
                    INSERT INTO battle_royale_mines_players (round_id, user_id, stake, mines, positions)
                    VALUES ($1,$2,$3,$4,$5)
                """, room.round_id, user_id, stake, mines, positions)

        room.players[user_id] = {
            'username': username, 'stake': stake, 'mines': mines, 'positions': positions,
            'revealed': 0, 'mult': 1.0, 'busted': False, 'cashed_out': False, 'payout': None, 'bonus_won': 0,
        }

        if room.task is None:
            room.task = asyncio.create_task(room.run_revealing())

        await room.broadcast({'type': 'player_joined', 'round_id': room.round_id, 'round': room.snapshot()})
        result = {"success": True, "round_id": room.round_id, "round": room.snapshot()}

    return convert_decimals(result)


@router.post("/reveal")
async def reveal(body: RoundIdBody, request: Request):
    user_id = await require_auth(request)
    async with _battle_royale_mines_registry_lock:
        room = _battle_royale_mines_rooms.get(body.round_id)
    if not room:
        raise HTTPException(404, "Round not found or already closed")

    async with room.lock:
        if room.status != 'revealing':
            raise HTTPException(400, "This round has already ended")
        p = room.players.get(user_id)
        if not p:
            raise HTTPException(400, "You're not in this round")
        if p['busted'] or p['cashed_out']:
            raise HTTPException(400, "You can no longer act this round")
        if p['revealed'] >= GRID_SIZE - p['mines']:
            raise HTTPException(400, "You've already cleared every safe tile")

        tile = p['revealed']   # deterministic reveal order over the pre-shuffled positions list
        hit_mine = tile in p['positions']

        if hit_mine:
            p['busted'] = True
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE battle_royale_mines_players SET busted=TRUE, payout=0 WHERE round_id=$1 AND user_id=$2",
                        room.round_id, user_id
                    )
                    await conn.execute(
                        "UPDATE battle_royale_mines_rounds SET bonus_pot = bonus_pot + $1 WHERE id=$2",
                        p['stake'], room.round_id
                    )
            room.bonus_pot = round(room.bonus_pot + p['stake'], 2)
            p['payout'] = 0
        else:
            p['revealed'] += 1
            p['mult'] = mines_multiplier(GRID_SIZE, p['mines'], p['revealed'])
            async with (await get_db()).acquire() as conn:
                await conn.execute(
                    "UPDATE battle_royale_mines_players SET revealed=$1, mult=$2 WHERE round_id=$3 AND user_id=$4",
                    p['revealed'], p['mult'], room.round_id, user_id
                )

        await room.broadcast({'type': 'player_revealed', 'round_id': room.round_id, 'user_id': user_id,
                               'hit_mine': hit_mine, 'revealed': p['revealed'], 'mult': p['mult'], 'busted': p['busted']})
        result = {"success": True, "hit_mine": hit_mine, "revealed": p['revealed'], "mult": p['mult'], "busted": p['busted']}

    return convert_decimals(result)


@router.post("/cashout")
async def cashout(body: RoundIdBody, request: Request):
    user_id = await require_auth(request)
    async with _battle_royale_mines_registry_lock:
        room = _battle_royale_mines_rooms.get(body.round_id)
    if not room:
        raise HTTPException(404, "Round not found or already closed")

    async with room.lock:
        if room.status != 'revealing':
            raise HTTPException(400, "This round has already ended")
        p = room.players.get(user_id)
        if not p:
            raise HTTPException(400, "You're not in this round")
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
                    "UPDATE battle_royale_mines_players SET cashed_out=TRUE, payout=$1 WHERE round_id=$2 AND user_id=$3",
                    win, room.round_id, user_id
                )
                await log_game(conn, user_id, 'battle_royale_mines', p['stake'], win, {
                    'round': room.round_id, 'revealed': p['revealed'], 'mult': p['mult'], 'auto_cashout': False,
                })

        await room.broadcast({'type': 'player_cashed_out', 'round_id': room.round_id, 'user_id': user_id, 'win': win})
        result = {"success": True, "win": win}

    return convert_decimals(result)


@router.get("/rounds")
async def list_rounds():
    async with _battle_royale_mines_registry_lock:
        rooms = list(_battle_royale_mines_rooms.values())
    result = {"rounds": [
        {
            'round_id': r.round_id, 'status': r.status,
            'player_count': len(r.players), 'deadline': r.deadline, 'bonus_pot': r.bonus_pot,
        }
        for r in rooms if r.status == 'revealing'
    ]}
    return convert_decimals(result)


@router.get("/rounds/{round_id}")
async def get_round(round_id: int):
    async with _battle_royale_mines_registry_lock:
        room = _battle_royale_mines_rooms.get(round_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        rnd = await conn.fetchrow("SELECT * FROM battle_royale_mines_rounds WHERE id=$1", round_id)
        if not rnd:
            raise HTTPException(404, "Round not found")
        players = await conn.fetch("""
            SELECT p.*, u.username FROM battle_royale_mines_players p
            JOIN users u ON u.user_id = p.user_id
            WHERE p.round_id=$1 ORDER BY p.created_at
        """, round_id)
    result = {
        'round_id': round_id, 'status': rnd['status'], 'bonus_pot': rnd['bonus_pot'],
        'players': [
            {
                'user_id': p['user_id'], 'username': p['username'], 'stake': p['stake'], 'mines': p['mines'],
                'revealed': p['revealed'], 'mult': p['mult'], 'busted': p['busted'],
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
async def battle_royale_mines_ws(websocket: WebSocket, round_id: int):
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

    async with _battle_royale_mines_registry_lock:
        room = _battle_royale_mines_rooms.get(round_id)
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
