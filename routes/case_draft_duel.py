# ============================================================
# routes/case_draft_duel.py
# CS2CaseBot | Case Draft Duel (1v1, cash entry fee)
#
# The one genuinely novel mechanic this session -- no alternating-
# pick precedent exists anywhere else in the codebase. Two players
# each pay an entry fee; a shared pool of 6 random cases is drawn,
# and players alternately draft one case at a time (3 picks each),
# immediately opening it via the same shared.get_random_item() the
# real case-opening flow and Case Battles use. Confirmed against
# routes/case_battles.py's _handle_open (lines 810-877): opened
# items go straight into the OPENER's own inventory regardless of
# who wins overall -- only the cash entry-fee pool changes hands to
# the winner (total-value tiebreak, 5% rake same as Case Battles,
# since this is fundamentally a 2-player Case Battle variant, not a
# symmetric duel like Dice/Weapon/Reaction).
# ============================================================

import asyncio
import time
from typing import Dict, List, Set, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from pydantic import BaseModel

import shared
from shared import (
    logger, get_db, require_auth, ensure_user_exists,
    broadcast_to_set, convert_decimals,
    secure_shuffle, secure_choice, deduct_balance, credit_win,
    get_random_item, CASES,
    check_rate_limit, RATE_WRITE, log_game,
)

router = APIRouter(prefix="/api/games/case-draft-duel", tags=["case-draft-duel"])

MAX_PLAYERS  = 2
POOL_SIZE    = 6   # 3 picks each
MIN_FEE      = 10.0
MAX_FEE      = 750_000.0
IDLE_WAITING_SECS = 600   # abandon a waiting-for-2nd-player duel after 10 min


