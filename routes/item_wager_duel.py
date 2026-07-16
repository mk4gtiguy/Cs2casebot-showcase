# ============================================================
# routes/item_wager_duel.py
# CS2CaseBot | Item Wager Duel (1v1)
#
# Two players each stake a real inventory item. Once the 2nd
# player joins, a short fixed "locking in" delay plays for
# suspense, then a weighted-random winner (probability = their
# stake value / total) takes both items. Direct simplification
# of routes/item_jackpot.py's JackpotRoom -- same escrow idiom,
# same per-room asyncio.Lock discipline -- but exactly 2 players
# and no resettable countdown (nothing to reset with only one
# open slot).
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
    secure_random, check_rate_limit, RATE_WRITE, log_game,
    relax_inventory_fk_to_set_null,
)

router = APIRouter(prefix="/api/games/duel", tags=["item-wager-duel"])

MAX_PLAYERS     = 2
LOCK_IN_SECS    = 3
MIN_STAKE_VALUE = 0.50
IDLE_WAITING_SECS = 600   # abandon a waiting-for-2nd-player duel after 10 min


# ============================================================
# TABLE SETUP
# ============================================================

async def init_duel_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS item_duels (
                id            SERIAL PRIMARY KEY,
                status        TEXT DEFAULT 'waiting'
                              CHECK (status IN ('waiting','locking','rolling','completed','cancelled')),
                total_value   DECIMAL(15,2) DEFAULT 0,
                winner_id     BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                winner_roll   DECIMAL(10,8),
                created_at    TIMESTAMP DEFAULT NOW(),
                rolled_at     TIMESTAMP,
                completed_at  TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS item_duel_entries (
                id            SERIAL PRIMARY KEY,
                duel_id       INTEGER NOT NULL REFERENCES item_duels(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                inventory_id  INTEGER NOT NULL REFERENCES inventory(id)  ON DELETE RESTRICT,
                item_name     TEXT NOT NULL,
                rarity        TEXT,
                condition     TEXT,
                is_stattrak   BOOLEAN DEFAULT FALSE,
                float_value   DECIMAL(10,4),
                image_url     TEXT,
                value         DECIMAL(15,2) NOT NULL CHECK (value >= 0.50),
                joined_at     TIMESTAMP DEFAULT NOW(),
                backed_out_at TIMESTAMP,
                UNIQUE(duel_id, inventory_id)
            )
        """)
        # Fully qualified index names -- see item_jackpot.py's comment on the
        # short-name collision bug this pattern rules out.
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_item_duel_entries_duel ON item_duel_entries(duel_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_item_duel_entries_user ON item_duel_entries(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_item_duels_status ON item_duels(status)")
        await relax_inventory_fk_to_set_null(conn, 'item_duel_entries')
    logger.info("✅ Item Wager Duel tables ready")


async def recover_stale_duels():
    """Startup crash-recovery, same shape as recover_stale_jackpots() -- now
    also covers 'waiting' since a duel's in-memory DuelRoom (the only thing a
    manual backout depends on) is gone after any restart, same reasoning as
    item_jackpot."""
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id FROM item_duels WHERE status IN ('waiting','locking','rolling') FOR UPDATE"
            )
            for row in stale:
                duel_id = row['id']
                entries = await conn.fetch(
                    "SELECT inventory_id FROM item_duel_entries WHERE duel_id=$1 AND backed_out_at IS NULL",
                    duel_id
                )
                item_ids = [e['inventory_id'] for e in entries]
                if item_ids:
                    await conn.execute(
                        "UPDATE inventory SET status='kept' WHERE id = ANY($1::int[]) AND status='staked'",
                        item_ids
                    )
                await conn.execute(
                    "UPDATE item_duels SET status='cancelled', completed_at=NOW() WHERE id=$1",
                    duel_id
                )
            if stale:
                logger.info(f"⚔️ Recovered {len(stale)} stale item duel(s), refunded all staked items")


async def expire_stale_item_duels_loop():
    """Runtime safety net (no restart needed): a duel stuck 'waiting' for a
    2nd player past IDLE_WAITING_SECS is abandoned -- refund the staked item
    and cancel it. Same atomic-claim pattern as item_jackpot's equivalent
    loop, so this can never race a legitimate 2nd player joining right at
    the boundary."""
    while True:
        await asyncio.sleep(60)
        try:
            pool = await get_db()
            async with pool.acquire() as conn:
                stale = await conn.fetch(
                    "SELECT id FROM item_duels WHERE status='waiting' "
                    "AND created_at <= NOW() - make_interval(secs => $1)",
                    IDLE_WAITING_SECS
                )
                count = 0
                for row in stale:
                    duel_id = row['id']
                    async with conn.transaction():
                        claimed = await conn.fetchval(
                            "UPDATE item_duels SET status='cancelled', completed_at=NOW() "
                            "WHERE id=$1 AND status='waiting' RETURNING id",
                            duel_id
                        )
                        if not claimed:
                            continue
                        entries = await conn.fetch(
                            "SELECT inventory_id FROM item_duel_entries WHERE duel_id=$1 AND backed_out_at IS NULL",
                            duel_id
                        )
                        item_ids = [e['inventory_id'] for e in entries]
                        if item_ids:
                            await conn.execute(
                                "UPDATE inventory SET status='kept' WHERE id = ANY($1::int[]) AND status='staked'",
                                item_ids
                            )
                        count += 1
                    async with _duel_registry_lock:
                        room = _duel_rooms.pop(duel_id, None)
                    if room:
                        await room.broadcast({'type': 'cancelled', 'duel_id': duel_id, 'reason': 'timed_out'})
                if count:
                    logger.info(f"⚔️ Expired {count} idle item duel(s), refunded staked items")
        except Exception as e:
            logger.warning(f"expire_stale_item_duels_loop failed: {e}")


# ============================================================
# ROOM
# ============================================================

class DuelRoom:
    def __init__(self, duel_id: int):
        self.duel_id = duel_id
        self.status = 'waiting'   # waiting | locking | rolling | completed | cancelled
        self.entries: Dict[int, Dict] = {}
        self.ws_set: Set[WebSocket] = set()
        self.ws_map: Dict[int, WebSocket] = {}
        self.task: Optional[asyncio.Task] = None
        self.lock_deadline: Optional[float] = None
        self.lock = asyncio.Lock()
        self.created_at = time.time()

    def total_value(self) -> float:
        return round(sum(e['value'] for e in self.entries.values()), 2)

    async def broadcast(self, msg: dict):
        dead = await broadcast_to_set(self.ws_set, convert_decimals(msg))
        self.ws_set -= dead

    def snapshot(self) -> dict:
        return {
            'duel_id': self.duel_id,
            'status': self.status,
            'total_value': self.total_value(),
            'lock_deadline': self.lock_deadline,
            'entries': [
                {'entry_id': eid, **e}
                for eid, e in self.entries.items()
            ],
        }

    async def run_lock_in(self):
        await asyncio.sleep(LOCK_IN_SECS)
        await self.roll()

    async def roll(self):
        async with self.lock:
            if self.status != 'locking':
                return   # guards double-invocation
            self.status = 'rolling'
            await self.broadcast({'type': 'rolling', 'duel_id': self.duel_id})

            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    rows = await conn.fetch("""
                        SELECT e.id, e.user_id, e.inventory_id, e.value, u.username
                        FROM item_duel_entries e
                        JOIN users u ON u.user_id = e.user_id
                        WHERE e.duel_id=$1 AND e.backed_out_at IS NULL
                        FOR UPDATE OF e
                    """, self.duel_id)

                    if len(rows) < MAX_PLAYERS:
                        # Safety net: shouldn't be reachable (locking only
                        # starts once both slots fill and backout is blocked
                        # once locking begins), but never pay out an
                        # under-filled duel -- refund everything instead.
                        item_ids = [r['inventory_id'] for r in rows]
                        if item_ids:
                            await conn.execute(
                                "UPDATE inventory SET status='kept' WHERE id = ANY($1::int[]) AND status='staked'",
                                item_ids
                            )
                        await conn.execute(
                            "UPDATE item_duels SET status='cancelled', completed_at=NOW() WHERE id=$1",
                            self.duel_id
                        )
                        self.status = 'cancelled'
                        await self.broadcast({'type': 'cancelled', 'duel_id': self.duel_id, 'reason': 'not_enough_players'})
                        async with _duel_registry_lock:
                            _duel_rooms.pop(self.duel_id, None)
                        return

                    total = sum(float(r['value']) for r in rows)
                    roll_val = secure_random() * total
                    cumulative = 0.0
                    winner_row = rows[-1]
                    for r in rows:
                        cumulative += float(r['value'])
                        if roll_val < cumulative:
                            winner_row = r
                            break
                    winner_id = winner_row['user_id']
                    winner_username = winner_row['username']

                    item_ids = [r['inventory_id'] for r in rows]
                    await conn.execute("""
                        UPDATE inventory SET user_id=$1, status='kept'
                        WHERE id = ANY($2::int[]) AND status='staked'
                    """, winner_id, item_ids)

                    await conn.execute("""
                        UPDATE item_duels
                        SET status='completed', winner_id=$1, winner_roll=$2,
                            total_value=$3, rolled_at=NOW(), completed_at=NOW()
                        WHERE id=$4
                    """, winner_id, (roll_val / total) if total else 0, total, self.duel_id)
                    for r in rows:
                        await log_game(conn, r['user_id'], 'item_wager_duel', float(r['value']),
                                       total if r['user_id'] == winner_id else 0.0,
                                       {'duel_id': self.duel_id})

            self.status = 'completed'
            await self.broadcast({
                'type': 'roll_result',
                'duel_id': self.duel_id,
                'winner_id': winner_id,
                'winner_username': winner_username,
                'total_value': round(total, 2),
                'entries': self.snapshot()['entries'],
            })
            async with _duel_registry_lock:
                _duel_rooms.pop(self.duel_id, None)


# ============================================================
# REGISTRY
# ============================================================

_duel_rooms: Dict[int, DuelRoom] = {}
_duel_registry_lock = asyncio.Lock()


async def create_private_room(participants: list) -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py). Unlike the cash duels' create_private_room, each
    participant stakes their own specific item rather than a shared cash
    amount, so `participants` is a list of (user_id, inventory_id) pairs
    instead of a bare user_id list. Otherwise same rationale as
    routes/dice_duel.py's create_private_room: everyone's already agreed
    to play, so stake everyone atomically and start the room directly in
    'locking' phase, skipping the one-at-a-time public /join flow."""
    if len(participants) != MAX_PLAYERS:
        raise HTTPException(400, f"Item Wager Duel needs exactly {MAX_PLAYERS} players")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("INSERT INTO item_duels (status) VALUES ('locking') RETURNING id")
            duel_id = row['id']

            entries: Dict[int, Dict] = {}
            for user_id, inventory_id in participants:
                item = await conn.fetchrow("""
                    UPDATE inventory SET status='staked'
                    WHERE id=$1 AND user_id=$2 AND status='kept'
                      AND price >= $3 AND in_loadout = FALSE AND protected = FALSE
                    RETURNING item_name, rarity, price, condition, is_stattrak, float_value, image_url
                """, inventory_id, user_id, MIN_STAKE_VALUE)
                if not item:
                    raise HTTPException(
                        400,
                        f"Item not available to stake (must be a kept, unequipped, unprotected item worth at least ${MIN_STAKE_VALUE:.2f})"
                    )
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'

                entry_row = await conn.fetchrow("""
                    INSERT INTO item_duel_entries
                        (duel_id, user_id, inventory_id, item_name, rarity, condition,
                         is_stattrak, float_value, image_url, value)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    RETURNING id
                """, duel_id, user_id, inventory_id, item['item_name'], item['rarity'],
                    item['condition'], item['is_stattrak'], item['float_value'], item['image_url'],
                    item['price'])

                entries[entry_row['id']] = {
                    'user_id': user_id,
                    'username': username,
                    'inventory_id': inventory_id,
                    'item_name': item['item_name'],
                    'rarity': item['rarity'],
                    'condition': item['condition'],
                    'is_stattrak': item['is_stattrak'],
                    'float_value': float(item['float_value']) if item['float_value'] is not None else None,
                    'image_url': item['image_url'],
                    'value': float(item['price']),
                }

    room = DuelRoom(duel_id)
    room.status = 'locking'
    room.entries = entries
    room.lock_deadline = time.time() + LOCK_IN_SECS
    async with _duel_registry_lock:
        _duel_rooms[duel_id] = room
    room.task = asyncio.create_task(room.run_lock_in())
    await room.broadcast({'type': 'locking_start', 'duel_id': duel_id, 'deadline': room.lock_deadline})
    return duel_id


async def _get_or_create_open_room() -> DuelRoom:
    async with _duel_registry_lock:
        for room in _duel_rooms.values():
            if room.status == 'waiting' and len(room.entries) < MAX_PLAYERS:
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO item_duels (status) VALUES ('waiting') RETURNING id"
            )
        room = DuelRoom(row['id'])
        _duel_rooms[room.duel_id] = room
        return room


