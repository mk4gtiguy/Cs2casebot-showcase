# ============================================================
# routes/live_case_auction.py
# CS2CaseBot | Live Case Auction
#
# A BLIND bid, not a reveal-then-sell auction: players bid on the
# right to open a specific (server-chosen) case sight-unseen. The
# winning bidder pays their final bid, the case opens via the
# existing shared.get_random_item(case_id), and they keep whatever
# item results -- win or lose relative to their bid. Matches the
# site's "Live" gambling convention (Live Roulette/Keno/Blackjack),
# not the existing peer-to-peer marketplace (routes/market.py, a
# pure fixed-price buy-now system with zero bidding concept).
#
# Bids are ESCROWED, not deferred: placing a bid immediately deducts
# the amount from the bidder's balance, and getting outbid immediately
# refunds the previous bid automatically. This guarantees the eventual
# winner's payment is already covered at auction close.
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
    require_game_enabled, secure_choice, get_random_item, CASES, log_game,
)

router = APIRouter(prefix="/api/games/live-case-auction", tags=["live-case-auction"])

MIN_BID       = 10
MAX_BID       = 750_000
MAX_PLAYERS   = 16   # a bidding room isn't seat-limited the way an action-room is
BIDDING_SECS  = 20   # resets to this on every new bid
MIN_INCREMENT_FLAT = 10
MIN_INCREMENT_PCT  = 0.05


def _min_next_bid(current_bid: float) -> float:
    if current_bid <= 0:
        return MIN_BID
    return round(current_bid + max(MIN_INCREMENT_FLAT, current_bid * MIN_INCREMENT_PCT), 2)


# ============================================================
# TABLE SETUP
# ============================================================