async def init_case_draft_duel_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS case_draft_duels (
                id                 SERIAL PRIMARY KEY,
                status             TEXT DEFAULT 'waiting'
                                   CHECK (status IN ('waiting','drafting','resolving','completed','cancelled')),
                entry_fee          DECIMAL(15,2) NOT NULL CHECK (entry_fee >= 10),
                player1_id         BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                player2_id         BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                pool_case_ids      JSONB,
                current_pick_index INTEGER DEFAULT 0,
                winner_id          BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                created_at         TIMESTAMP DEFAULT NOW(),
                completed_at       TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS case_draft_duel_picks (
                id          SERIAL PRIMARY KEY,
                duel_id     INTEGER NOT NULL REFERENCES case_draft_duels(id) ON DELETE CASCADE,
                user_id     BIGINT  NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                case_id     TEXT NOT NULL,
                pick_index  INTEGER NOT NULL,
                item_name   TEXT NOT NULL,
                rarity      TEXT,
                value       DECIMAL(15,2) NOT NULL,
                is_stattrak BOOLEAN DEFAULT FALSE,
                float_value DECIMAL(10,4),
                image_url   TEXT,
                opened_at   TIMESTAMP DEFAULT NOW(),
                UNIQUE(duel_id, pick_index)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_case_draft_duels_status ON case_draft_duels(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_case_draft_duel_picks_duel ON case_draft_duel_picks(duel_id)")
    logger.info("✅ Case Draft Duel tables ready")


async def recover_stale_case_draft_duels():
    """Startup crash-recovery: refund the entry fee to both players for any
    duel stuck mid-draft. Items already picked stay in the picker's
    inventory (they're real, already-granted items, same as Case Battles --
    only the still-unpaid entry-fee escrow needs unwinding). 'waiting' is
    included because a duel's in-memory DraftDuelRoom (the only thing the
    manual backout depends on) is gone after any restart."""
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id, entry_fee, player1_id, player2_id FROM case_draft_duels WHERE status IN ('waiting','drafting','resolving') FOR UPDATE"
            )
            for row in stale:
                fee = float(row["entry_fee"])
                if row["player1_id"]:
                    await shared.add_balance(row["player1_id"], fee, conn)
                if row["player2_id"]:
                    await shared.add_balance(row["player2_id"], fee, conn)
                await conn.execute(
                    "UPDATE case_draft_duels SET status='cancelled', completed_at=NOW() WHERE id=$1", row["id"]
                )
            if stale:
                logger.info(f"🃏 Recovered {len(stale)} stale case draft duel(s), refunded both entry fees")


async def expire_stale_case_draft_duels_loop():
    """Runtime safety net (no restart needed), same shape as dice_duel's
    equivalent loop."""
    while True:
        await asyncio.sleep(60)
        try:
            pool = await get_db()
            async with pool.acquire() as conn:
                stale = await conn.fetch(
                    "SELECT id FROM case_draft_duels WHERE status='waiting' "
                    "AND created_at <= NOW() - make_interval(secs => $1)",
                    IDLE_WAITING_SECS
                )
                count = 0
                for row in stale:
                    duel_id = row['id']
                    async with conn.transaction():
                        claimed = await conn.fetchrow(
                            "UPDATE case_draft_duels SET status='cancelled', completed_at=NOW() "
                            "WHERE id=$1 AND status='waiting' RETURNING entry_fee, player1_id, player2_id",
                            duel_id
                        )
                        if not claimed:
                            continue
                        fee = float(claimed["entry_fee"])
                        if claimed["player1_id"]:
                            await shared.add_balance(claimed["player1_id"], fee, conn)
                        if claimed["player2_id"]:
                            await shared.add_balance(claimed["player2_id"], fee, conn)
                        count += 1
                    async with _draft_registry_lock:
                        room = _draft_rooms.pop(duel_id, None)
                    if room:
                        await room.broadcast({'type': 'cancelled', 'duel_id': duel_id, 'reason': 'timed_out'})
                if count:
                    logger.info(f"🃏 Expired {count} idle case draft duel(s), refunded entry fees")
        except Exception as e:
            logger.warning(f"expire_stale_case_draft_duels_loop failed: {e}")


class DraftDuelRoom:
    def __init__(self, duel_id: int, entry_fee: float):
        self.duel_id = duel_id
        self.entry_fee = entry_fee
        self.status = 'waiting'   # waiting | drafting | resolving | completed | cancelled
        self.players: Dict[int, Dict] = {}   # user_id -> {username}, insertion order = pick order
        self.pool_case_ids: List[str] = []
        self.picks: List[Dict] = []
        self.ws_set: Set[WebSocket] = set()
        self.ws_map: Dict[int, WebSocket] = {}
        self.lock = asyncio.Lock()
        self.created_at = time.time()

    def player_order(self) -> List[int]:
        return list(self.players.keys())

    def totals(self) -> Dict[int, float]:
        totals: Dict[int, float] = {}
        for pk in self.picks:
            totals[pk['user_id']] = totals.get(pk['user_id'], 0.0) + pk['value']
        return totals

    async def broadcast(self, msg: dict):
        dead = await broadcast_to_set(self.ws_set, convert_decimals(msg))
        self.ws_set -= dead

    def snapshot(self) -> dict:
        order = self.player_order()
        turn_user_id = None
        if self.status == 'drafting' and len(order) == MAX_PLAYERS and len(self.picks) < POOL_SIZE:
            turn_user_id = order[len(self.picks) % 2]
        picked_ids = {pk['case_id'] for pk in self.picks}
        return {
            'duel_id': self.duel_id,
            'entry_fee': self.entry_fee,
            'status': self.status,
            'players': [{'user_id': uid, **p} for uid, p in self.players.items()],
            'pool_case_ids': self.pool_case_ids,
            'available_case_ids': [c for c in self.pool_case_ids if c not in picked_ids],
            'picks': self.picks,
            'totals': self.totals(),
            'turn_user_id': turn_user_id,
        }

    async def resolve(self):
        async with self.lock:
            if self.status != 'drafting':
                return
            self.status = 'resolving'

            order = self.player_order()
            if len(order) < MAX_PLAYERS:
                pool = await get_db()
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        for uid in order:
                            await shared.add_balance(uid, self.entry_fee, conn)
                        await conn.execute(
                            "UPDATE case_draft_duels SET status='cancelled', completed_at=NOW() WHERE id=$1",
                            self.duel_id
                        )
                self.status = 'cancelled'
                await self.broadcast({'type': 'cancelled', 'duel_id': self.duel_id, 'reason': 'not_enough_players'})
                async with _draft_registry_lock:
                    _draft_rooms.pop(self.duel_id, None)
                return

            totals = self.totals()
            p1, p2 = order[0], order[1]
            t1, t2 = totals.get(p1, 0.0), totals.get(p2, 0.0)
            if t1 == t2:
                # Total-value tie -- coinflip tiebreak rather than a reroll,
                # since the drafted items are already real and granted (unlike
                # Dice/Weapon/Reaction Duel, redrafting would mean discarding
                # already-owned items, which isn't fair to either player).
                winner_id = secure_choice([p1, p2])
            else:
                winner_id = p1 if t1 > t2 else p2

            prize = round(self.entry_fee * 2 * 0.95, 2)   # Case Battles' rake convention
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await credit_win(winner_id, prize, conn)
                    await conn.execute(
                        "UPDATE case_draft_duels SET status='completed', winner_id=$1, completed_at=NOW() WHERE id=$2",
                        winner_id, self.duel_id
                    )
                    for uid in (p1, p2):
                        await log_game(conn, uid, 'case_draft_duel', self.entry_fee,
                                       prize if uid == winner_id else 0.0,
                                       {'duel_id': self.duel_id, 'totals': {str(k): v for k, v in totals.items()}})

            self.status = 'completed'
            await self.broadcast({
                'type': 'duel_result',
                'duel_id': self.duel_id,
                'winner_id': winner_id,
                'totals': {str(k): v for k, v in totals.items()},
                'prize': prize,
            })
            async with _draft_registry_lock:
                _draft_rooms.pop(self.duel_id, None)


_draft_rooms: Dict[int, DraftDuelRoom] = {}
_draft_registry_lock = asyncio.Lock()


async def create_private_room(participant_user_ids: list, entry_fee: float) -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py) -- see routes/dice_duel.py's create_private_room
    for the full rationale. Differs from that template in that this game
    has no timed "locking" phase -- both players are already known, so
    the draft pool is drawn immediately and the room starts straight in
    'drafting' status, same as the moment a 2nd public joiner arrives."""
    if len(participant_user_ids) != MAX_PLAYERS:
        raise HTTPException(400, f"Case Draft Duel needs exactly {MAX_PLAYERS} players")

    pool_case_ids = secure_shuffle(list(CASES.keys()))[:POOL_SIZE]

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                INSERT INTO case_draft_duels (status, entry_fee, player1_id, player2_id, pool_case_ids)
                VALUES ('drafting', $1, $2, $3, $4) RETURNING id
            """, entry_fee, participant_user_ids[0], participant_user_ids[1], pool_case_ids)
            duel_id = row['id']

            players: Dict[int, Dict] = {}
            for uid in participant_user_ids:
                if not await deduct_balance(uid, entry_fee, conn):
                    raise HTTPException(400, f"Player {uid} has insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                players[uid] = {'username': user_row['username'] if user_row else f'Player {uid}'}

    room = DraftDuelRoom(duel_id, entry_fee)
    room.status = 'drafting'
    room.players = players
    room.pool_case_ids = pool_case_ids
    async with _draft_registry_lock:
        _draft_rooms[duel_id] = room
    await room.broadcast({'type': 'drafting_start', 'duel_id': duel_id, 'duel': room.snapshot()})
    return duel_id


async def _get_or_create_open_room(entry_fee: float) -> DraftDuelRoom:
    async with _draft_registry_lock:
        for room in _draft_rooms.values():
            if room.status == 'waiting' and room.entry_fee == entry_fee and len(room.players) < MAX_PLAYERS:
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO case_draft_duels (status, entry_fee) VALUES ('waiting', $1) RETURNING id",
                entry_fee
            )
        room = DraftDuelRoom(row['id'], entry_fee)
        _draft_rooms[room.duel_id] = room
        return room