# ============================================================
# REST ROUTES
# ============================================================

class JoinRequest(BaseModel):
    inventory_id: int
    duel_id: Optional[int] = None


class LeaveRequest(BaseModel):
    duel_id: int


@router.post("/join")
async def join_duel(req: JoinRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)
    await ensure_user_exists(user_id)

    if req.duel_id is not None:
        async with _duel_registry_lock:
            room = _duel_rooms.get(req.duel_id)
        if not room:
            raise HTTPException(404, "Duel not found or already closed")
    else:
        room = await _get_or_create_open_room()

    async with room.lock:
        if room.status != 'waiting':
            raise HTTPException(400, "This duel is no longer accepting entries")
        if len(room.entries) >= MAX_PLAYERS:
            raise HTTPException(400, "This duel is full")
        if any(e['user_id'] == user_id for e in room.entries.values()):
            raise HTTPException(400, "You're already in this duel")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                item = await conn.fetchrow("""
                    UPDATE inventory SET status='staked'
                    WHERE id=$1 AND user_id=$2 AND status='kept'
                      AND price >= $3 AND in_loadout = FALSE AND protected = FALSE
                    RETURNING item_name, rarity, price, condition, is_stattrak, float_value, image_url
                """, req.inventory_id, user_id, MIN_STAKE_VALUE)
                if not item:
                    raise HTTPException(
                        400,
                        f"Item not available to stake (must be a kept, unequipped, unprotected item worth at least ${MIN_STAKE_VALUE:.2f})"
                    )

                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'

                entry_row = await conn.fetchrow("""
                    INSERT INTO item_duel_entries
                        (duel_id, user_id, inventory_id, item_name, rarity, condition,
                         is_stattrak, float_value, image_url, value)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    RETURNING id
                """, room.duel_id, user_id, req.inventory_id, item['item_name'], item['rarity'],
                    item['condition'], item['is_stattrak'], item['float_value'], item['image_url'],
                    item['price'])

        entry_id = entry_row['id']
        room.entries[entry_id] = {
            'user_id': user_id,
            'username': username,
            'inventory_id': req.inventory_id,
            'item_name': item['item_name'],
            'rarity': item['rarity'],
            'condition': item['condition'],
            'is_stattrak': item['is_stattrak'],
            'float_value': float(item['float_value']) if item['float_value'] is not None else None,
            'image_url': item['image_url'],
            'value': float(item['price']),
        }

        if len(room.entries) >= MAX_PLAYERS:
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

    async with _duel_registry_lock:
        room = _duel_rooms.get(req.duel_id)
    if not room:
        raise HTTPException(404, "Duel not found or already closed")

    async with room.lock:
        if room.status != 'waiting':
            raise HTTPException(400, "This duel has already started -- your stake is locked in")

        entry_id = next((eid for eid, e in room.entries.items() if e['user_id'] == user_id), None)
        if entry_id is None:
            raise HTTPException(400, "You're not in this duel")

        inventory_id = room.entries[entry_id]['inventory_id']

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                item = await conn.fetchrow(
                    "UPDATE inventory SET status='kept' WHERE id=$1 AND user_id=$2 AND status='staked' RETURNING id",
                    inventory_id, user_id
                )
                if not item:
                    raise HTTPException(500, "Could not reclaim item -- please contact support")
                await conn.execute(
                    "UPDATE item_duel_entries SET backed_out_at=NOW() WHERE id=$1",
                    entry_id
                )

        del room.entries[entry_id]

        if len(room.entries) == 0:
            room.status = 'cancelled'
            pool = await get_db()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE item_duels SET status='cancelled', completed_at=NOW() WHERE id=$1",
                    room.duel_id
                )
            async with _duel_registry_lock:
                _duel_rooms.pop(room.duel_id, None)
        else:
            await room.broadcast({'type': 'player_left', 'duel_id': room.duel_id, 'duel': room.snapshot()})

    return {"success": True}


