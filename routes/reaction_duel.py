# ============================================================
# routes/reaction_duel.py
# CS2CaseBot | Reaction Duel (1v1, cash-staked)
#
# Two players each stake equal cash. Once the 2nd player joins,
# the room "arms" -- after a randomized 2-5s delay (so neither
# player can anticipate it), the server broadcasts a 'go' signal
# over the WS. Each player submits their own client-measured
# reaction time; a submission made before 'go' fired is a false
# start (automatic loss). Lower ms wins both stakes, no house
# rake. Reuses the exact anti-cheat floor from the solo Reaction
# Time game (routes/ticket_games.py:80, ms<100 -> ms=9999) and
# the DuelRoom scaffolding from routes/dice_duel.py, with an
# added "armed" decision-window phase in between.
# ============================================================

import asyncio
import time
from typing import Dict, Set, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from pydantic import BaseModel

import shared
from shared import (
    logger, get_db, require_auth, ensure_user_exists,
    broadcast_to_set, convert_decimals,
    secure_randint, deduct_balance, add_balance,
    check_rate_limit, RATE_WRITE, log_game,
)

router = APIRouter(prefix="/api/games/reaction-duel", tags=["reaction-duel"])

MAX_PLAYERS       = 2
MIN_STAKE         = 10.0
MAX_STAKE         = 750_000.0
GO_DELAY_MIN_MS   = 2000
GO_DELAY_MAX_MS   = 5000
SUBMIT_WINDOW_SECS = 10   # grace period after 'go' before a non-responder forfeits
IDLE_WAITING_SECS = 600   # abandon a waiting-for-2nd-player duel after 10 min


async def init_reaction_duel_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reaction_duels (
                id           SERIAL PRIMARY KEY,
                status       TEXT DEFAULT 'waiting'
                             CHECK (status IN ('waiting','armed','resolving','completed','cancelled')),
                stake        DECIMAL(15,2) NOT NULL CHECK (stake >= 10),
                player1_id   BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                player2_id   BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                ms1          DECIMAL(10,2),
                ms2          DECIMAL(10,2),
                false_start_user_id BIGINT,
                winner_id    BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                created_at   TIMESTAMP DEFAULT NOW(),
                completed_at TIMESTAMP
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reaction_duels_status ON reaction_duels(status)")
    logger.info("✅ Reaction Duel table ready")


async def recover_stale_reaction_duels():
    """Startup crash-recovery -- 'waiting' included for the same reason as
    dice_duel's equivalent: the in-memory DuelRoom that leave_duel() depends
    on for a manual backout is gone after any restart."""
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id, stake, player1_id, player2_id FROM reaction_duels WHERE status IN ('waiting','armed','resolving') FOR UPDATE"
            )
            for row in stale:
                stake = float(row["stake"])
                if row["player1_id"]:
                    await add_balance(row["player1_id"], stake, conn)
                if row["player2_id"]:
                    await add_balance(row["player2_id"], stake, conn)
                await conn.execute(
                    "UPDATE reaction_duels SET status='cancelled', completed_at=NOW() WHERE id=$1", row["id"]
                )
            if stale:
                logger.info(f"⚡ Recovered {len(stale)} stale reaction duel(s), refunded both stakes")


async def expire_stale_reaction_duels_loop():
    """Runtime safety net (no restart needed), same shape as dice_duel's
    equivalent loop."""
    while True:
        await asyncio.sleep(60)
        try:
            pool = await get_db()
            async with pool.acquire() as conn:
                stale = await conn.fetch(
                    "SELECT id FROM reaction_duels WHERE status='waiting' "
                    "AND created_at <= NOW() - make_interval(secs => $1)",
                    IDLE_WAITING_SECS
                )
                count = 0
                for row in stale:
                    duel_id = row['id']
                    async with conn.transaction():
                        claimed = await conn.fetchrow(
                            "UPDATE reaction_duels SET status='cancelled', completed_at=NOW() "
                            "WHERE id=$1 AND status='waiting' RETURNING stake, player1_id, player2_id",
                            duel_id
                        )
                        if not claimed:
                            continue
                        stake = float(claimed["stake"])
                        if claimed["player1_id"]:
                            await add_balance(claimed["player1_id"], stake, conn)
                        if claimed["player2_id"]:
                            await add_balance(claimed["player2_id"], stake, conn)
                        count += 1
                    async with _reaction_registry_lock:
                        room = _reaction_rooms.pop(duel_id, None)
                    if room:
                        await room.broadcast({'type': 'cancelled', 'duel_id': duel_id, 'reason': 'timed_out'})
                if count:
                    logger.info(f"⚡ Expired {count} idle reaction duel(s), refunded stakes")
        except Exception as e:
            logger.warning(f"expire_stale_reaction_duels_loop failed: {e}")