class JoinRequest(BaseModel):
    entry_fee: float
    duel_id: Optional[int] = None


class LeaveRequest(BaseModel):
    duel_id: int


class PickRequest(BaseModel):
    duel_id: int
    case_id: str


@router.post("/join")
async def join_duel(req: JoinRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)
    await ensure_user_exists(user_id)

    entry_fee = round(float(req.entry_fee), 2)
    if entry_fee < MIN_FEE or entry_fee > MAX_FEE:
        raise HTTPException(400, f"Entry fee must be between ${MIN_FEE:.2f} and ${MAX_FEE:,.2f}")

    if req.duel_id is not None:
        async with _draft_registry_lock:
            room = _draft_rooms.get(req.duel_id)
        if not room:
            raise HTTPException(404, "Duel not found or already closed")
        if room.entry_fee != entry_fee:
            raise HTTPException(400, f"This duel's entry fee is ${room.entry_fee:.2f}")
    else:
        room = await _get_or_create_open_room(entry_fee)

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
                if not await deduct_balance(user_id, entry_fee, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'

                col = 'player1_id' if room.players == {} else 'player2_id'
                await conn.execute(f"UPDATE case_draft_duels SET {col}=$1 WHERE id=$2", user_id, room.duel_id)

        room.players[user_id] = {'username': username}

        if len(room.players) >= MAX_PLAYERS:
            room.pool_case_ids = secure_shuffle(list(CASES.keys()))[:POOL_SIZE]
            room.status = 'drafting'
            pool = await get_db()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE case_draft_duels SET status='drafting', pool_case_ids=$1 WHERE id=$2",
                    room.pool_case_ids, room.duel_id
                )
            await room.broadcast({'type': 'drafting_start', 'duel_id': room.duel_id, 'duel': room.snapshot()})

        await room.broadcast({'type': 'player_joined', 'duel_id': room.duel_id, 'duel': room.snapshot()})
        result = {"success": True, "duel_id": room.duel_id, "duel": room.snapshot()}

    return convert_decimals(result)


