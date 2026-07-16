# ============================================================
# routes/live_roulette.py
# CS2CaseBot | Live Roulette
#
# Shared-wheel variant of the existing solo Roulette (routes/games_medium.py).
# Players place any of the 11 existing bet types during a betting countdown;
# at countdown end ONE shared secure_choice(ROULETTE_NUMBERS) result is drawn
# and every player's bets are evaluated against it via the exact same
# evaluate_roulette_bet() used by the solo game. Closest fit to Crash's own
# "betting countdown -> single synchronized resolve" pattern.
# ============================================================

import asyncio
import time
from typing import Dict, Set, Optional, List, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from pydantic import BaseModel

import shared
from shared import (
    logger, get_db, require_auth, ensure_user_exists,
    deduct_balance, broadcast_to_set, convert_decimals,
    credit_win, require_game_enabled, clamp_bet as shared_clamp_bet,
    HOUSE_EDGE,
)
from routes.games_medium import (
    ROULETTE_NUMBERS, ROULETTE_RED, roulette_spin, evaluate_roulette_bet, log_game,
)

router = APIRouter(prefix="/api/games/live-roulette", tags=["live-roulette"])

MIN_BET     = 50
MAX_BET     = 750_000
MAX_BETS_PER_PLAYER = 10   # matches solo Roulette's per-spin cap
BETTING_SECS = 15


def clamp_bet(amount: float) -> float:
    return shared_clamp_bet(amount, MIN_BET, MAX_BET)


# ============================================================
# TABLE SETUP
# ============================================================