@router.get("/duels")
async def list_duels():
    async with _duel_registry_lock:
        rooms = list(_duel_rooms.values())
    result = {"duels": [
        {
            'duel_id': r.duel_id,
            'status': r.status,
            'player_count': len(r.entries),
            'total_value': r.total_value(),
        }
        for r in rooms if r.status == 'waiting'
    ]}
    return convert_decimals(result)


@router.get("/duels/{duel_id}")
async def get_duel(duel_id: int):
    async with _duel_registry_lock:
        room = _duel_rooms.get(duel_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        duel = await conn.fetchrow("SELECT * FROM item_duels WHERE id=$1", duel_id)
        if not duel:
            raise HTTPException(404, "Duel not found")
        entries = await conn.fetch("""
            SELECT e.*, u.username FROM item_duel_entries e
            JOIN users u ON u.user_id = e.user_id
            WHERE e.duel_id=$1 ORDER BY e.joined_at
        """, duel_id)

    result = {
        'duel_id': duel_id,
        'status': duel['status'],
        'total_value': duel['total_value'] or 0,
        'winner_id': duel['winner_id'],
        'lock_deadline': None,
        'entries': [
            {
                'entry_id': e['id'], 'user_id': e['user_id'], 'username': e['username'],
                'item_name': e['item_name'], 'rarity': e['rarity'], 'condition': e['condition'],
                'is_stattrak': e['is_stattrak'], 'float_value': e['float_value'],
                'image_url': e['image_url'], 'value': e['value'],
                'backed_out': e['backed_out_at'] is not None,
            }
            for e in entries
        ],
    }
    return convert_decimals(result)


@router.get("/history")
async def duel_history(limit: int = 20):
    limit = max(1, min(limit, 50))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT d.id, d.total_value, d.winner_id, d.completed_at, u.username AS winner_username
            FROM item_duels d
            LEFT JOIN users u ON u.user_id = d.winner_id
            WHERE d.status='completed'
            ORDER BY d.completed_at DESC
            LIMIT $1
        """, limit)
    result = {"history": [
        {
            'duel_id': r['id'],
            'total_value': r['total_value'] or 0,
            'winner_id': r['winner_id'],
            'winner_username': r['winner_username'],
            'completed_at': r['completed_at'].isoformat() if r['completed_at'] else None,
        }
        for r in rows
    ]}
    return convert_decimals(result)


# ============================================================
# WEBSOCKET
# ============================================================

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

    async with _duel_registry_lock:
        room = _duel_rooms.get(duel_id)
    if not room:
        try:
            await websocket.send_json({'type': 'no_room', 'duel_id': duel_id})
        except Exception:
            pass
        await websocket.close()
        return

    is_player = any(e['user_id'] == user_id for e in room.entries.values())
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
