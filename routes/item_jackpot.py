# ============================================================
# routes/item_jackpot.py
# CS2CaseBot | Item Jackpot Pot
#
# Players stake a real inventory item into a shared pot. Once
# MIN_PLAYERS have joined, a countdown starts (resetting on each new
# join); at expiry a weighted-random winner (probability = their
# stake value / total pot value) takes every staked item. No house
# rake, no bots (real items are at stake).
#
# Real items are at stake, so the DB is the source of truth for
# entries throughout a room's life (unlike Crash, where bets only
# touch the DB at settlement) -- in-memory JackpotRoom state is a
# cache, not authoritative. Every mutation to a room's status/entries
# goes through that room's own asyncio.Lock so join/backout/roll can
# never interleave for the same room -- see join_pot/leave_pot/roll.
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
    secure_random, check_rate_limit, RATE_WRITE,
    relax_inventory_fk_to_set_null,
)

router = APIRouter(prefix="/api/games/jackpot", tags=["item-jackpot"])

MIN_PLAYERS     = 2
MAX_PLAYERS     = 8
COUNTDOWN_SECS  = 30
MIN_STAKE_VALUE = 0.50
IDLE_WAITING_SECS = 600   # abandon an empty-of-a-2nd-player waiting pot after 10 min


# ============================================================
# TABLE SETUP
# ============================================================