async def init_live_roulette_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS live_roulette_rounds (
                id            SERIAL PRIMARY KEY,
                status        TEXT DEFAULT 'betting'
                              CHECK (status IN ('betting','spinning','settled','cancelled')),
                result_number INTEGER,
                created_at    TIMESTAMP DEFAULT NOW(),
                resolved_at   TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS live_roulette_bets (
                id            SERIAL PRIMARY KEY,
                round_id      INTEGER NOT NULL REFERENCES live_roulette_rounds(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
                bet_type      TEXT NOT NULL,
                bet_value     TEXT NOT NULL,
                amount        DECIMAL(15,2) NOT NULL,
                payout        DECIMAL(15,2),
                created_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_live_roulette_bets_round ON live_roulette_bets(round_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_live_roulette_bets_user ON live_roulette_bets(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_live_roulette_rounds_status ON live_roulette_rounds(status)")
    logger.info("✅ Live Roulette tables ready")


async def recover_stale_live_roulette_rounds():
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id FROM live_roulette_rounds WHERE status IN ('betting','spinning') FOR UPDATE"
            )
            for row in stale:
                round_id = row['id']
                bets = await conn.fetch(
                    "SELECT user_id, amount FROM live_roulette_bets WHERE round_id=$1 AND payout IS NULL",
                    round_id
                )
                for b in bets:
                    await shared.add_balance(b['user_id'], float(b['amount']), conn)
                await conn.execute(
                    "UPDATE live_roulette_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                    round_id
                )
            if stale:
                logger.info(f"🎡 Recovered {len(stale)} stale Live Roulette round(s), refunded all bets")


# ============================================================
# ROOM
# ============================================================

class LiveRouletteRoom:
    def __init__(self, round_id: int):
        self.round_id = round_id
        self.status = 'betting'   # betting | spinning | settled | cancelled
        # user_id -> {username, bets: [{bet_type, bet_value, amount}]}
        self.players: Dict[int, Dict] = {}
        self.ws_set: Set[WebSocket] = set()
        self.ws_map: Dict[int, WebSocket] = {}
        self.task: Optional[asyncio.Task] = None
        self.betting_deadline: Optional[float] = None
        self.lock = asyncio.Lock()
        self.created_at = time.time()

    async def broadcast(self, msg: dict):
        dead = await broadcast_to_set(self.ws_set, convert_decimals(msg))
        self.ws_set -= dead

    def snapshot(self) -> dict:
        return {
            'round_id': self.round_id,
            'status': self.status,
            'betting_deadline': self.betting_deadline,
            'players': [
                {'user_id': uid, 'username': p['username'],
                 'total_bet': sum(b['amount'] for b in p['bets']), 'bet_count': len(p['bets'])}
                for uid, p in self.players.items()
            ],
        }

    async def run_betting(self):
        for sec in range(BETTING_SECS, 0, -1):
            await asyncio.sleep(1)
            async with self.lock:
                if self.status != 'betting':
                    return
            await self.broadcast({'type': 'betting_tick', 'round_id': self.round_id, 'seconds': sec - 1})
        await self.resolve()

    async def resolve(self):
        async with self.lock:
            if self.status != 'betting':
                return   # guards double-invocation
            self.status = 'spinning'
            await self.broadcast({'type': 'spinning', 'round_id': self.round_id})

            if not self.players:
                pool = await get_db()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE live_roulette_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )
                self.status = 'cancelled'
                async with _live_roulette_registry_lock:
                    _live_roulette_rooms.pop(self.round_id, None)
                return

            result = roulette_spin()
            results = []
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for uid, p in self.players.items():
                        player_total_win = 0.0
                        bet_breakdown = []
                        for b in p['bets']:
                            mult = evaluate_roulette_bet(b['bet_type'], b['bet_value'], result)
                            win = shared.apply_house(b['amount'] * mult, HOUSE_EDGE) if mult else 0
                            player_total_win += win
                            bet_breakdown.append({**b, 'mult': mult, 'win': win, 'won': mult > 0})
                            await conn.execute("""
                                UPDATE live_roulette_bets SET payout=$1
                                WHERE round_id=$2 AND user_id=$3 AND bet_type=$4 AND bet_value=$5 AND payout IS NULL
                            """, win, self.round_id, uid, b['bet_type'], str(b['bet_value']))
                        if player_total_win:
                            player_total_win = await credit_win(uid, player_total_win, conn)
                        total_bet = sum(b['amount'] for b in p['bets'])
                        await log_game(conn, uid, 'live_roulette', total_bet, player_total_win, {
                            'round': self.round_id, 'result': result, 'bets': bet_breakdown,
                        })
                        results.append({
                            'user_id': uid, 'username': p['username'],
                            'total_bet': total_bet, 'total_win': player_total_win,
                            'bets': bet_breakdown,
                        })
                    await conn.execute("""
                        UPDATE live_roulette_rounds SET status='settled', result_number=$1, resolved_at=NOW()
                        WHERE id=$2
                    """, result, self.round_id)

            self.status = 'settled'
            is_red = result in ROULETTE_RED and result != 0
            is_black = result != 0 and not is_red
            await self.broadcast({
                'type': 'result', 'round_id': self.round_id, 'number': result,
                'color': 'red' if is_red else ('black' if is_black else 'green'),
                'players': results,
            })
            async with _live_roulette_registry_lock:
                _live_roulette_rooms.pop(self.round_id, None)


# ============================================================
# REGISTRY
# ============================================================

_live_roulette_rooms: Dict[int, LiveRouletteRoom] = {}
_live_roulette_registry_lock = asyncio.Lock()


async def _get_or_create_open_room() -> LiveRouletteRoom:
    async with _live_roulette_registry_lock:
        for room in _live_roulette_rooms.values():
            if room.status == 'betting':
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO live_roulette_rounds (status) VALUES ('betting') RETURNING id"
            )
        room = LiveRouletteRoom(row['id'])
        room.betting_deadline = time.time() + BETTING_SECS
        _live_roulette_rooms[room.round_id] = room
        return room


async def create_private_room(participant_user_ids: list, amount: float) -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py). Scoped to exactly 2 players. Live Roulette's
    public /bet conflates seating with placing a bet in one action
    (unlike the duel games, there's no separate "join" step) -- rather
    than building new challenge-creation UI to let the sender pick a
    bet type, each player is auto-placed on a straight "red" bet for
    the flat stake, so accepting the challenge stakes real money
    immediately like every other hook. Players can still place
    additional bets themselves during the round's normal countdown."""
    if len(participant_user_ids) != 2:
        raise HTTPException(400, "Live Roulette friend challenges need exactly 2 players")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO live_roulette_rounds (status) VALUES ('betting') RETURNING id"
            )
            round_id = row['id']

            players: Dict[int, Dict] = {}
            for uid in participant_user_ids:
                if not await deduct_balance(uid, amount, conn):
                    raise HTTPException(400, f"Player {uid} has insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                username = user_row['username'] if user_row else f'Player {uid}'
                await conn.execute("""
                    INSERT INTO live_roulette_bets (round_id, user_id, bet_type, bet_value, amount)
                    VALUES ($1,$2,'red','red',$3)
                """, round_id, uid, amount)
                players[uid] = {
                    'username': username,
                    'bets': [{'bet_type': 'red', 'bet_value': 'red', 'amount': amount}],
                }

    room = LiveRouletteRoom(round_id)
    room.players = players
    room.betting_deadline = time.time() + BETTING_SECS
    async with _live_roulette_registry_lock:
        _live_roulette_rooms[round_id] = room
    room.task = asyncio.create_task(room.run_betting())
    await room.broadcast({'type': 'round_start', 'round_id': round_id, 'round': room.snapshot()})
    return round_id


# ============================================================
# REST ROUTES
# ============================================================

class LiveBetRequest(BaseModel):
    bet_type: str
    bet_value: Any
    amount: float
    round_id: Optional[int] = None


@router.post("/bet")
async def place_bet(req: LiveBetRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("live-roulette")
    amount = clamp_bet(req.amount)
    await ensure_user_exists(user_id)

    if req.round_id is not None:
        async with _live_roulette_registry_lock:
            room = _live_roulette_rooms.get(req.round_id)
        if not room:
            raise HTTPException(404, "Round not found or already closed")
    else:
        room = await _get_or_create_open_room()

    async with room.lock:
        if room.status != 'betting':
            raise HTTPException(400, "This round is no longer accepting bets")
        existing = room.players.get(user_id, {'bets': []})
        if len(existing['bets']) >= MAX_BETS_PER_PLAYER:
            raise HTTPException(400, f"Maximum {MAX_BETS_PER_PLAYER} simultaneous bets per round")

        # Validate the bet type/value eagerly against a dummy result so a bad
        # bet is rejected at placement time, not silently swallowed at resolve.
        evaluate_roulette_bet(req.bet_type, req.bet_value, 0)

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await deduct_balance(user_id, amount, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'
                await conn.execute("""
                    INSERT INTO live_roulette_bets (round_id, user_id, bet_type, bet_value, amount)
                    VALUES ($1,$2,$3,$4,$5)
                """, room.round_id, user_id, req.bet_type, str(req.bet_value), amount)

        if user_id not in room.players:
            room.players[user_id] = {'username': username, 'bets': []}
        room.players[user_id]['bets'].append({
            'bet_type': req.bet_type, 'bet_value': req.bet_value, 'amount': amount,
        })

        if room.task is None:
            room.task = asyncio.create_task(room.run_betting())

        await room.broadcast({'type': 'bet_placed', 'round_id': room.round_id, 'round': room.snapshot()})
        result = {"success": True, "round_id": room.round_id, "round": room.snapshot()}

    return convert_decimals(result)


@router.get("/rounds")
async def list_rounds():
    async with _live_roulette_registry_lock:
        rooms = list(_live_roulette_rooms.values())
    result = {"rounds": [
        {
            'round_id': r.round_id, 'status': r.status,
            'player_count': len(r.players), 'betting_deadline': r.betting_deadline,
        }
        for r in rooms if r.status == 'betting'
    ]}
    return convert_decimals(result)


@router.get("/rounds/{round_id}")
async def get_round(round_id: int):
    async with _live_roulette_registry_lock:
        room = _live_roulette_rooms.get(round_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        rnd = await conn.fetchrow("SELECT * FROM live_roulette_rounds WHERE id=$1", round_id)
        if not rnd:
            raise HTTPException(404, "Round not found")
        bets = await conn.fetch("""
            SELECT b.*, u.username FROM live_roulette_bets b
            JOIN users u ON u.user_id = b.user_id
            WHERE b.round_id=$1 ORDER BY b.created_at
        """, round_id)
    result = {
        'round_id': round_id, 'status': rnd['status'], 'result_number': rnd['result_number'],
        'bets': [
            {
                'user_id': b['user_id'], 'username': b['username'], 'bet_type': b['bet_type'],
                'bet_value': b['bet_value'], 'amount': b['amount'], 'payout': b['payout'],
            }
            for b in bets
        ],
    }
    return convert_decimals(result)


@router.get("/history")
async def spin_history(limit: int = 20):
    limit = max(1, min(limit, 50))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, result_number, resolved_at FROM live_roulette_rounds
            WHERE status='settled' ORDER BY resolved_at DESC LIMIT $1
        """, limit)
    result = {"history": [
        {
            'round_id': r['id'], 'result_number': r['result_number'],
            'resolved_at': r['resolved_at'].isoformat() if r['resolved_at'] else None,
        }
        for r in rows
    ]}
    return convert_decimals(result)


# ============================================================
# WEBSOCKET
# ============================================================

@router.websocket("/ws/{round_id}")
async def live_roulette_ws(websocket: WebSocket, round_id: int):
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

    async with _live_roulette_registry_lock:
        room = _live_roulette_rooms.get(round_id)
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