class DuelRoom:
    def __init__(self, duel_id: int, stake: float):
        self.duel_id = duel_id
        self.stake = stake
        self.status = 'waiting'   # waiting | armed | resolving | completed | cancelled
        self.players: Dict[int, Dict] = {}
        self.submissions: Dict[int, Dict] = {}   # user_id -> {ms, false_start}
        self.go_at: Optional[float] = None
        self.go_fired = False
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
            'duel_id': self.duel_id,
            'stake': self.stake,
            'status': self.status,
            'go_at': self.go_at,
            'go_fired': self.go_fired,
            'players': [{'user_id': uid, **p} for uid, p in self.players.items()],
        }

    async def run_armed_phase(self):
        delay = secure_randint(GO_DELAY_MIN_MS, GO_DELAY_MAX_MS) / 1000.0
        self.go_at = time.time() + delay
        await asyncio.sleep(delay)
        self.go_fired = True
        await self.broadcast({'type': 'go', 'duel_id': self.duel_id, 'go_at': self.go_at})
        await asyncio.sleep(SUBMIT_WINDOW_SECS)
        await self.resolve()

    async def resolve(self):
        async with self.lock:
            if self.status != 'armed':
                return   # already resolved, or a rearm reset this in the meantime
            self.status = 'resolving'
            await self.broadcast({'type': 'resolving', 'duel_id': self.duel_id})

            uids = list(self.players.keys())
            if len(uids) < MAX_PLAYERS:
                pool = await get_db()
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        for uid in uids:
                            await add_balance(uid, self.stake, conn)
                        await conn.execute(
                            "UPDATE reaction_duels SET status='cancelled', completed_at=NOW() WHERE id=$1",
                            self.duel_id
                        )
                self.status = 'cancelled'
                await self.broadcast({'type': 'cancelled', 'duel_id': self.duel_id, 'reason': 'not_enough_players'})
                async with _reaction_registry_lock:
                    _reaction_rooms.pop(self.duel_id, None)
                return

            p1, p2 = uids[0], uids[1]
            sub1, sub2 = self.submissions.get(p1), self.submissions.get(p2)

            if sub1 is None and sub2 is None:
                # Neither player reacted within the submit window -- cancel + refund.
                pool = await get_db()
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await add_balance(p1, self.stake, conn)
                        await add_balance(p2, self.stake, conn)
                        await conn.execute(
                            "UPDATE reaction_duels SET status='cancelled', completed_at=NOW() WHERE id=$1",
                            self.duel_id
                        )
                self.status = 'cancelled'
                await self.broadcast({'type': 'cancelled', 'duel_id': self.duel_id, 'reason': 'no_response'})
                async with _reaction_registry_lock:
                    _reaction_rooms.pop(self.duel_id, None)
                return

            def is_forfeit(sub):
                return sub is None or sub.get('false_start')

            f1, f2 = is_forfeit(sub1), is_forfeit(sub2)
            if f1 and f2:
                # Both false-started (or both somehow forfeited) -- reroll
                # rather than cancel, same "reroll on a tie" convention as
                # Dice/Weapon Duel; stakes stay locked in.
                self._rearm()
                return

            if f1:
                winner_id = p2
            elif f2:
                winner_id = p1
            elif sub1['ms'] == sub2['ms']:
                self._rearm()
                return
            else:
                winner_id = p1 if sub1['ms'] < sub2['ms'] else p2

            ms1 = sub1['ms'] if sub1 else None
            ms2 = sub2['ms'] if sub2 else None
            false_start_uid = p1 if f1 and sub1 and sub1.get('false_start') else (p2 if f2 and sub2 and sub2.get('false_start') else None)

            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await add_balance(winner_id, self.stake * 2, conn)
                    await conn.execute("""
                        UPDATE reaction_duels
                        SET status='completed', ms1=$1, ms2=$2, false_start_user_id=$3, winner_id=$4, completed_at=NOW()
                        WHERE id=$5
                    """, ms1, ms2, false_start_uid, winner_id, self.duel_id)
                    for uid in (p1, p2):
                        await log_game(conn, uid, 'reaction_duel', self.stake,
                                       self.stake * 2 if uid == winner_id else 0.0,
                                       {'duel_id': self.duel_id, 'ms1': ms1, 'ms2': ms2})

            self.status = 'completed'
            await self.broadcast({
                'type': 'duel_result',
                'duel_id': self.duel_id,
                'winner_id': winner_id,
                'ms1': ms1, 'ms2': ms2,
                'false_start_user_id': false_start_uid,
                'player1_id': p1, 'player2_id': p2,
                'total_value': round(self.stake * 2, 2),
            })
            async with _reaction_registry_lock:
                _reaction_rooms.pop(self.duel_id, None)

    def _rearm(self):
        """Reset for a reroll -- caller must already hold self.lock."""
        self.submissions = {}
        self.go_at = None
        self.go_fired = False
        self.status = 'armed'
        self.task = asyncio.create_task(self.run_armed_phase())
        asyncio.create_task(self.broadcast({'type': 'rearm', 'duel_id': self.duel_id}))