async def init_live_case_auction_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS case_auction_rounds (
                id             SERIAL PRIMARY KEY,
                status         TEXT DEFAULT 'bidding'
                               CHECK (status IN ('bidding','settled','cancelled')),
                case_id        TEXT NOT NULL,
                current_bid    DECIMAL(15,2) DEFAULT 0,
                high_bidder_id BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                won_item_name  TEXT,
                won_item_value DECIMAL(15,2),
                created_at     TIMESTAMP DEFAULT NOW(),
                resolved_at    TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS case_auction_bids (
                id            SERIAL PRIMARY KEY,
                round_id      INTEGER NOT NULL REFERENCES case_auction_rounds(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
                amount        DECIMAL(15,2) NOT NULL,
                refunded      BOOLEAN DEFAULT FALSE,
                created_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_case_auction_bids_round ON case_auction_bids(round_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_case_auction_bids_user ON case_auction_bids(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_case_auction_rounds_status ON case_auction_rounds(status)")
    logger.info("✅ Live Case Auction tables ready")


async def recover_stale_case_auction_rounds():
    """Every previously-outbid bid was already auto-refunded at outbid time --
    only the CURRENT (unrefunded) high bidder's escrowed amount needs
    refunding for a round stuck mid-flight."""
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id, high_bidder_id, current_bid FROM case_auction_rounds WHERE status='bidding' FOR UPDATE"
            )
            for row in stale:
                round_id = row['id']
                if row['high_bidder_id'] is not None and float(row['current_bid']) > 0:
                    await shared.add_balance(row['high_bidder_id'], float(row['current_bid']), conn)
                    await conn.execute(
                        "UPDATE case_auction_bids SET refunded=TRUE WHERE round_id=$1 AND refunded=FALSE",
                        round_id
                    )
                await conn.execute(
                    "UPDATE case_auction_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                    round_id
                )
            if stale:
                logger.info(f"🔨 Recovered {len(stale)} stale Live Case Auction round(s), refunded the current high bidder")


# ============================================================
# ROOM
# ============================================================

class AuctionRoom:
    def __init__(self, round_id: int, case_id: str):
        self.round_id = round_id
        self.case_id = case_id
        self.status = 'bidding'
        self.current_bid = 0.0
        self.high_bidder_id: Optional[int] = None
        self.high_bidder_name: Optional[str] = None
        self.ws_set: Set[WebSocket] = set()
        self.ws_map: Dict[int, WebSocket] = {}
        self.task: Optional[asyncio.Task] = None
        self.bid_deadline: float = time.time() + BIDDING_SECS
        self.lock = asyncio.Lock()
        self.created_at = time.time()

    async def broadcast(self, msg: dict):
        dead = await broadcast_to_set(self.ws_set, convert_decimals(msg))
        self.ws_set -= dead

    def snapshot(self) -> dict:
        case = CASES.get(self.case_id, {})
        return {
            'round_id': self.round_id,
            'status': self.status,
            'case_id': self.case_id,
            'case_name': case.get('name'),
            'case_emoji': case.get('emoji'),
            'current_bid': self.current_bid,
            'min_next_bid': _min_next_bid(self.current_bid),
            'high_bidder_id': self.high_bidder_id,
            'high_bidder_name': self.high_bidder_name,
            'bid_deadline': self.bid_deadline,
        }

    async def run_bidding(self):
        while True:
            async with self.lock:
                if self.status != 'bidding':
                    return
                remaining = self.bid_deadline - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(1.0, max(0.05, remaining)))
            async with self.lock:
                if self.status != 'bidding':
                    return
            await self.broadcast({'type': 'tick', 'round_id': self.round_id,
                                   'seconds': max(0, int(self.bid_deadline - time.time()))})
        await self.resolve()

    async def resolve(self):
        async with self.lock:
            if self.status != 'bidding':
                return
            self.status = 'settled'
            await self.broadcast({'type': 'round_ending', 'round_id': self.round_id})

            if self.high_bidder_id is None:
                pool = await get_db()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE case_auction_rounds SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                        self.round_id
                    )
                self.status = 'cancelled'
                await self.broadcast({'type': 'cancelled', 'round_id': self.round_id, 'reason': 'no_bids'})
                async with _live_case_auction_registry_lock:
                    _live_case_auction_rooms.pop(self.round_id, None)
                return

            item = get_random_item(self.case_id)
            skin_img_file = item.get('image_filename') if item else None
            skin_img_url = f"/static/images/skins/{skin_img_file}" if skin_img_file else None

            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Payment is already escrowed from the winning bid -- no
                    # further balance change needed, just deliver the item.
                    await conn.execute("""
                        INSERT INTO inventory
                            (user_id, item_name, item_type, rarity, price, condition, is_stattrak, status, float_value, image_url)
                        VALUES ($1,$2,'weapon',$3,$4,$5,$6,'kept',$7,$8)
                    """, self.high_bidder_id, item['name'], item['rarity'], item['price'],
                        item.get('condition', 'Field-Tested'), item.get('is_stattrak', False),
                        item.get('float', 0.0), skin_img_url)
                    await conn.execute("""
                        UPDATE users SET total_opens = total_opens + 1,
                            total_golds = total_golds + $2
                        WHERE user_id = $1
                    """, self.high_bidder_id, 1 if item['rarity'] == 'Gold' else 0)
                    await conn.execute("""
                        UPDATE case_auction_rounds
                        SET status='settled', won_item_name=$1, won_item_value=$2, resolved_at=NOW()
                        WHERE id=$3
                    """, item['name'], item['price'], self.round_id)
                    await log_game(conn, self.high_bidder_id, 'live_case_auction', self.current_bid, item['price'], {
                        'round': self.round_id, 'case_id': self.case_id, 'won_item': item['name'],
                    })

            await self.broadcast({
                'type': 'result', 'round_id': self.round_id, 'winner_id': self.high_bidder_id,
                'winner_name': self.high_bidder_name, 'paid': self.current_bid,
                'item': {'name': item['name'], 'rarity': item['rarity'], 'value': item['price']},
                **self.snapshot(),
            })
            async with _live_case_auction_registry_lock:
                _live_case_auction_rooms.pop(self.round_id, None)


# ============================================================
# REGISTRY
# ============================================================

_live_case_auction_rooms: Dict[int, AuctionRoom] = {}
_live_case_auction_registry_lock = asyncio.Lock()


async def _get_or_create_open_room() -> AuctionRoom:
    async with _live_case_auction_registry_lock:
        for room in _live_case_auction_rooms.values():
            if room.status == 'bidding' and len(room.ws_set) < MAX_PLAYERS:
                return room
        case_id = secure_choice(list(CASES.keys()))
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO case_auction_rounds (status, case_id) VALUES ('bidding', $1) RETURNING id",
                case_id
            )
        room = AuctionRoom(row['id'], case_id)
        _live_case_auction_rooms[room.round_id] = room
        room.task = asyncio.create_task(room.run_bidding())
        return room


# ============================================================
# REST ROUTES
# ============================================================

class BidRequest(BaseModel):
    amount: float
    round_id: Optional[int] = None


@router.post("/bid")
async def place_bid(req: BidRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("live-case-auction")
    amount = round(min(max(req.amount, MIN_BID), MAX_BID), 2)
    await ensure_user_exists(user_id)

    if req.round_id is not None:
        async with _live_case_auction_registry_lock:
            room = _live_case_auction_rooms.get(req.round_id)
        if not room:
            raise HTTPException(404, "Auction not found or already closed")
    else:
        room = await _get_or_create_open_room()

    async with room.lock:
        if room.status != 'bidding':
            raise HTTPException(400, "This auction has already closed")
        min_next = _min_next_bid(room.current_bid)
        if amount < min_next:
            raise HTTPException(400, f"Minimum next bid is ${min_next:.2f}")
        if room.high_bidder_id == user_id:
            raise HTTPException(400, "You're already the high bidder")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await deduct_balance(user_id, amount, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'

                # Refund the previous high bidder's escrowed amount in the
                # SAME transaction as the new bidder's deduction -- atomic
                # hand-off, no window where money is simultaneously held by
                # both bidders or by neither.
                if room.high_bidder_id is not None:
                    await shared.add_balance(room.high_bidder_id, room.current_bid, conn)
                    await conn.execute(
                        "UPDATE case_auction_bids SET refunded=TRUE WHERE round_id=$1 AND user_id=$2 AND refunded=FALSE",
                        room.round_id, room.high_bidder_id
                    )

                await conn.execute(
                    "INSERT INTO case_auction_bids (round_id, user_id, amount) VALUES ($1,$2,$3)",
                    room.round_id, user_id, amount
                )
                await conn.execute(
                    "UPDATE case_auction_rounds SET current_bid=$1, high_bidder_id=$2 WHERE id=$3",
                    amount, user_id, room.round_id
                )

        room.current_bid = amount
        room.high_bidder_id = user_id
        room.high_bidder_name = username
        room.bid_deadline = time.time() + BIDDING_SECS

        await room.broadcast({'type': 'new_bid', 'round_id': room.round_id, 'user_id': user_id,
                               'username': username, 'amount': amount, **room.snapshot()})
        result = {"success": True, "round_id": room.round_id, **room.snapshot()}

    return convert_decimals(result)


@router.get("/rounds")
async def list_rounds():
    async with _live_case_auction_registry_lock:
        rooms = list(_live_case_auction_rooms.values())
    result = {"rounds": [
        {**r.snapshot(), 'viewer_count': len(r.ws_map)}
        for r in rooms if r.status == 'bidding'
    ]}
    return convert_decimals(result)


@router.get("/rounds/{round_id}")
async def get_round(round_id: int):
    async with _live_case_auction_registry_lock:
        room = _live_case_auction_rooms.get(round_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        rnd = await conn.fetchrow("SELECT * FROM case_auction_rounds WHERE id=$1", round_id)
        if not rnd:
            raise HTTPException(404, "Auction not found")
    case = CASES.get(rnd['case_id'], {})
    result = {
        'round_id': round_id, 'status': rnd['status'], 'case_id': rnd['case_id'],
        'case_name': case.get('name'), 'case_emoji': case.get('emoji'),
        'current_bid': rnd['current_bid'], 'high_bidder_id': rnd['high_bidder_id'],
        'won_item_name': rnd['won_item_name'], 'won_item_value': rnd['won_item_value'],
    }
    return convert_decimals(result)


# ============================================================
# WEBSOCKET
# ============================================================

@router.websocket("/ws/{round_id}")
async def live_case_auction_ws(websocket: WebSocket, round_id: int):
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

    async with _live_case_auction_registry_lock:
        room = _live_case_auction_rooms.get(round_id)
    if not room:
        try:
            await websocket.send_json({'type': 'no_room', 'round_id': round_id})
        except Exception:
            pass
        await websocket.close()
        return

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
        room.ws_map.pop(user_id, None)