async def init_jackpot_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS item_jackpots (
                id            SERIAL PRIMARY KEY,
                status        TEXT DEFAULT 'waiting'
                              CHECK (status IN ('waiting','countdown','rolling','completed','cancelled')),
                total_value   DECIMAL(15,2) DEFAULT 0,
                winner_id     BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                winner_roll   DECIMAL(10,8),
                created_at    TIMESTAMP DEFAULT NOW(),
                countdown_started_at TIMESTAMP,
                rolled_at     TIMESTAMP,
                completed_at  TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS item_jackpot_entries (
                id            SERIAL PRIMARY KEY,
                jackpot_id    INTEGER NOT NULL REFERENCES item_jackpots(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
                inventory_id  INTEGER NOT NULL REFERENCES inventory(id)    ON DELETE RESTRICT,
                item_name     TEXT NOT NULL,
                rarity        TEXT,
                condition     TEXT,
                is_stattrak   BOOLEAN DEFAULT FALSE,
                float_value   DECIMAL(10,4),
                image_url     TEXT,
                value         DECIMAL(15,2) NOT NULL CHECK (value >= 0.50),
                joined_at     TIMESTAMP DEFAULT NOW(),
                backed_out_at TIMESTAMP,
                UNIQUE(jackpot_id, inventory_id)
            )
        """)
        # Fully qualified names throughout -- a shorter "idx_jackpot_entries_user"
        # collided with a same-named index already on the unrelated pre-existing
        # solo Slots progressive-jackpot feature's own `jackpot_entries` table
        # (Postgres index names must be unique per schema), which meant
        # IF NOT EXISTS silently no-op'd instead of ever creating it. Keeping
        # all three names namespaced under item_jackpot* to rule that out.
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_item_jackpot_entries_jackpot ON item_jackpot_entries(jackpot_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_item_jackpot_entries_user ON item_jackpot_entries(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_item_jackpots_status ON item_jackpots(status)")
        await relax_inventory_fk_to_set_null(conn, 'item_jackpot_entries')
    logger.info("✅ Item Jackpot tables ready")


async def recover_stale_jackpots():
    """Startup crash-recovery: any pot left mid-flight (waiting/countdown/
    rolling) from a previous process death can't be trusted -- the in-memory
    JackpotRoom that was driving it is gone after a restart, and since
    leave_pot() (the only manual way out of a 'waiting' pot) also depends on
    that same in-memory room, a 'waiting' pot left over from before a restart
    is unrecoverable by any other means too. Refund every staked item back to
    its owner and mark the pot cancelled."""
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id FROM item_jackpots WHERE status IN ('waiting','countdown','rolling') FOR UPDATE"
            )
            for row in stale:
                jackpot_id = row['id']
                entries = await conn.fetch(
                    "SELECT inventory_id FROM item_jackpot_entries WHERE jackpot_id=$1 AND backed_out_at IS NULL",
                    jackpot_id
                )
                item_ids = [e['inventory_id'] for e in entries]
                if item_ids:
                    await conn.execute(
                        "UPDATE inventory SET status='kept' WHERE id = ANY($1::int[]) AND status='staked'",
                        item_ids
                    )
                await conn.execute(
                    "UPDATE item_jackpots SET status='cancelled', completed_at=NOW() WHERE id=$1",
                    jackpot_id
                )
            if stale:
                logger.info(f"🎁 Recovered {len(stale)} stale item jackpot(s), refunded all staked items")


async def expire_stale_item_jackpots_loop():
    """Runtime safety net (no restart needed): a public pot stuck 'waiting'
    for a 2nd+ player past IDLE_WAITING_SECS is abandoned -- refund every
    staked item and cancel it. IDLE_WAITING_SECS was already defined for
    exactly this above, but nothing ever actually enforced it until now.
    Claims each pot with an atomic status-guarded UPDATE first, so this can
    never race a legitimate join filling the pot right at the boundary."""
    while True:
        await asyncio.sleep(60)
        try:
            pool = await get_db()
            async with pool.acquire() as conn:
                stale = await conn.fetch(
                    "SELECT id FROM item_jackpots WHERE status='waiting' "
                    "AND created_at <= NOW() - make_interval(secs => $1)",
                    IDLE_WAITING_SECS
                )
                count = 0
                for row in stale:
                    jackpot_id = row['id']
                    async with conn.transaction():
                        claimed = await conn.fetchval(
                            "UPDATE item_jackpots SET status='cancelled', completed_at=NOW() "
                            "WHERE id=$1 AND status='waiting' RETURNING id",
                            jackpot_id
                        )
                        if not claimed:
                            continue
                        entries = await conn.fetch(
                            "SELECT inventory_id FROM item_jackpot_entries WHERE jackpot_id=$1 AND backed_out_at IS NULL",
                            jackpot_id
                        )
                        item_ids = [e['inventory_id'] for e in entries]
                        if item_ids:
                            await conn.execute(
                                "UPDATE inventory SET status='kept' WHERE id = ANY($1::int[]) AND status='staked'",
                                item_ids
                            )
                        count += 1
                    async with _jackpot_registry_lock:
                        room = _jackpot_rooms.pop(jackpot_id, None)
                    if room:
                        await room.broadcast({'type': 'cancelled', 'jackpot_id': jackpot_id, 'reason': 'timed_out'})
                if count:
                    logger.info(f"🎁 Expired {count} idle item jackpot(s), refunded staked items")
        except Exception as e:
            logger.warning(f"expire_stale_item_jackpots_loop failed: {e}")


# ============================================================
# ROOM
# ============================================================

class JackpotRoom:
    def __init__(self, jackpot_id: int):
        self.jackpot_id = jackpot_id
        self.status = 'waiting'   # waiting | countdown | rolling | completed | cancelled
        # entry_id -> {user_id, username, inventory_id, item_name, rarity,
        #              condition, is_stattrak, float_value, image_url, value}
        self.entries: Dict[int, Dict] = {}
        self.ws_set: Set[WebSocket] = set()
        self.ws_map: Dict[int, WebSocket] = {}
        self.task: Optional[asyncio.Task] = None
        self.countdown_deadline: Optional[float] = None
        self.lock = asyncio.Lock()
        self.created_at = time.time()

    def total_value(self) -> float:
        return round(sum(e['value'] for e in self.entries.values()), 2)

    async def broadcast(self, msg: dict):
        dead = await broadcast_to_set(self.ws_set, convert_decimals(msg))
        self.ws_set -= dead

    def snapshot(self) -> dict:
        return {
            'jackpot_id': self.jackpot_id,
            'status': self.status,
            'total_value': self.total_value(),
            'countdown_deadline': self.countdown_deadline,
            'entries': [
                {'entry_id': eid, **e}
                for eid, e in self.entries.items()
            ],
        }

    async def run_countdown(self):
        """Polls every 0.5s rather than a fixed sleep(30) so a countdown_reset
        from a new join (which just mutates self.countdown_deadline) is picked
        up without needing to cancel/recreate this task."""
        while True:
            await asyncio.sleep(0.5)
            async with self.lock:
                if self.status != 'countdown':
                    return
                if len(self.entries) >= MAX_PLAYERS:
                    break
                if self.countdown_deadline is not None and self.countdown_deadline - time.time() <= 0:
                    break
        await self.roll()

    async def roll(self):
        async with self.lock:
            if self.status != 'countdown':
                return   # guards double-invocation
            self.status = 'rolling'
            await self.broadcast({'type': 'rolling', 'jackpot_id': self.jackpot_id})

            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    rows = await conn.fetch("""
                        SELECT e.id, e.user_id, e.inventory_id, e.value, u.username
                        FROM item_jackpot_entries e
                        JOIN users u ON u.user_id = e.user_id
                        WHERE e.jackpot_id=$1 AND e.backed_out_at IS NULL
                        FOR UPDATE OF e
                    """, self.jackpot_id)

                    if len(rows) < MIN_PLAYERS:
                        # Safety net: shouldn't be reachable (countdown only
                        # starts at MIN_PLAYERS and backout is blocked once
                        # countdown begins), but never pay out an under-filled
                        # pot -- refund everything instead.
                        item_ids = [r['inventory_id'] for r in rows]
                        if item_ids:
                            await conn.execute(
                                "UPDATE inventory SET status='kept' WHERE id = ANY($1::int[]) AND status='staked'",
                                item_ids
                            )
                        await conn.execute(
                            "UPDATE item_jackpots SET status='cancelled', completed_at=NOW() WHERE id=$1",
                            self.jackpot_id
                        )
                        self.status = 'cancelled'
                        await self.broadcast({'type': 'cancelled', 'jackpot_id': self.jackpot_id, 'reason': 'not_enough_players'})
                        async with _jackpot_registry_lock:
                            _jackpot_rooms.pop(self.jackpot_id, None)
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
                        UPDATE item_jackpots
                        SET status='completed', winner_id=$1, winner_roll=$2,
                            total_value=$3, rolled_at=NOW(), completed_at=NOW()
                        WHERE id=$4
                    """, winner_id, (roll_val / total) if total else 0, total, self.jackpot_id)

            self.status = 'completed'
            await self.broadcast({
                'type': 'roll_result',
                'jackpot_id': self.jackpot_id,
                'winner_id': winner_id,
                'winner_username': winner_username,
                'total_value': round(total, 2),
                'entries': self.snapshot()['entries'],
            })
            async with _jackpot_registry_lock:
                _jackpot_rooms.pop(self.jackpot_id, None)


# ============================================================
# REGISTRY
# ============================================================

_jackpot_rooms: Dict[int, JackpotRoom] = {}
_jackpot_registry_lock = asyncio.Lock()


async def _get_or_create_open_room() -> JackpotRoom:
    async with _jackpot_registry_lock:
        for room in _jackpot_rooms.values():
            if room.status == 'waiting' and len(room.entries) < MAX_PLAYERS:
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO item_jackpots (status) VALUES ('waiting') RETURNING id"
            )
        room = JackpotRoom(row['id'])
        _jackpot_rooms[room.jackpot_id] = room
        return room