_reaction_rooms: Dict[int, DuelRoom] = {}
_reaction_registry_lock = asyncio.Lock()


async def create_private_room(participant_user_ids: list, stake: float) -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py) -- see routes/dice_duel.py's create_private_room
    for the full rationale. Differs from that template only in that this
    game's post-both-players phase is 'armed' (with its own randomized
    go-delay), not 'locking'."""
    if len(participant_user_ids) != MAX_PLAYERS:
        raise HTTPException(400, f"Reaction Duel needs exactly {MAX_PLAYERS} players")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                INSERT INTO reaction_duels (status, stake, player1_id, player2_id)
                VALUES ('armed', $1, $2, $3) RETURNING id
            """, stake, participant_user_ids[0], participant_user_ids[1])
            duel_id = row['id']

            players: Dict[int, Dict] = {}
            for uid in participant_user_ids:
                if not await deduct_balance(uid, stake, conn):
                    raise HTTPException(400, f"Player {uid} has insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                players[uid] = {'username': user_row['username'] if user_row else f'Player {uid}'}

    room = DuelRoom(duel_id, stake)
    room.status = 'armed'
    room.players = players
    async with _reaction_registry_lock:
        _reaction_rooms[duel_id] = room
    room.task = asyncio.create_task(room.run_armed_phase())
    await room.broadcast({'type': 'armed', 'duel_id': duel_id})
    return duel_id


async def _get_or_create_open_room(stake: float) -> DuelRoom:
    async with _reaction_registry_lock:
        for room in _reaction_rooms.values():
            if room.status == 'waiting' and room.stake == stake and len(room.players) < MAX_PLAYERS:
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO reaction_duels (status, stake) VALUES ('waiting', $1) RETURNING id",
                stake
            )
        room = DuelRoom(row['id'], stake)
        _reaction_rooms[room.duel_id] = room
        return room


class JoinRequest(BaseModel):
    stake: float
    duel_id: Optional[int] = None


class LeaveRequest(BaseModel):
    duel_id: int


class ReactRequest(BaseModel):
    duel_id: int
    ms: float


@router.post("/join")
async def join_duel(req: JoinRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)
    await ensure_user_exists(user_id)

    stake = round(float(req.stake), 2)
    if stake < MIN_STAKE or stake > MAX_STAKE:
        raise HTTPException(400, f"Stake must be between ${MIN_STAKE:.2f} and ${MAX_STAKE:,.2f}")

    if req.duel_id is not None:
        async with _reaction_registry_lock:
            room = _reaction_rooms.get(req.duel_id)
        if not room:
            raise HTTPException(404, "Duel not found or already closed")
        if room.stake != stake:
            raise HTTPException(400, f"This duel's stake is ${room.stake:.2f}")
    else:
        room = await _get_or_create_open_room(stake)

    async with room.lock:
        if room.status != 'waiting':
            raise HTTPException(400, "This duel is no longer accepting entries")
        if len(room.players) >= MAX_PLAYERS:
            raise HTTPException(400, "This duel is full")
        if user_id in room.players:
            raise HTTPException(400, "You're already in this duel")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await deduct_balance(user_id, stake, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'

                col = 'player1_id' if room.players == {} else 'player2_id'
                await conn.execute(f"UPDATE reaction_duels SET {col}=$1 WHERE id=$2", user_id, room.duel_id)

        room.players[user_id] = {'username': username}

        if len(room.players) >= MAX_PLAYERS:
            room.status = 'armed'
            room.task = asyncio.create_task(room.run_armed_phase())
            await room.broadcast({'type': 'armed', 'duel_id': room.duel_id})

        await room.broadcast({'type': 'player_joined', 'duel_id': room.duel_id, 'duel': room.snapshot()})
        result = {"success": True, "duel_id": room.duel_id, "duel": room.snapshot()}

    return convert_decimals(result)


@router.post("/leave")
async def leave_duel(req: LeaveRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)

    async with _reaction_registry_lock:
        room = _reaction_rooms.get(req.duel_id)
    if not room:
        raise HTTPException(404, "Duel not found or already closed")

    async with room.lock:
        if room.status != 'waiting':
            raise HTTPException(400, "This duel has already started -- your stake is locked in")
        if user_id not in room.players:
            raise HTTPException(400, "You're not in this duel")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await add_balance(user_id, room.stake, conn)

        del room.players[user_id]

        if len(room.players) == 0:
            room.status = 'cancelled'
            pool = await get_db()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE reaction_duels SET status='cancelled', completed_at=NOW() WHERE id=$1",
                    room.duel_id
                )
            async with _reaction_registry_lock:
                _reaction_rooms.pop(room.duel_id, None)
        else:
            await room.broadcast({'type': 'player_left', 'duel_id': room.duel_id, 'duel': room.snapshot()})

    return {"success": True}