@router.post("/leave")
async def leave_duel(req: LeaveRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)

    async with _draft_registry_lock:
        room = _draft_rooms.get(req.duel_id)
    if not room:
        raise HTTPException(404, "Duel not found or already closed")

    async with room.lock:
        if room.status != 'waiting':
            raise HTTPException(400, "This duel has already started -- your entry is locked in")
        if user_id not in room.players:
            raise HTTPException(400, "You're not in this duel")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await shared.add_balance(user_id, room.entry_fee, conn)

        del room.players[user_id]

        if len(room.players) == 0:
            room.status = 'cancelled'
            pool = await get_db()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE case_draft_duels SET status='cancelled', completed_at=NOW() WHERE id=$1",
                    room.duel_id
                )
            async with _draft_registry_lock:
                _draft_rooms.pop(room.duel_id, None)
        else:
            await room.broadcast({'type': 'player_left', 'duel_id': room.duel_id, 'duel': room.snapshot()})

    return {"success": True}


@router.post("/pick")
async def pick_case(req: PickRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)

    async with _draft_registry_lock:
        room = _draft_rooms.get(req.duel_id)
    if not room:
        raise HTTPException(404, "Duel not found or already closed")

    finished = False
    async with room.lock:
        if room.status != 'drafting':
            raise HTTPException(400, "This duel isn't accepting picks right now")
        order = room.player_order()
        if user_id not in order:
            raise HTTPException(403, "You're not a participant in this duel")
        if len(room.picks) >= POOL_SIZE:
            raise HTTPException(400, "The draft is already complete")

        expected_user = order[len(room.picks) % 2]
        if user_id != expected_user:
            raise HTTPException(400, "It's not your turn")

        picked_ids = {pk['case_id'] for pk in room.picks}
        if req.case_id not in room.pool_case_ids or req.case_id in picked_ids:
            raise HTTPException(400, "That case isn't available to pick")

        item = get_random_item(req.case_id)
        if not item:
            raise HTTPException(400, "Could not open that case")

        pick_index = len(room.picks)
        skin_img_file = item.get('image_filename')
        skin_img_url = f"/static/images/skins/{skin_img_file}" if skin_img_file else None

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO case_draft_duel_picks
                        (duel_id, user_id, case_id, pick_index, item_name, rarity, value, is_stattrak, float_value, image_url)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """, room.duel_id, user_id, req.case_id, pick_index, item['name'], item['rarity'],
                    item['price'], item.get('is_stattrak', False), item.get('float', 0.0), skin_img_url)

                # Same idiom as Case Battles' _handle_open -- the opened item
                # goes straight into the picker's own inventory regardless of
                # who ends up winning the duel's cash prize.
                await conn.execute("""
                    INSERT INTO inventory
                        (user_id, item_name, item_type, rarity, price, condition, is_stattrak, status, float_value, image_url)
                    VALUES ($1,$2,'weapon',$3,$4,$5,$6,'kept',$7,$8)
                """, user_id, item['name'], item['rarity'], item['price'],
                    item.get('condition', 'Field-Tested'), item.get('is_stattrak', False),
                    item.get('float', 0.0), skin_img_url)
                await conn.execute("""
                    UPDATE users SET total_opens = total_opens + 1,
                        total_golds = total_golds + $2
                    WHERE user_id = $1
                """, user_id, 1 if item['rarity'] == 'Gold' else 0)

        pick_record = {
            'user_id': user_id, 'case_id': req.case_id, 'pick_index': pick_index,
            'item_name': item['name'], 'rarity': item['rarity'], 'value': float(item['price']),
            'is_stattrak': item.get('is_stattrak', False), 'image_url': skin_img_url,
        }
        room.picks.append(pick_record)

        finished = len(room.picks) >= POOL_SIZE
        await room.broadcast({'type': 'pick_made', 'duel_id': room.duel_id, 'pick': pick_record, 'duel': room.snapshot()})
        result = {"success": True, "pick": pick_record}

    if finished:
        await room.resolve()

    return convert_decimals(result)


@router.get("/duels")
async def list_duels():
    async with _draft_registry_lock:
        rooms = list(_draft_rooms.values())
    result = {"duels": [
        {
            'duel_id': r.duel_id,
            'entry_fee': r.entry_fee,
            'status': r.status,
            'player_count': len(r.players),
        }
        for r in rooms if r.status == 'waiting'
    ]}
    return convert_decimals(result)


@router.get("/duels/{duel_id}")
async def get_duel(duel_id: int):
    async with _draft_registry_lock:
        room = _draft_rooms.get(duel_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        duel = await conn.fetchrow("""
            SELECT d.*, u1.username AS p1_username, u2.username AS p2_username
            FROM case_draft_duels d
            LEFT JOIN users u1 ON u1.user_id = d.player1_id
            LEFT JOIN users u2 ON u2.user_id = d.player2_id
            WHERE d.id=$1
        """, duel_id)
        if not duel:
            raise HTTPException(404, "Duel not found")
        picks = await conn.fetch(
            "SELECT * FROM case_draft_duel_picks WHERE duel_id=$1 ORDER BY pick_index", duel_id
        )

    players = []
    if duel['player1_id']:
        players.append({'user_id': duel['player1_id'], 'username': duel['p1_username']})
    if duel['player2_id']:
        players.append({'user_id': duel['player2_id'], 'username': duel['p2_username']})

    result = {
        'duel_id': duel_id,
        'entry_fee': duel['entry_fee'],
        'status': duel['status'],
        'winner_id': duel['winner_id'],
        'pool_case_ids': duel['pool_case_ids'],
        'players': players,
        'picks': [
            {
                'user_id': p['user_id'], 'case_id': p['case_id'], 'pick_index': p['pick_index'],
                'item_name': p['item_name'], 'rarity': p['rarity'], 'value': p['value'],
                'is_stattrak': p['is_stattrak'], 'image_url': p['image_url'],
            }
            for p in picks
        ],
        'turn_user_id': None,
    }
    return convert_decimals(result)


@router.get("/history")
async def duel_history(limit: int = 20):
    limit = max(1, min(limit, 50))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT d.id, d.entry_fee, d.winner_id, d.completed_at, u.username AS winner_username
            FROM case_draft_duels d
            LEFT JOIN users u ON u.user_id = d.winner_id
            WHERE d.status='completed'
            ORDER BY d.completed_at DESC
            LIMIT $1
        """, limit)
    result = {"history": [
        {
            'duel_id': r['id'], 'entry_fee': r['entry_fee'], 'winner_id': r['winner_id'],
            'winner_username': r['winner_username'],
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

    async with _draft_registry_lock:
        room = _draft_rooms.get(duel_id)
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
