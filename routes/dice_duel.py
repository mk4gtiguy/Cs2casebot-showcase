# ============================================================
# routes/dice_duel.py
# CS2CaseBot | Dice Duel (1v1, cash-staked)
#
# Two players each stake equal cash. Once the 2nd player joins, a
# short fixed "locking in" delay plays for suspense, then both
# roll 2-12 (secure_randint) -- reroll both on a tie -- higher
# roll wins both stakes. No house rake, same "duel family"
# convention as the item-wager duels. Direct simplification of
# routes/item_wager_duel.py's DuelRoom -- same per-room lock/
# registry/WS-auth discipline -- but cash instead of an item, so
# there's no separate entries table (cash isn't a physical object
# that can be "stuck" mid-flight the way an item can).
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

router = APIRouter(prefix="/api/games/dice-duel", tags=["dice-duel"])

MAX_PLAYERS  = 2
LOCK_IN_SECS = 3
MIN_STAKE    = 10.0
MAX_STAKE    = 750_000.0
IDLE_WAITING_SECS = 600   # abandon a waiting-for-2nd-player duel after 10 min


async def init_dice_duel_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dice_duels (
                id           SERIAL PRIMARY KEY,
                status       TEXT DEFAULT 'waiting'
                             CHECK (status IN ('waiting','locking','rolling','completed','cancelled')),
                stake        DECIMAL(15,2) NOT NULL CHECK (stake >= 10),
                player1_id   BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                player2_id   BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                roll1        INTEGER,
                roll2        INTEGER,
                winner_id    BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                created_at   TIMESTAMP DEFAULT NOW(),
                completed_at TIMESTAMP
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_dice_duels_status ON dice_duels(status)")
    logger.info("✅ Dice Duel table ready")


async def recover_stale_dice_duels():
    """Startup crash-recovery: any duel left mid-flight (waiting/locking/
    rolling) from a previous process death refunds the stake to whichever
    player(s) already paid in -- cash equivalent of item_jackpot's item
    refund sweep. 'waiting' is included because a duel's in-memory DuelRoom
    (the only thing leave_duel()'s manual backout depends on) is gone after
    any restart, so a duel stuck waiting for a 2nd player is unrecoverable
    by any other means too."""
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id, stake, player1_id, player2_id FROM dice_duels WHERE status IN ('waiting','locking','rolling') FOR UPDATE"
            )
            for row in stale:
                stake = float(row["stake"])
                if row["player1_id"]:
                    await add_balance(row["player1_id"], stake, conn)
                if row["player2_id"]:
                    await add_balance(row["player2_id"], stake, conn)
                await conn.execute(
                    "UPDATE dice_duels SET status='cancelled', completed_at=NOW() WHERE id=$1", row["id"]
                )
            if stale:
                logger.info(f"🎲 Recovered {len(stale)} stale dice duel(s), refunded both stakes")