@router.post("/react")
async def react(req: ReactRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)

    async with _reaction_registry_lock:
        room = _reaction_rooms.get(req.duel_id)
    if not room:
        raise HTTPException(404, "Duel not found or already closed")

    both_in = False
    async with room.lock:
        if room.status != 'armed':
            raise HTTPException(400, "This duel isn't accepting reactions right now")
        if user_id not in room.players:
            raise HTTPException(403, "You're not a participant in this duel")
        if user_id in room.submissions:
            raise HTTPException(400, "You've already reacted")

        ms = float(req.ms)
        if ms < 100:   # anti-cheat floor -- impossible to react faster (routes/ticket_games.py:80)
            ms = 9999
        false_start = not room.go_fired
        room.submissions[user_id] = {'ms': ms, 'false_start': false_start}
        both_in = len(room.submissions) >= MAX_PLAYERS

    if both_in:
        await room.resolve()

    return {"success": True}


@router.get("/duels")
async def list_duels():
    async with _reaction_registry_lock:
        rooms = list(_reaction_rooms.values())
    result = {"duels": [
        {
            'duel_id': r.duel_id,
            'stake': r.stake,
            'status': r.status,
            'player_count': len(r.players),
        }
        for r in rooms if r.status == 'waiting'
    ]}
    return convert_decimals(result)


@router.get("/duels/{duel_id}")
async def get_duel(duel_id: int):
    async with _reaction_registry_lock:
        room = _reaction_rooms.get(duel_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        duel = await conn.fetchrow("""
            SELECT d.*, u1.username AS p1_username, u2.username AS p2_username
            FROM reaction_duels d
            LEFT JOIN users u1 ON u1.user_id = d.player1_id
            LEFT JOIN users u2 ON u2.user_id = d.player2_id
            WHERE d.id=$1
        """, duel_id)
        if not duel:
            raise HTTPException(404, "Duel not found")

    players = []
    if duel['player1_id']:
        players.append({'user_id': duel['player1_id'], 'username': duel['p1_username']})
    if duel['player2_id']:
        players.append({'user_id': duel['player2_id'], 'username': duel['p2_username']})

    result = {
        'duel_id': duel_id,
        'stake': duel['stake'],
        'status': duel['status'],
        'winner_id': duel['winner_id'],
        'ms1': duel['ms1'], 'ms2': duel['ms2'],
        'go_at': None, 'go_fired': False,
        'players': players,
    }
    return convert_decimals(result)


@router.get("/history")
async def duel_history(limit: int = 20):
    limit = max(1, min(limit, 50))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT d.id, d.stake, d.winner_id, d.ms1, d.ms2, d.completed_at, u.username AS winner_username
            FROM reaction_duels d
            LEFT JOIN users u ON u.user_id = d.winner_id
            WHERE d.status='completed'
            ORDER BY d.completed_at DESC
            LIMIT $1
        """, limit)
    result = {"history": [
        {
            'duel_id': r['id'], 'stake': r['stake'], 'winner_id': r['winner_id'],
            'winner_username': r['winner_username'], 'ms1': r['ms1'], 'ms2': r['ms2'],
            'completed_at': r['completed_at'].isoformat() if r['completed_at'] else None,
        }
        for r in rows
    ]}
    return convert_decimals(result)


@router.websocket("/ws/{duel_id}")
async def duel_ws(websocket: WebSocket, duel_id: int):
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

    async with _reaction_registry_lock:
        room = _reaction_rooms.get(duel_id)
    if not room:
        try:
            await websocket.send_json({'type': 'no_room', 'duel_id': duel_id})
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