# ============================================================
# REST ROUTES
# ============================================================

class JoinRequest(BaseModel):
    inventory_id: int
    jackpot_id: Optional[int] = None


class LeaveRequest(BaseModel):
    jackpot_id: int


@router.post("/join")
async def join_pot(req: JoinRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)
    await ensure_user_exists(user_id)

    if req.jackpot_id is not None:
        async with _jackpot_registry_lock:
            room = _jackpot_rooms.get(req.jackpot_id)
        if not room:
            raise HTTPException(404, "Pot not found or already closed")
    else:
        room = await _get_or_create_open_room()

    async with room.lock:
        if room.status not in ('waiting', 'countdown'):
            raise HTTPException(400, "This pot is no longer accepting entries")
        if len(room.entries) >= MAX_PLAYERS:
            raise HTTPException(400, "This pot is full")
        if any(e['user_id'] == user_id for e in room.entries.values()):
            raise HTTPException(400, "You're already in this pot")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Atomic ownership+status+min-value guard in one statement --
                # same idiom as sell_item's "UPDATE ... WHERE status='kept'
                # RETURNING" (server.py), so a concurrent stake attempt on the
                # same item elsewhere can never both succeed. in_loadout=FALSE
                # keeps equipped items out of pots.
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
                    INSERT INTO item_jackpot_entries
                        (jackpot_id, user_id, inventory_id, item_name, rarity, condition,
                         is_stattrak, float_value, image_url, value)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    RETURNING id
                """, room.jackpot_id, user_id, req.inventory_id, item['item_name'], item['rarity'],
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

        if room.status == 'waiting' and len(room.entries) >= MIN_PLAYERS:
            room.status = 'countdown'
            room.countdown_deadline = time.time() + COUNTDOWN_SECS
            await room.broadcast({
                'type': 'countdown_start', 'jackpot_id': room.jackpot_id,
                'deadline': room.countdown_deadline,
            })
            room.task = asyncio.create_task(room.run_countdown())
        elif room.status == 'countdown':
            room.countdown_deadline = time.time() + COUNTDOWN_SECS
            await room.broadcast({
                'type': 'countdown_reset', 'jackpot_id': room.jackpot_id,
                'deadline': room.countdown_deadline,
            })

        await room.broadcast({'type': 'player_joined', 'jackpot_id': room.jackpot_id, 'pot': room.snapshot()})
        result = {"success": True, "jackpot_id": room.jackpot_id, "pot": room.snapshot()}

    return convert_decimals(result)


@router.post("/leave")
async def leave_pot(req: LeaveRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)

    async with _jackpot_registry_lock:
        room = _jackpot_rooms.get(req.jackpot_id)
    if not room:
        raise HTTPException(404, "Pot not found or already closed")

    async with room.lock:
        # This check is the whole ballgame: it must run under the same lock
        # join_pot()'s waiting->countdown transition and roll() use, so a
        # backout can never land in the gap between "countdown started" and
        # "roll() acquires the lock."
        if room.status != 'waiting':
            raise HTTPException(400, "This pot has already started -- your stake is locked in")

        entry_id = next((eid for eid, e in room.entries.items() if e['user_id'] == user_id), None)
        if entry_id is None:
            raise HTTPException(400, "You're not in this pot")

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
                    "UPDATE item_jackpot_entries SET backed_out_at=NOW() WHERE id=$1",
                    entry_id
                )

        del room.entries[entry_id]

        if len(room.entries) == 0:
            room.status = 'cancelled'
            pool = await get_db()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE item_jackpots SET status='cancelled', completed_at=NOW() WHERE id=$1",
                    room.jackpot_id
                )
            async with _jackpot_registry_lock:
                _jackpot_rooms.pop(room.jackpot_id, None)
        else:
            await room.broadcast({'type': 'player_left', 'jackpot_id': room.jackpot_id, 'pot': room.snapshot()})

    return {"success": True}


@router.get("/pots")
async def list_pots():
    async with _jackpot_registry_lock:
        rooms = list(_jackpot_rooms.values())
    result = {"pots": [
        {
            'jackpot_id': r.jackpot_id,
            'status': r.status,
            'player_count': len(r.entries),
            'total_value': r.total_value(),
            'countdown_deadline': r.countdown_deadline,
        }
        for r in rooms if r.status in ('waiting', 'countdown')
    ]}
    return convert_decimals(result)


@router.get("/pots/{jackpot_id}")
async def get_pot(jackpot_id: int):
    async with _jackpot_registry_lock:
        room = _jackpot_rooms.get(jackpot_id)
    if room:
        return convert_decimals(room.snapshot())

    # Not live in memory -- fall back to DB for a completed/cancelled pot.
    pool = await get_db()
    async with pool.acquire() as conn:
        pot = await conn.fetchrow("SELECT * FROM item_jackpots WHERE id=$1", jackpot_id)
        if not pot:
            raise HTTPException(404, "Pot not found")
        entries = await conn.fetch("""
            SELECT e.*, u.username FROM item_jackpot_entries e
            JOIN users u ON u.user_id = e.user_id
            WHERE e.jackpot_id=$1 ORDER BY e.joined_at
        """, jackpot_id)

    result = {
        'jackpot_id': jackpot_id,
        'status': pot['status'],
        'total_value': pot['total_value'] or 0,
        'winner_id': pot['winner_id'],
        'countdown_deadline': None,
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
async def jackpot_history(limit: int = 20):
    limit = max(1, min(limit, 50))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT j.id, j.total_value, j.winner_id, j.completed_at, u.username AS winner_username
            FROM item_jackpots j
            LEFT JOIN users u ON u.user_id = j.winner_id
            WHERE j.status='completed'
            ORDER BY j.completed_at DESC
            LIMIT $1
        """, limit)
    result = {"history": [
        {
            'jackpot_id': r['id'],
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

@router.websocket("/ws/{jackpot_id}")
async def jackpot_ws(websocket: WebSocket, jackpot_id: int):
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

    async with _jackpot_registry_lock:
        room = _jackpot_rooms.get(jackpot_id)
    if not room:
        try:
            await websocket.send_json({'type': 'no_room', 'jackpot_id': jackpot_id})
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
            # Only 'ping' allowed -- all actions via HTTP
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
