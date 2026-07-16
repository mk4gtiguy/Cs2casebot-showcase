# ============================================================
# routes/live_blackjack.py
# CS2CaseBot | Live Blackjack
#
# A true multi-seat table (up to 7 players), modeled directly on
# routes/games_poker.py's HoldemRoom turn-order/timeout pattern --
# NOT a simplified synchronized-parallel-hands game like the other
# three Session 5 games. Hand evaluation reuses the exact pure
# functions from the solo Blackjack (routes/games_hard.py):
# bj_hand_value / is_blackjack / dealer_play / new_bj_shoe. Only the
# stateful multi-seat table/turn logic here is new -- the existing
# solo _bj_sessions per-user in-memory state does not carry over.
#
# Split is capped at ONE split (2 hands max) per player, a deliberate
# simplification vs solo Blackjack's up-to-4-hand split, to keep the
# turn loop's per-seat action count bounded.
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
    HOUSE_EDGE,
)
from routes.games_hard import new_bj_shoe, bj_hand_value, is_blackjack, dealer_play, log_game

router = APIRouter(prefix="/api/games/live-blackjack", tags=["live-blackjack"])

MIN_BET       = 50
MAX_BET       = 750_000
MAX_SEATS     = 7   # real casino table convention
BETTING_SECS  = 10  # short window to let more players join before dealing
INSURANCE_SECS = 10
TURN_TIMEOUT_SECS = 30   # matches Poker's HoldemRoom exactly


def clamp_bet(amount: float) -> float:
    return shared_clamp_bet(amount, MIN_BET, MAX_BET)


# ============================================================
# TABLE SETUP
# ============================================================