async def expire_stale_dice_duels_loop():
    """Runtime safety net (no restart needed): a duel stuck 'waiting' for a
    2nd player past IDLE_WAITING_SECS is abandoned -- refund whoever already
    paid in and cancel it. Atomic status-guarded claim so this can never
    race a legitimate 2nd player joining right at the boundary."""
    while True:
        await asyncio.sleep(60)
        try:
            pool = await get_db()
            async with pool.acquire() as conn:
                stale = await conn.fetch(
                    "SELECT id FROM dice_duels WHERE status='waiting' "
                    "AND created_at <= NOW() - make_interval(secs => $1)",
                    IDLE_WAITING_SECS
                )
                count = 0
                for row in stale:
                    duel_id = row['id']
                    async with conn.transaction():
                        claimed = await conn.fetchrow(
                            "UPDATE dice_duels SET status='cancelled', completed_at=NOW() "
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
                    async with _dice_registry_lock:
                        room = _dice_rooms.pop(duel_id, None)
                    if room:
                        await room.broadcast({'type': 'cancelled', 'duel_id': duel_id, 'reason': 'timed_out'})
                if count:
                    logger.info(f"🎲 Expired {count} idle dice duel(s), refunded stakes")
        except Exception as e:
            logger.warning(f"expire_stale_dice_duels_loop failed: {e}")


class DuelRoom:
    def __init__(self, duel_id: int, stake: float):
        self.duel_id = duel_id
        self.stake = stake
        self.status = 'waiting'   # waiting | locking | rolling | completed | cancelled
        self.players: Dict[int, Dict] = {}   # user_id -> {username}
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
            'duel_id': self.duel_id,
            'stake': self.stake,
            'status': self.status,
            'lock_deadline': self.lock_deadline,
            'players': [{'user_id': uid, **p} for uid, p in self.players.items()],
        }

    async def run_lock_in(self):
        await asyncio.sleep(LOCK_IN_SECS)
        await self.roll()

    async def roll(self):
        async with self.lock:
            if self.status != 'locking':
                return
            self.status = 'rolling'
            await self.broadcast({'type': 'rolling', 'duel_id': self.duel_id})

            uids = list(self.players.keys())
            if len(uids) < MAX_PLAYERS:
                pool = await get_db()
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        for uid in uids:
                            await add_balance(uid, self.stake, conn)
                        await conn.execute(
                            "UPDATE dice_duels SET status='cancelled', completed_at=NOW() WHERE id=$1",
                            self.duel_id
                        )
                self.status = 'cancelled'
                await self.broadcast({'type': 'cancelled', 'duel_id': self.duel_id, 'reason': 'not_enough_players'})
                async with _dice_registry_lock:
                    _dice_rooms.pop(self.duel_id, None)
                return

            p1, p2 = uids[0], uids[1]
            # Reroll both dice on a tie -- the only symmetric outcome,
            # unlike Item Jackpot's weighted-by-value roll which never ties.
            roll1, roll2 = secure_randint(2, 12), secure_randint(2, 12)
            while roll1 == roll2:
                roll1, roll2 = secure_randint(2, 12), secure_randint(2, 12)
            winner_id = p1 if roll1 > roll2 else p2

            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await add_balance(winner_id, self.stake * 2, conn)
                    await conn.execute("""
                        UPDATE dice_duels
                        SET status='completed', roll1=$1, roll2=$2, winner_id=$3, completed_at=NOW()
                        WHERE id=$4
                    """, roll1, roll2, winner_id, self.duel_id)
                    for uid in (p1, p2):
                        await log_game(conn, uid, 'dice_duel', self.stake,
                                       self.stake * 2 if uid == winner_id else 0.0,
                                       {'duel_id': self.duel_id, 'roll1': roll1, 'roll2': roll2})

            self.status = 'completed'
            await self.broadcast({
                'type': 'roll_result',
                'duel_id': self.duel_id,
                'winner_id': winner_id,
                'roll1': roll1, 'roll2': roll2,
                'player1_id': p1, 'player2_id': p2,
                'total_value': round(self.stake * 2, 2),
            })
            async with _dice_registry_lock:
                _dice_rooms.pop(self.duel_id, None)


_dice_rooms: Dict[int, DuelRoom] = {}
_dice_registry_lock = asyncio.Lock()


async def create_private_room(participant_user_ids: list, stake: float) -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py). All participants are already known and have
    already accepted the challenge -- unlike normal /join, this stakes
    everyone atomically in one transaction and starts the room directly
    in 'locking' phase, skipping the one-at-a-time public /join flow
    entirely (so it never touches the open-lobby matching in
    _get_or_create_open_room, and never appears there either). Raises
    HTTPException(400) if any participant can't cover the stake -- the
    whole transaction rolls back, so nobody is left partially charged."""
    if len(participant_user_ids) != MAX_PLAYERS:
        raise HTTPException(400, f"Dice Duel needs exactly {MAX_PLAYERS} players")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                INSERT INTO dice_duels (status, stake, player1_id, player2_id)
                VALUES ('locking', $1, $2, $3) RETURNING id
            """, stake, participant_user_ids[0], participant_user_ids[1])
            duel_id = row['id']

            players: Dict[int, Dict] = {}
            for uid in participant_user_ids:
                if not await deduct_balance(uid, stake, conn):
                    raise HTTPException(400, f"Player {uid} has insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                players[uid] = {'username': user_row['username'] if user_row else f'Player {uid}'}

    room = DuelRoom(duel_id, stake)
    room.status = 'locking'
    room.players = players
    room.lock_deadline = time.time() + LOCK_IN_SECS
    async with _dice_registry_lock:
        _dice_rooms[duel_id] = room
    room.task = asyncio.create_task(room.run_lock_in())
    await room.broadcast({'type': 'locking_start', 'duel_id': duel_id, 'deadline': room.lock_deadline})
    return duel_id


async def _get_or_create_open_room(stake: float) -> DuelRoom:
    async with _dice_registry_lock:
        for room in _dice_rooms.values():
            if room.status == 'waiting' and room.stake == stake and len(room.players) < MAX_PLAYERS:
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO dice_duels (status, stake) VALUES ('waiting', $1) RETURNING id",
                stake
            )
        room = DuelRoom(row['id'], stake)
        _dice_rooms[room.duel_id] = room
        return room


class JoinRequest(BaseModel):
    stake: float
    duel_id: Optional[int] = None


class LeaveRequest(BaseModel):
    duel_id: int


@router.post("/join")
async def join_duel(req: JoinRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)
    await ensure_user_exists(user_id)

    stake = round(float(req.stake), 2)
    if stake < MIN_STAKE or stake > MAX_STAKE:
        raise HTTPException(400, f"Stake must be between ${MIN_STAKE:.2f} and ${MAX_STAKE:,.2f}")

    if req.duel_id is not None:
        async with _dice_registry_lock:
            room = _dice_rooms.get(req.duel_id)
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
                await conn.execute(f"UPDATE dice_duels SET {col}=$1 WHERE id=$2", user_id, room.duel_id)

        room.players[user_id] = {'username': username}

        if len(room.players) >= MAX_PLAYERS:
            room.status = 'locking'
            room.lock_deadline = time.time() + LOCK_IN_SECS
            await room.broadcast({
                'type': 'locking_start', 'duel_id': room.duel_id,
                'deadline': room.lock_deadline,
            })
            room.task = asyncio.create_task(room.run_lock_in())

        await room.broadcast({'type': 'player_joined', 'duel_id': room.duel_id, 'duel': room.snapshot()})
        result = {"success": True, "duel_id": room.duel_id, "duel": room.snapshot()}

    return convert_decimals(result)


@router.post("/leave")
async def leave_duel(req: LeaveRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)

    async with _dice_registry_lock:
        room = _dice_rooms.get(req.duel_id)
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
                    "UPDATE dice_duels SET status='cancelled', completed_at=NOW() WHERE id=$1",
                    room.duel_id
                )
            async with _dice_registry_lock:
                _dice_rooms.pop(room.duel_id, None)
        else:
            await room.broadcast({'type': 'player_left', 'duel_id': room.duel_id, 'duel': room.snapshot()})

    return {"success": True}


@router.get("/duels")
async def list_duels():
    async with _dice_registry_lock:
        rooms = list(_dice_rooms.values())
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
    async with _dice_registry_lock:
        room = _dice_rooms.get(duel_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        duel = await conn.fetchrow("""
            SELECT d.*, u1.username AS p1_username, u2.username AS p2_username
            FROM dice_duels d
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
        'roll1': duel['roll1'], 'roll2': duel['roll2'],
        'lock_deadline': None,
        'players': players,
    }
    return convert_decimals(result)


@router.get("/history")
async def duel_history(limit: int = 20):
    limit = max(1, min(limit, 50))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT d.id, d.stake, d.winner_id, d.roll1, d.roll2, d.completed_at, u.username AS winner_username
            FROM dice_duels d
            LEFT JOIN users u ON u.user_id = d.winner_id
            WHERE d.status='completed'
            ORDER BY d.completed_at DESC
            LIMIT $1
        """, limit)
    result = {"history": [
        {
            'duel_id': r['id'], 'stake': r['stake'], 'winner_id': r['winner_id'],
            'winner_username': r['winner_username'], 'roll1': r['roll1'], 'roll2': r['roll2'],
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

    async with _dice_registry_lock:
        room = _dice_rooms.get(duel_id)
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