async def init_live_blackjack_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS live_blackjack_tables (
                id            SERIAL PRIMARY KEY,
                status        TEXT DEFAULT 'waiting'
                              CHECK (status IN ('waiting','betting','dealing','insurance','playing','dealer_turn','settled','cancelled')),
                dealer_hand   JSONB,
                created_at    TIMESTAMP DEFAULT NOW(),
                resolved_at   TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS live_blackjack_seats (
                id            SERIAL PRIMARY KEY,
                table_id      INTEGER NOT NULL REFERENCES live_blackjack_tables(id) ON DELETE CASCADE,
                user_id       BIGINT  NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
                seat_number   INTEGER NOT NULL,
                hand          JSONB,
                split_hand    JSONB,
                bet           DECIMAL(15,2) NOT NULL,
                double_down   BOOLEAN DEFAULT FALSE,
                insurance_bet DECIMAL(15,2) DEFAULT 0,
                status        TEXT DEFAULT 'active',
                payout        DECIMAL(15,2),
                created_at    TIMESTAMP DEFAULT NOW(),
                UNIQUE(table_id, seat_number)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_live_blackjack_seats_table ON live_blackjack_seats(table_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_live_blackjack_seats_user ON live_blackjack_seats(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_live_blackjack_tables_status ON live_blackjack_tables(status)")
    logger.info("✅ Live Blackjack tables ready")


async def recover_stale_live_blackjack_tables():
    """Any table left in a non-terminal status at startup refunds every
    seat's bet + insurance_bet (the in-memory BlackjackRoom driving it is
    gone after a restart, so mid-hand state can't be trusted)."""
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch("""
                SELECT id FROM live_blackjack_tables
                WHERE status NOT IN ('settled','cancelled') FOR UPDATE
            """)
            for row in stale:
                table_id = row['id']
                seats = await conn.fetch(
                    "SELECT user_id, bet, insurance_bet FROM live_blackjack_seats WHERE table_id=$1 AND payout IS NULL",
                    table_id
                )
                for s in seats:
                    refund = float(s['bet']) + float(s['insurance_bet'] or 0)
                    await shared.add_balance(s['user_id'], refund, conn)
                await conn.execute(
                    "UPDATE live_blackjack_tables SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                    table_id
                )
            if stale:
                logger.info(f"♠️ Recovered {len(stale)} stale Live Blackjack table(s), refunded all seated bets")


# ============================================================
# ROOM
# ============================================================

class BlackjackRoom:
    def __init__(self, table_id: int):
        self.table_id = table_id
        self.status = 'waiting'
        self.seats: Dict[int, int] = {}       # seat_number -> user_id
        self.players: Dict[int, Dict] = {}    # user_id -> player state (see _join)
        self.dealer_hand: List[Dict] = []
        self.shoe: List[Dict] = []
        self.ws_set: Set = set()
        self.ws_map: Dict[int, object] = {}
        self.task: Optional[asyncio.Task] = None
        self.betting_deadline: Optional[float] = None
        self.action_seat: Optional[int] = None
        self.action_uid: Optional[int] = None
        self.action_hand_idx: int = 0
        self.waiting_for_human = False
        self.lock = asyncio.Lock()
        self.created_at = time.time()

    async def broadcast(self, msg: dict):
        dead = await broadcast_to_set(self.ws_set, convert_decimals(msg))
        self.ws_set -= dead

    async def send_to(self, user_id: int, msg: dict):
        ws = self.ws_map.get(user_id)
        if ws:
            try:
                await ws.send_json(convert_decimals(msg))
            except Exception:
                pass

    def _hand_public(self, hand: List[Dict], hide_hole: bool = False) -> List[Dict]:
        if hide_hole and len(hand) >= 2:
            return [hand[0], {'rank': '?', 'suit': '?', 'display': '??'}] + hand[2:]
        return hand

    def snapshot(self, for_user: Optional[int] = None) -> dict:
        hide_dealer_hole = self.status in ('dealing', 'insurance', 'playing')
        return {
            'table_id': self.table_id,
            'status': self.status,
            'betting_deadline': self.betting_deadline,
            'dealer_hand': self._hand_public(self.dealer_hand, hide_dealer_hole),
            'action_seat': self.action_seat,
            'action_uid': self.action_uid,
            'action_hand_idx': self.action_hand_idx,
            'seats': [
                {
                    'seat': seat, 'user_id': uid, 'username': self.players[uid]['username'],
                    'bet': self.players[uid]['bet'],
                    'hands': self.players[uid]['hands'],
                    'hand_status': self.players[uid]['hand_status'],
                    'insurance': self.players[uid]['insurance'],
                }
                for seat, uid in sorted(self.seats.items())
            ],
        }

    # ── Lifecycle ──────────────────────────────────────────

    async def run_betting(self):
        for sec in range(BETTING_SECS, 0, -1):
            await asyncio.sleep(1)
            async with self.lock:
                if self.status != 'betting':
                    return
            await self.broadcast({'type': 'betting_tick', 'table_id': self.table_id, 'seconds': sec - 1})
        await self._deal()

    async def _deal(self):
        async with self.lock:
            if self.status != 'betting' or not self.players:
                return
            self.status = 'dealing'
            self.shoe = new_bj_shoe(decks=6)
            self.dealer_hand = [self.shoe.pop(), self.shoe.pop()]
            for uid in self.players:
                p = self.players[uid]
                p['hands'] = [[self.shoe.pop(), self.shoe.pop()]]
                p['hand_bets'] = [p['bet']]
                p['hand_status'] = ['active']
                p['can_split'] = True

            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for uid, p in self.players.items():
                        await conn.execute(
                            "UPDATE live_blackjack_seats SET hand=$1 WHERE table_id=$2 AND user_id=$3",
                            p['hands'][0], self.table_id, uid
                        )
                    await conn.execute(
                        "UPDATE live_blackjack_tables SET dealer_hand=$1, status='dealing' WHERE id=$2",
                        self.dealer_hand, self.table_id
                    )

            # Mark player blackjacks immediately (they don't get a turn)
            for uid, p in self.players.items():
                if is_blackjack(p['hands'][0]):
                    p['hand_status'][0] = 'blackjack'

            await self.broadcast({'type': 'dealt', 'table_id': self.table_id, 'round': self.snapshot()})

        if self.dealer_hand[0]['rank'] == 'A':
            await self._run_insurance_phase()
        else:
            await self._run_playing_phase()

    async def _run_insurance_phase(self):
        async with self.lock:
            self.status = 'insurance'
        await self.broadcast({'type': 'insurance_open', 'table_id': self.table_id, 'seconds': INSURANCE_SECS})
        await asyncio.sleep(INSURANCE_SECS)
        async with self.lock:
            if self.status != 'insurance':
                return
        await self._run_playing_phase()

    async def buy_insurance(self, user_id: int) -> float:
        async with self.lock:
            if self.status != 'insurance':
                raise HTTPException(400, "Insurance is not open right now")
            p = self.players.get(user_id)
            if not p or p['insurance'] > 0:
                raise HTTPException(400, "Not eligible for insurance")
            cost = round(p['bet'] / 2, 2)
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    if not await deduct_balance(user_id, cost, conn):
                        raise HTTPException(400, "Insufficient balance for insurance")
                    await conn.execute(
                        "UPDATE live_blackjack_seats SET insurance_bet=$1 WHERE table_id=$2 AND user_id=$3",
                        cost, self.table_id, user_id
                    )
            p['insurance'] = cost
        await self.broadcast({'type': 'insurance_bought', 'table_id': self.table_id, 'user_id': user_id, 'cost': cost})
        return cost

    async def _run_playing_phase(self):
        async with self.lock:
            self.status = 'playing'
        await self.broadcast({'type': 'playing_start', 'table_id': self.table_id})

        for seat in sorted(self.seats.keys()):
            uid = self.seats[seat]
            p = self.players.get(uid)
            if not p:
                continue
            hand_idx = 0
            while hand_idx < len(p['hands']):
                # Inner loop (not a single call) so a 'split' on this hand --
                # which leaves hand_idx's status at 'active' and appends a
                # NEW hand rather than resolving this one -- correctly gives
                # the player another turn on the SAME hand before advancing,
                # instead of skipping straight to the newly split-off hand.
                while p['hand_status'][hand_idx] == 'active':
                    await self._play_seat_hand(uid, seat, hand_idx)
                hand_idx += 1

        await self._dealer_turn_and_settle()

    async def _play_seat_hand(self, uid: int, seat: int, hand_idx: int):
        p = self.players.get(uid)
        if not p or p['hand_status'][hand_idx] != 'active':
            return

        self.action_uid = uid
        self.action_seat = seat
        self.action_hand_idx = hand_idx
        self.waiting_for_human = True

        val, _ = bj_hand_value(p['hands'][hand_idx])
        can_double = len(p['hands'][hand_idx]) == 2
        can_split = (p['can_split'] and len(p['hands']) == 1 and len(p['hands'][0]) == 2
                     and p['hands'][0][0]['rank'] == p['hands'][0][1]['rank'])

        await self.send_to(uid, {
            'type': 'your_turn', 'table_id': self.table_id, 'hand_idx': hand_idx,
            'hand_value': val, 'can_double': can_double, 'can_split': can_split,
        })
        await self.broadcast({'type': 'turn_change', 'table_id': self.table_id, 'seat': seat, 'user_id': uid, 'hand_idx': hand_idx})

        try:
            deadline = time.time() + TURN_TIMEOUT_SECS
            while self.waiting_for_human and time.time() < deadline:
                await asyncio.sleep(0.1)
            if self.waiting_for_human:
                # Timeout default: stand -- no fold equivalent once cards are
                # dealt and the bet is committed (matches how a distracted
                # real-table player just gets whatever the dealer's forced
                # play does to their hand).
                await self._apply_action(uid, 'stand', hand_idx)
        finally:
            self.waiting_for_human = False

    async def process_human_action(self, uid: int, action: str, hand_idx: int):
        if not self.waiting_for_human or uid != self.action_uid or hand_idx != self.action_hand_idx:
            raise HTTPException(400, "It's not your turn")
        await self._apply_action(uid, action, hand_idx)

    async def _apply_action(self, uid: int, action: str, hand_idx: int):
        p = self.players.get(uid)
        if not p:
            self.waiting_for_human = False
            return
        hand = p['hands'][hand_idx]

        if action == 'hit':
            hand.append(self.shoe.pop())
            val, _ = bj_hand_value(hand)
            if val > 21:
                p['hand_status'][hand_idx] = 'bust'
                self.waiting_for_human = False
            elif val == 21:
                # Auto-stand on 21 -- no further action is possible/beneficial,
                # and leaving status at 'active' would re-prompt this hand
                # forever since the outer playing-phase loop only advances
                # once status leaves 'active'.
                p['hand_status'][hand_idx] = 'stand'
                self.waiting_for_human = False
            await self.broadcast({'type': 'action_taken', 'table_id': self.table_id, 'user_id': uid,
                                   'hand_idx': hand_idx, 'action': 'hit', 'hand': hand, 'value': val})

        elif action == 'stand':
            p['hand_status'][hand_idx] = 'stand'
            self.waiting_for_human = False
            await self.broadcast({'type': 'action_taken', 'table_id': self.table_id, 'user_id': uid,
                                   'hand_idx': hand_idx, 'action': 'stand'})

        elif action == 'double':
            if len(hand) != 2:
                raise HTTPException(400, "Can only double on your first two cards")
            extra = p['hand_bets'][hand_idx]
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    if not await deduct_balance(uid, extra, conn):
                        raise HTTPException(400, "Insufficient balance to double down")
                    await conn.execute(
                        "UPDATE live_blackjack_seats SET double_down=TRUE WHERE table_id=$1 AND user_id=$2",
                        self.table_id, uid
                    )
            p['hand_bets'][hand_idx] *= 2
            hand.append(self.shoe.pop())
            val, _ = bj_hand_value(hand)
            p['hand_status'][hand_idx] = 'bust' if val > 21 else 'stand'
            self.waiting_for_human = False
            await self.broadcast({'type': 'action_taken', 'table_id': self.table_id, 'user_id': uid,
                                   'hand_idx': hand_idx, 'action': 'double', 'hand': hand, 'value': val})

        elif action == 'split':
            if not (p['can_split'] and len(p['hands']) == 1 and len(hand) == 2
                    and hand[0]['rank'] == hand[1]['rank']):
                raise HTTPException(400, "Split not available")
            extra = p['hand_bets'][hand_idx]
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    if not await deduct_balance(uid, extra, conn):
                        raise HTTPException(400, "Insufficient balance to split")
            second_card = hand.pop()
            hand.append(self.shoe.pop())
            new_hand = [second_card, self.shoe.pop()]
            p['hands'].append(new_hand)
            p['hand_bets'].append(extra)
            p['hand_status'].append('active')
            p['can_split'] = False
            async with (await get_db()).acquire() as conn:
                await conn.execute(
                    "UPDATE live_blackjack_seats SET hand=$1, split_hand=$2 WHERE table_id=$3 AND user_id=$4",
                    p['hands'][0], p['hands'][1], self.table_id, uid
                )
            await self.broadcast({'type': 'action_taken', 'table_id': self.table_id, 'user_id': uid,
                                   'hand_idx': hand_idx, 'action': 'split', 'hands': p['hands']})
            # This hand (index 0) still needs to be played out post-split;
            # the newly created hand (index 1) is picked up by the outer
            # _run_playing_phase loop once this call returns.
            self.waiting_for_human = False

        else:
            raise HTTPException(400, f"Unknown action: {action}")

    # ── Dealer turn + settle ───────────────────────────────

    async def _dealer_turn_and_settle(self):
        async with self.lock:
            if self.status not in ('playing', 'insurance'):
                return
            self.status = 'dealer_turn'
            self.action_uid = None
            self.action_seat = None

            any_live_hand = any(
                any(s in ('active', 'stand') for s in p['hand_status'])
                for p in self.players.values()
            )
            if any_live_hand:
                self.dealer_hand = dealer_play(self.dealer_hand, self.shoe)
            d_val, _ = bj_hand_value(self.dealer_hand)
            d_bust = d_val > 21
            d_bj = is_blackjack(self.dealer_hand)

            await self.broadcast({'type': 'dealer_turn', 'table_id': self.table_id,
                                   'dealer_hand': self.dealer_hand, 'dealer_value': d_val})

            results = []
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for uid, p in self.players.items():
                        seat_total_win = 0.0
                        hand_results = []
                        for i, hand in enumerate(p['hands']):
                            pv, _ = bj_hand_value(hand)
                            bet_i = p['hand_bets'][i]
                            status = p['hand_status'][i]
                            p_bust = status == 'bust'
                            p_bj = status == 'blackjack' and len(p['hands']) == 1

                            if p_bust:
                                win_i, res = 0, 'bust'
                            elif d_bj and not p_bj:
                                win_i, res = 0, 'loss'
                            elif p_bj and not d_bj:
                                win_i, res = shared.apply_house(bet_i * 2.5, HOUSE_EDGE), 'blackjack'
                            elif p_bj and d_bj:
                                win_i, res = bet_i, 'push'
                            elif d_bust or pv > d_val:
                                win_i, res = shared.apply_house(bet_i * 2, HOUSE_EDGE), 'win'
                            elif pv == d_val:
                                win_i, res = bet_i, 'push'
                            else:
                                win_i, res = 0, 'loss'

                            seat_total_win += win_i
                            hand_results.append({'hand': hand, 'value': pv, 'bet': bet_i, 'win': win_i, 'result': res})

                        if p['insurance'] and d_bj:
                            seat_total_win += p['insurance'] * 3

                        if seat_total_win:
                            seat_total_win = await credit_win(uid, seat_total_win, conn)

                        total_bet = sum(p['hand_bets'])
                        await log_game(conn, uid, 'live_blackjack', total_bet, seat_total_win, {
                            'table': self.table_id, 'dealer_hand': self.dealer_hand, 'hands': hand_results,
                        })
                        await conn.execute(
                            "UPDATE live_blackjack_seats SET payout=$1 WHERE table_id=$2 AND user_id=$3",
                            seat_total_win, self.table_id, uid
                        )
                        results.append({
                            'user_id': uid, 'username': p['username'], 'hands': hand_results,
                            'total_win': seat_total_win,
                        })
                    await conn.execute("""
                        UPDATE live_blackjack_tables SET status='settled', dealer_hand=$1, resolved_at=NOW()
                        WHERE id=$2
                    """, self.dealer_hand, self.table_id)

            self.status = 'settled'
            await self.broadcast({
                'type': 'result', 'table_id': self.table_id, 'dealer_hand': self.dealer_hand,
                'dealer_value': d_val, 'players': results,
            })
            async with _live_blackjack_registry_lock:
                _live_blackjack_rooms.pop(self.table_id, None)

    # ── Leave ──────────────────────────────────────────────

    async def leave(self, user_id: int):
        async with self.lock:
            p = self.players.get(user_id)
            if not p:
                raise HTTPException(400, "Not seated at this table")
            if self.status in ('waiting', 'betting'):
                pool = await get_db()
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await shared.add_balance(user_id, p['bet'], conn)
                        await conn.execute(
                            "DELETE FROM live_blackjack_seats WHERE table_id=$1 AND user_id=$2",
                            self.table_id, user_id
                        )
                seat = p['seat']
                del self.players[user_id]
                del self.seats[seat]
            else:
                # Mid-hand: force an immediate stand on whatever hand is in
                # progress rather than a refund -- once cards are dealt the
                # bet is committed, same principle as Poker chips already
                # in the pot. Matches Poker's HoldemRoom leave behavior.
                if self.waiting_for_human and self.action_uid == user_id:
                    self.waiting_for_human = False
                if 'hand_status' in p:
                    for i, s in enumerate(p['hand_status']):
                        if s == 'active':
                            p['hand_status'][i] = 'stand'
        await self.broadcast({'type': 'player_left', 'table_id': self.table_id, 'user_id': user_id})


# ============================================================
# REGISTRY
# ============================================================

_live_blackjack_rooms: Dict[int, BlackjackRoom] = {}
_live_blackjack_registry_lock = asyncio.Lock()


async def _get_or_create_open_table() -> BlackjackRoom:
    async with _live_blackjack_registry_lock:
        for room in _live_blackjack_rooms.values():
            if room.status in ('waiting', 'betting') and len(room.seats) < MAX_SEATS:
                return room
        pool = await get_db()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO live_blackjack_tables (status) VALUES ('waiting') RETURNING id"
            )
        room = BlackjackRoom(row['id'])
        _live_blackjack_rooms[room.table_id] = room
        return room


async def create_private_room(participant_user_ids: list, bet: float) -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py). Scoped to exactly 2 players, auto-assigned to
    seats 0 and 1 in participant_user_ids order -- stakes both
    atomically in one transaction and starts the betting countdown
    directly, skipping the open-lobby /join flow
    (_get_or_create_open_table) entirely."""
    if len(participant_user_ids) != 2:
        raise HTTPException(400, "Live Blackjack friend challenges need exactly 2 players")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO live_blackjack_tables (status) VALUES ('betting') RETURNING id"
            )
            table_id = row['id']

            seats: Dict[int, int] = {}
            players: Dict[int, Dict] = {}
            for seat, uid in enumerate(participant_user_ids):
                if not await deduct_balance(uid, bet, conn):
                    raise HTTPException(400, f"Player {uid} has insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                username = user_row['username'] if user_row else f'Player {uid}'
                await conn.execute("""
                    INSERT INTO live_blackjack_seats (table_id, user_id, seat_number, bet)
                    VALUES ($1,$2,$3,$4)
                """, table_id, uid, seat, bet)
                seats[seat] = uid
                players[uid] = {
                    'seat': seat, 'username': username, 'bet': bet,
                    'hands': [], 'hand_bets': [], 'hand_status': [], 'insurance': 0, 'can_split': True,
                }

    room = BlackjackRoom(table_id)
    room.seats = seats
    room.players = players
    room.status = 'betting'
    room.betting_deadline = time.time() + BETTING_SECS
    async with _live_blackjack_registry_lock:
        _live_blackjack_rooms[table_id] = room
    room.task = asyncio.create_task(room.run_betting())
    await room.broadcast({'type': 'table_start', 'table_id': table_id, 'round': room.snapshot()})
    return table_id


# ============================================================
# REST ROUTES
# ============================================================

class JoinRequest(BaseModel):
    amount: float
    table_id: Optional[int] = None


class ActionRequest(BaseModel):
    action: str   # hit | stand | double | split
    hand_idx: int = 0


@router.post("/join")
async def join_table(req: JoinRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("live-blackjack")
    bet = clamp_bet(req.amount)
    await ensure_user_exists(user_id)

    if req.table_id is not None:
        async with _live_blackjack_registry_lock:
            room = _live_blackjack_rooms.get(req.table_id)
        if not room:
            raise HTTPException(404, "Table not found or already closed")
    else:
        room = await _get_or_create_open_table()

    async with room.lock:
        if room.status not in ('waiting', 'betting'):
            raise HTTPException(400, "This table is mid-hand — wait for the next round")
        if user_id in room.players:
            raise HTTPException(400, "You're already seated at this table")
        if len(room.seats) >= MAX_SEATS:
            raise HTTPException(400, "This table is full")

        seat = next(s for s in range(MAX_SEATS) if s not in room.seats)

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                username = user_row['username'] if user_row else f'Player {user_id}'
                await conn.execute("""
                    INSERT INTO live_blackjack_seats (table_id, user_id, seat_number, bet)
                    VALUES ($1,$2,$3,$4)
                """, room.table_id, user_id, seat, bet)

        room.seats[seat] = user_id
        room.players[user_id] = {
            'seat': seat, 'username': username, 'bet': bet,
            'hands': [], 'hand_bets': [], 'hand_status': [], 'insurance': 0, 'can_split': True,
        }

        if room.status == 'waiting':
            room.status = 'betting'
            room.betting_deadline = time.time() + BETTING_SECS
            room.task = asyncio.create_task(room.run_betting())

        await room.broadcast({'type': 'player_joined', 'table_id': room.table_id, 'round': room.snapshot()})
        result = {"success": True, "table_id": room.table_id, "seat": seat, "round": room.snapshot()}

    return convert_decimals(result)


@router.post("/action")
async def take_action(req: ActionRequest, request: Request):
    user_id = await require_auth(request)
    async with _live_blackjack_registry_lock:
        room = None
        for r in _live_blackjack_rooms.values():
            if user_id in r.players:
                room = r
                break
    if not room:
        raise HTTPException(404, "You're not seated at any active table")
    await room.process_human_action(user_id, req.action, req.hand_idx)
    return {"success": True}


@router.post("/insurance")
async def take_insurance(request: Request):
    user_id = await require_auth(request)
    async with _live_blackjack_registry_lock:
        room = None
        for r in _live_blackjack_rooms.values():
            if user_id in r.players:
                room = r
                break
    if not room:
        raise HTTPException(404, "You're not seated at any active table")
    cost = await room.buy_insurance(user_id)
    return {"success": True, "cost": cost}


@router.post("/leave")
async def leave_table(request: Request):
    user_id = await require_auth(request)
    async with _live_blackjack_registry_lock:
        room = None
        for r in _live_blackjack_rooms.values():
            if user_id in r.players:
                room = r
                break
    if not room:
        raise HTTPException(404, "You're not seated at any active table")
    await room.leave(user_id)
    return {"success": True}


@router.get("/tables")
async def list_tables():
    async with _live_blackjack_registry_lock:
        rooms = list(_live_blackjack_rooms.values())
    result = {"tables": [
        {
            'table_id': r.table_id, 'status': r.status,
            'seat_count': len(r.seats), 'max_seats': MAX_SEATS,
        }
        for r in rooms if r.status in ('waiting', 'betting')
    ]}
    return convert_decimals(result)


@router.get("/tables/{table_id}")
async def get_table(table_id: int):
    async with _live_blackjack_registry_lock:
        room = _live_blackjack_rooms.get(table_id)
    if room:
        return convert_decimals(room.snapshot())

    pool = await get_db()
    async with pool.acquire() as conn:
        t = await conn.fetchrow("SELECT * FROM live_blackjack_tables WHERE id=$1", table_id)
        if not t:
            raise HTTPException(404, "Table not found")
        seats = await conn.fetch("""
            SELECT s.*, u.username FROM live_blackjack_seats s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.table_id=$1 ORDER BY s.seat_number
        """, table_id)
    result = {
        'table_id': table_id, 'status': t['status'], 'dealer_hand': t['dealer_hand'],
        'seats': [
            {
                'seat': s['seat_number'], 'user_id': s['user_id'], 'username': s['username'],
                'hand': s['hand'], 'split_hand': s['split_hand'], 'bet': s['bet'], 'payout': s['payout'],
            }
            for s in seats
        ],
    }
    return convert_decimals(result)


# ============================================================
# WEBSOCKET
# ============================================================

@router.websocket("/ws/{table_id}")
async def live_blackjack_ws(websocket: WebSocket, table_id: int):
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

    async with _live_blackjack_registry_lock:
        room = _live_blackjack_rooms.get(table_id)
    if not room:
        try:
            await websocket.send_json({'type': 'no_room', 'table_id': table_id})
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
