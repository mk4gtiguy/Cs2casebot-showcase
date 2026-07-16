# ============================================================
# routes/games_poker.py
# CS2CaseBot | Poker Backend
#
# Texas Hold'em: 4-player WebSocket rooms, bot AI with
#   personality-driven betting, full hand evaluation,
#   side pots, blinds, full action flow.
# Video Poker: Jacks-or-Better solo mode, draw mechanic.
# ============================================================

import json
import asyncio
import time
from datetime import datetime
from itertools import combinations
from typing import Dict, Set, Optional, List, Tuple, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from pydantic import BaseModel

import shared
from shared import (
    logger, get_db, require_auth, get_user_id_from_session,
    ensure_user_exists, deduct_balance, add_balance,
    convert_decimals, broadcast_to_set,
    secure_random, secure_randint, secure_choice, secure_shuffle,
    apply_house_edge, HOUSE_EDGE_POKER,
    get_vip_status, apply_vip_boost, credit_win, require_game_enabled,
)

router = APIRouter(prefix="/api/games/poker", tags=["games-poker"])

HOUSE_EDGE   = 0.03   # 3% rake on every pot
MIN_BET      = 100
MAX_BET      = 750_000
MAX_PLAYERS  = 4

def clamp_bet(v: float) -> float:
    return shared.clamp_bet(v, MIN_BET, MAX_BET)

def apply_house(raw: float) -> float:
    return shared.apply_house(raw, HOUSE_EDGE)

async def log_game(conn, user_id: int, game_type: str,
                   bet: float, win: float, meta: dict = None):
    # Fix: poker now bumps earn_money quest progress like every other game
    # (previously never did, unlike easy/medium/hard/heavy).
    await shared.log_game(conn, user_id, game_type, bet, win, meta,
                          win_inclusive=True, update_earn_quest=True)

# ============================================================
# CARD ENGINE
# ============================================================

RANKS  = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']
SUITS  = ['♠','♥','♦','♣']
RANK_V = {r: i for i, r in enumerate(RANKS)}   # '2'=0 … 'A'=12

def new_deck() -> List[Dict]:
    deck = [{'rank': r, 'suit': s,
             'display': r + s,
             'value': RANK_V[r]}
            for r in RANKS for s in SUITS]
    deck[:] = secure_shuffle(deck)
    return deck

# ── Hand evaluator ───────────────────────────────────────────
# Returns (rank_int, tiebreaker_list) — higher is better.
# rank_int: 8=straight flush, 7=quads, 6=full house, 5=flush,
#           4=straight, 3=trips, 2=two pair, 1=pair, 0=high card

HAND_NAMES = {
    8: 'Straight Flush', 7: 'Four of a Kind', 6: 'Full House',
    5: 'Flush',          4: 'Straight',        3: 'Three of a Kind',
    2: 'Two Pair',       1: 'Pair',             0: 'High Card',
}

def _counts(cards: List[Dict]) -> Dict[int, int]:
    c: Dict[int, int] = {}
    for card in cards:
        v = card['value']
        c[v] = c.get(v, 0) + 1
    return c

def _is_flush(cards: List[Dict]) -> bool:
    return len({c['suit'] for c in cards}) == 1

def _straight_high(cards: List[Dict]) -> int:
    """Return the high card value of a straight, or -1."""
    vals = sorted({c['value'] for c in cards}, reverse=True)
    if len(vals) < 5:
        return -1
    # Normal straight
    if vals[0] - vals[-1] == 4 and len(vals) == 5:
        return vals[0]
    # Wheel (A-2-3-4-5): A=12, then 0,1,2,3
    if set(vals) == {12, 0, 1, 2, 3}:
        return 3   # 5-high straight
    return -1

def evaluate_5(cards: List[Dict]) -> Tuple[int, List[int]]:
    """Evaluate exactly 5 cards. Returns (rank, tiebreakers)."""
    vals = sorted([c['value'] for c in cards], reverse=True)
    cnt  = _counts(cards)
    groups = sorted(cnt.items(), key=lambda x: (x[1], x[0]), reverse=True)
    freq   = [g[1] for g in groups]
    gvals  = [g[0] for g in groups]
    sf  = _is_flush(cards)
    sh  = _straight_high(cards)

    if sf and sh >= 0:
        return 8, [sh]
    if freq[0] == 4:
        return 7, [gvals[0], gvals[1]]
    if freq[0] == 3 and freq[1] == 2:
        return 6, [gvals[0], gvals[1]]
    if sf:
        return 5, vals
    if sh >= 0:
        return 4, [sh]
    if freq[0] == 3:
        return 3, [gvals[0]] + sorted(gvals[1:], reverse=True)
    if freq[0] == 2 and freq[1] == 2:
        top = sorted([gvals[0], gvals[1]], reverse=True)
        kicker = gvals[2]
        return 2, top + [kicker]
    if freq[0] == 2:
        return 1, [gvals[0]] + sorted(gvals[1:], reverse=True)
    return 0, vals

def best_hand_from_7(cards: List[Dict]) -> Tuple[int, List[int], List[Dict]]:
    """Find best 5-card hand from up to 7 cards."""
    best_rank = -1
    best_tb:  List[int] = []
    best_combo: List[Dict] = []
    for combo in combinations(cards, 5):
        r, tb = evaluate_5(list(combo))
        if (r > best_rank) or (r == best_rank and tb > best_tb):
            best_rank  = r
            best_tb    = tb
            best_combo = list(combo)
    return best_rank, best_tb, best_combo

def compare_hands(a_cards: List[Dict], b_cards: List[Dict],
                  community: List[Dict]) -> int:
    """Returns 1 if a wins, -1 if b wins, 0 for tie."""
    ar, at, _ = best_hand_from_7(a_cards + community)
    br, bt, _ = best_hand_from_7(b_cards + community)
    if (ar, at) > (br, bt): return 1
    if (ar, at) < (br, bt): return -1
    return 0

# ============================================================
# BOT PERSONALITIES (poker)
# ============================================================

BOT_PROFILES = {
    'maniac': {
        'name':         '😈 The Maniac',
        'raise_freq':   0.70,   # raises 70% of the time when in
        'fold_thresh':  0.05,   # only folds worst 5% of hands
        'bluff_freq':   0.55,
        'bet_mult':     (2.5, 4.0),   # raise size range (× big blind)
    },
    'nit': {
        'name':         '🧊 The Nit',
        'raise_freq':   0.15,
        'fold_thresh':  0.55,   # folds over half the time
        'bluff_freq':   0.05,
        'bet_mult':     (2.0, 2.5),
    },
    'calling_station': {
        'name':         '📞 The Caller',
        'raise_freq':   0.10,
        'fold_thresh':  0.08,
        'bluff_freq':   0.10,
        'bet_mult':     (2.0, 3.0),
    },
    'shark': {
        'name':         '🦈 The Shark',
        'raise_freq':   0.40,
        'fold_thresh':  0.30,
        'bluff_freq':   0.25,
        'bet_mult':     (2.5, 3.5),
    },
}

BOT_NAMES = list(BOT_PROFILES.keys())

def bot_hand_strength(hole: List[Dict], community: List[Dict]) -> float:
    """Rough hand strength 0.0–1.0 for bot decision-making."""
    if not community:
        # Pre-flop: use hole card values
        vals = sorted([c['value'] for c in hole], reverse=True)
        # High cards, pairs, suited
        strength = (vals[0] + vals[1]) / 24.0   # max = 1.0 for AA
        if hole[0]['value'] == hole[1]['value']:
            strength += 0.20   # pocket pair bonus
        if hole[0]['suit'] == hole[1]['suit']:
            strength += 0.05   # suited bonus
        return min(1.0, strength)

    rank, _, _ = best_hand_from_7(hole + community)
    # Map hand rank (0–8) to 0.0–1.0
    return rank / 8.0

def bot_action(profile_key: str, strength: float,
               call_amount: float, pot: float,
               big_blind: float) -> Tuple[str, float]:
    """
    Returns ('fold'|'call'|'raise'|'check', amount).
    amount = total to put in (not raise increment).
    """
    p   = BOT_PROFILES[profile_key]
    low = p['bet_mult'][0]
    high = p['bet_mult'][1]
    raise_size = round(big_blind * (low + secure_random() * (high - low)), 0)

    # Bluff override
    is_bluffing = secure_random() < p['bluff_freq']

    effective_strength = min(1.0, strength + (0.4 if is_bluffing else 0))

    if call_amount == 0:
        # Check or raise
        if secure_random() < p['raise_freq'] * effective_strength:
            return 'raise', raise_size
        return 'check', 0

    if effective_strength < p['fold_thresh']:
        return 'fold', 0

    if secure_random() < p['raise_freq'] * effective_strength:
        return 'raise', max(call_amount + max(call_amount, big_blind), raise_size)

    return 'call', call_amount

# ============================================================
# TEXAS HOLD'EM ROOM
# ============================================================

class PokerPlayer:
    def __init__(self, user_id: int, username: str, chips: float,
                 seat: int, is_bot: bool = False,
                 bot_profile: str = 'shark'):
        self.user_id     = user_id
        self.username    = username
        self.chips       = chips
        self.seat        = seat
        self.is_bot      = is_bot
        self.bot_profile = bot_profile
        self.hole_cards: List[Dict] = []
        self.bet_this_street = 0.0
        self.total_bet       = 0.0
        self.status          = 'active'   # active|folded|allin|sitting_out
        self.is_dealer       = False
        self.is_sb           = False
        self.is_bb           = False

    def to_dict(self, show_cards: bool = False) -> dict:
        return {
            'user_id':    self.user_id,
            'username':   self.username,
            'chips':      round(self.chips, 2),
            'seat':       self.seat,
            'is_bot':     self.is_bot,
            'bot_name':   BOT_PROFILES[self.bot_profile]['name'] if self.is_bot else None,
            'status':     self.status,
            'bet':        round(self.bet_this_street, 2),
            'total_bet':  round(self.total_bet, 2),
            'is_dealer':  self.is_dealer,
            'is_sb':      self.is_sb,
            'is_bb':      self.is_bb,
            'hole_cards': self.hole_cards if show_cards else (
                [{'display': '🂠'}, {'display': '🂠'}] if self.hole_cards else []
            ),
            'card_count': len(self.hole_cards),
        }


class HoldemRoom:
    SMALL_BLIND_FRAC = 0.05   # 5% of buy-in as small blind
    BIG_BLIND_FRAC   = 0.10   # 10% of buy-in as big blind
    BOT_ACTION_DELAY = 1.5    # seconds between bot actions
    MAX_ROUNDS       = 30     # auto-close table after N rounds

    def __init__(self, room_code: str, buy_in: float):
        self.room_code   = room_code
        self.buy_in      = buy_in
        self.small_blind = max(50, round(buy_in * self.SMALL_BLIND_FRAC, -1))
        self.big_blind   = self.small_blind * 2

        self.players:  Dict[int, PokerPlayer] = {}
        self.seats:    Dict[int, int]          = {}   # seat → user_id
        self.ws_set:   Set[WebSocket]          = set()
        self.ws_map:   Dict[int, WebSocket]    = {}

        self.phase       = 'waiting'   # waiting|pre-flop|flop|turn|river|showdown
        self.community:  List[Dict]    = []
        self.deck:       List[Dict]    = []
        self.pot         = 0.0
        self.side_pots:  List[Dict]    = []
        self.current_bet = 0.0
        self.dealer_seat = 0
        self.action_seat = 0
        self.action_uid  = None        # user_id of the player whose turn it is
        self.round_num   = 0
        self.task:       Optional[asyncio.Task] = None
        self.created_at  = time.time()
        self.waiting_for_human = False  # pause bot logic for human action
        self._real_pot   = 0.0          # sum of real-player contributions only (not bot virtual chips)

    # ── WS helpers ────────────────────────────────────────
    def add_ws(self, user_id: int, ws: WebSocket):
        self.ws_set.add(ws)
        self.ws_map[user_id] = ws

    def remove_ws(self, user_id: int, ws: WebSocket):
        self.ws_set.discard(ws)
        self.ws_map.pop(user_id, None)

    async def broadcast(self, msg: dict):
        dead = await broadcast_to_set(self.ws_set, msg)
        self.ws_set -= dead

    async def send_to(self, user_id: int, msg: dict):
        ws = self.ws_map.get(user_id)
        if ws:
            try:
                await ws.send_json(msg)
            except Exception:
                pass

    # ── Seat management ───────────────────────────────────
    @property
    def active_players(self) -> List[PokerPlayer]:
        return [p for p in self.players.values()
                if p.status in ('active', 'allin')]

    @property
    def real_player_count(self) -> int:
        return sum(1 for p in self.players.values() if not p.is_bot)

    def next_free_seat(self) -> Optional[int]:
        used = set(self.seats.keys())
        for s in range(MAX_PLAYERS):
            if s not in used:
                return s
        return None

    def fill_bots(self):
        """Fill remaining seats with bot players."""
        profiles = secure_shuffle(list(BOT_NAMES))
        bot_idx  = 0
        while len(self.players) < MAX_PLAYERS:
            seat = self.next_free_seat()
            if seat is None:
                break
            profile   = profiles[bot_idx % len(profiles)]
            bot_idx  += 1
            bot_uid   = -(len(self.players) + 1)
            bot_name  = BOT_PROFILES[profile]['name']
            player    = PokerPlayer(
                user_id     = bot_uid,
                username    = bot_name,
                chips       = self.buy_in,
                seat        = seat,
                is_bot      = True,
                bot_profile = profile,
            )
            self.players[bot_uid] = player
            self.seats[seat]      = bot_uid

    # ── Round lifecycle ───────────────────────────────────
    async def run_game_loop(self):
        """Continuous game loop — deals rounds until table closes."""
        await asyncio.sleep(3)   # brief wait for connections
        while self.real_player_count > 0 and self.round_num < self.MAX_ROUNDS:
            try:
                await self._play_round()
                self.round_num += 1
                await asyncio.sleep(5)   # inter-round pause
            except asyncio.CancelledError:
                await self._refund_remaining_chips()
                return
            except Exception as e:
                logger.error(f"Poker round error in {self.room_code}: {e}")
                await asyncio.sleep(2)

        await self._refund_remaining_chips()
        await self.broadcast({'type': 'table_closing', 'room_code': self.room_code})
        async with _holdem_lock:
            _holdem_rooms.pop(self.room_code, None)

    async def _refund_remaining_chips(self):
        """Credit remaining chips back to any real players still at the table."""
        pool = await get_db()
        for player in list(self.players.values()):
            if not player.is_bot and player.chips > 0:
                try:
                    async with pool.acquire() as conn:
                        await add_balance(player.user_id, player.chips, conn)
                    logger.info(
                        f"♠️ Refunded {player.chips} chips to player {player.user_id} "
                        f"(room {self.room_code} closed)"
                    )
                    player.chips = 0
                except Exception as e:
                    logger.error(
                        f"Failed to refund chips to player {player.user_id}: {e}"
                    )

    async def _play_round(self):
        self.phase     = 'pre-flop'
        self.pot       = 0.0
        self._real_pot = 0.0
        self.community = []
        self.deck     = new_deck()
        self.current_bet = self.big_blind

        # Reset players for new round
        eligible = [p for p in self.players.values() if p.chips > 0]
        if len(eligible) < 2:
            return

        for p in self.players.values():
            p.hole_cards       = []
            p.bet_this_street  = 0.0
            p.total_bet        = 0.0
            p.status           = 'active' if p.chips > 0 else 'sitting_out'
            p.is_dealer        = False
            p.is_sb            = False
            p.is_bb            = False

        # Assign dealer / blinds
        seats_in_play = sorted([p.seat for p in eligible])
        self.dealer_seat = seats_in_play[self.round_num % len(seats_in_play)]

        def _next_seat(seat: int) -> int:
            idx = seats_in_play.index(seat)
            return seats_in_play[(idx + 1) % len(seats_in_play)]

        sb_seat  = _next_seat(self.dealer_seat)
        bb_seat  = _next_seat(sb_seat)
        utg_seat = _next_seat(bb_seat)

        dealer_uid = self.seats.get(self.dealer_seat)
        sb_uid     = self.seats.get(sb_seat)
        bb_uid     = self.seats.get(bb_seat)

        if dealer_uid and dealer_uid in self.players:
            self.players[dealer_uid].is_dealer = True
        if sb_uid and sb_uid in self.players:
            self.players[sb_uid].is_sb = True
            await self._post_blind(sb_uid, self.small_blind)
        if bb_uid and bb_uid in self.players:
            self.players[bb_uid].is_bb = True
            await self._post_blind(bb_uid, self.big_blind)

        # Deal hole cards
        for p in eligible:
            p.hole_cards = [self.deck.pop(), self.deck.pop()]
            # Send private hole cards only to this player
            await self.send_to(p.user_id, {
                'type':       'hole_cards',
                'hole_cards': p.hole_cards,
            })

        await self.broadcast({
            'type':       'round_start',
            'round':      self.round_num + 1,
            'pot':        self.pot,
            'players':    [p.to_dict() for p in self.players.values()],
            'community':  [],
            'dealer':     self.dealer_seat,
            'phase':      'pre-flop',
        })

        # Betting streets
        self.action_seat = utg_seat
        await self._betting_round(seats_in_play, utg_seat)

        for street, n_cards in [('flop', 3), ('turn', 1), ('river', 1)]:
            if self._hand_over():
                break
            self._deal_community(n_cards)
            self.phase = street
            self.current_bet = 0.0
            for p in self.players.values():
                p.bet_this_street = 0.0
            await self.broadcast({
                'type':      'street',
                'phase':     street,
                'community': self.community,
                'pot':       self.pot,
                'players':   [p.to_dict() for p in self.players.values()],
            })
            self.action_seat = sb_seat
            await self._betting_round(seats_in_play, sb_seat)

        await self._showdown()

    async def _post_blind(self, uid: int, amount: float):
        p = self.players.get(uid)
        if not p:
            return
        actual = min(amount, p.chips)
        p.chips          -= actual
        p.bet_this_street = actual
        p.total_bet      += actual
        self.pot         += actual
        if not p.is_bot:
            self._real_pot += actual
        if p.chips == 0:
            p.status = 'allin'

    def _deal_community(self, n: int):
        self.deck.pop()   # burn card
        for _ in range(n):
            self.community.append(self.deck.pop())

    def _hand_over(self) -> bool:
        return sum(1 for p in self.players.values()
                   if p.status == 'active') <= 1

    async def _betting_round(self, seats_in_play: List[int], start_seat: int):
        """Run a full betting round."""
        if not seats_in_play:
            return

        n          = len(seats_in_play)
        start_idx  = seats_in_play.index(start_seat) if start_seat in seats_in_play else 0
        acted      = set()
        last_raise = self.seats.get(start_seat)

        loop_count = 0
        i          = start_idx

        while loop_count < n * 3:
            seat   = seats_in_play[i % n]
            uid    = self.seats.get(seat)
            if not uid:
                i += 1
                loop_count += 1
                continue
            player = self.players.get(uid)
            if not player or player.status not in ('active',):
                i += 1
                loop_count += 1
                continue

            # Everyone acted and no pending call → street over
            call_amt = max(0, self.current_bet - player.bet_this_street)
            all_acted = all(
                uid2 in acted or self.players[uid2].status != 'active'
                for uid2 in self.players
            )
            if all_acted and call_amt == 0:
                break

            if player.is_bot:
                await asyncio.sleep(self.BOT_ACTION_DELAY)
                strength = bot_hand_strength(player.hole_cards, self.community)
                action, amount = bot_action(
                    player.bot_profile, strength,
                    call_amt, self.pot, self.big_blind
                )
                await self._apply_action(uid, action, amount)
                await self.broadcast({
                    'type':    'bot_action',
                    'user_id': uid,
                    'username': player.username,
                    'action':  action,
                    'amount':  amount,
                    'pot':     self.pot,
                    'players': [p.to_dict() for p in self.players.values()],
                })
                if action == 'raise':
                    last_raise = uid
                    acted.discard(uid)
                acted.add(uid)

            else:
                # Human player — set flag and wait
                self.waiting_for_human = True
                self.action_seat       = seat
                self.action_uid        = uid
                await self.send_to(uid, {
                    'type':       'your_turn',
                    'call_amount': call_amt,
                    'pot':        self.pot,
                    'min_raise':  max(self.big_blind, self.current_bet * 2),
                    'your_chips': player.chips,
                    'community':  self.community,
                    'phase':      self.phase,
                })
                # Wait up to 30s for human action (Fix 11: try/finally ensures reset)
                try:
                    deadline = time.time() + 30
                    while self.waiting_for_human and time.time() < deadline:
                        await asyncio.sleep(0.1)
                    if self.waiting_for_human:
                        # Timeout — auto-fold
                        await self._apply_action(uid, 'fold', 0)
                finally:
                    self.waiting_for_human = False   # always reset
                acted.add(uid)

            i          += 1
            loop_count += 1

    async def _apply_action(self, uid: int, action: str, amount: float):
        p = self.players.get(uid)
        if not p:
            return

        call_needed = max(0, self.current_bet - p.bet_this_street)

        if action == 'fold':
            p.status = 'folded'

        elif action in ('call', 'check'):
            actual = min(call_needed, p.chips)
            p.chips          -= actual
            p.bet_this_street += actual
            p.total_bet      += actual
            self.pot         += actual
            if not p.is_bot:
                self._real_pot += actual
            if p.chips == 0:
                p.status = 'allin'

        elif action == 'raise':
            # Fix 4: enforce minimum raise
            min_raise = max(self.big_blind, self.current_bet * 2)
            if amount < min_raise:
                raise ValueError(
                    f"Minimum raise is {min_raise:.2f} "
                    f"(current bet {self.current_bet:.2f}, big blind {self.big_blind:.2f})"
                )
            total_raise = min(amount, p.chips)
            p.chips          -= total_raise
            p.bet_this_street += total_raise
            p.total_bet      += total_raise
            self.pot         += total_raise
            if not p.is_bot:
                self._real_pot += total_raise
            self.current_bet  = p.bet_this_street
            if p.chips == 0:
                p.status = 'allin'

    async def process_human_action(self, uid: int, action: str, amount: float):
        """Called by WebSocket handler when human submits action."""
        if not self.waiting_for_human:
            return
        if uid != self.action_uid:
            return  # Not this player's turn — reject the action
        player = self.players.get(uid)
        if not player or player.status != 'active':
            return
        try:
            await self._apply_action(uid, action, amount)
        except ValueError as e:
            await self.send_to(uid, {'type': 'action_error', 'message': str(e)})
            return   # keep waiting_for_human=True so player can retry
        await self.broadcast({
            'type':    'player_action',
            'user_id': uid,
            'username': player.username,
            'action':  action,
            'amount':  amount,
            'pot':     self.pot,
            'players': [p.to_dict() for p in self.players.values()],
        })
        self.waiting_for_human = False

    async def _showdown(self):
        self.phase = 'showdown'

        # Find eligible winners
        alive = [p for p in self.players.values()
                 if p.status in ('active', 'allin')]

        if len(alive) == 1:
            winner = alive[0]
            win    = round(self._real_pot * (1 - HOUSE_EDGE_POKER), 2)
            # Credit chips only — holdem_leave returns chips to DB so paying
            # here via _add_win would double-credit the winner's real balance.
            winner.chips += win
            await self.broadcast({
                'type':     'showdown',
                'winners':  [winner.to_dict(show_cards=True)],
                'community': self.community,
                'pot':      self.pot,
                'net_pot':  win,
            })
            return

        # Multi-player showdown — evaluate hands using all 7 cards
        ranked = []
        for p in alive:
            all7 = p.hole_cards + self.community
            rank, tb, best = best_hand_from_7(all7)
            ranked.append((rank, tb, p, best))

        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)

        # Include ALL players (folded too) so their contributions reach winners;
        # _calculate_side_pots excludes folded players from the eligible-to-win lists.
        pot_players = [
            {'user_id': p.user_id, 'total_contributed': p.total_bet, 'status': p.status}
            for p in self.players.values()
            if p.total_bet > 0
        ]
        side_pots = _calculate_side_pots(pot_players)

        # Build hand-rank lookup
        hand_lookup = {r[2].user_id: (r[0], r[1], r[2], r[3]) for r in ranked}

        # Scale pot amounts down to real-player money only — bot chips are virtual.
        real_scale = self._real_pot / max(self.pot, 0.01)

        winner_dicts = []

        # Credit chips only — holdem_leave returns chips to DB so paying via
        # _add_win here would double-credit every winner's real balance.
        for pot_info in side_pots:
            eligible_ids = pot_info['eligible']
            pot_amount   = round(pot_info['amount'] * real_scale, 2)

            eligible_ranked = [
                hand_lookup[uid] for uid in eligible_ids if uid in hand_lookup
            ]
            if not eligible_ranked:
                continue
            eligible_ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)

            top_rank = eligible_ranked[0][0]
            top_tb   = eligible_ranked[0][1]
            pot_winners = [
                (r[2], r[3]) for r in eligible_ranked
                if r[0] == top_rank and r[1] == top_tb
            ]

            base_split = round(pot_amount * (1 - HOUSE_EDGE_POKER) / len(pot_winners), 2)
            for p, best_cards in pot_winners:
                p.chips += base_split
                wr = p.to_dict(show_cards=True)
                wr['best_hand']  = best_cards
                wr['hand_name']  = HAND_NAMES[top_rank]
                wr['payout']     = base_split
                winner_dicts.append(wr)

        all_player_cards = [p.to_dict(show_cards=True) for p in alive]

        await self.broadcast({
            'type':        'showdown',
            'winners':     winner_dicts,
            'all_players': all_player_cards,
            'community':   self.community,
            'pot':         self.pot,
            'net_pot':     round(self.pot * (1 - HOUSE_EDGE_POKER), 2),
            'split':       len(winner_dicts) > 1,
        })


# ============================================================
# ROOM REGISTRY
# ============================================================

def _calculate_side_pots(players: list) -> list:
    """
    Fix 25: Returns list of {'amount': float, 'eligible': [user_id]} dicts.
    players: list of {'user_id': int, 'total_contributed': float, 'status': str}
    """
    contributions = sorted(
        [p for p in players if p['total_contributed'] > 0],
        key=lambda p: p['total_contributed']
    )
    pots = []
    prev_level = 0.0
    remaining = list(contributions)

    for player in contributions:
        level = player['total_contributed']
        if level <= prev_level:
            continue
        pot_slice = (level - prev_level) * len(remaining)
        eligible  = [p['user_id'] for p in remaining if p.get('status') != 'folded']
        pots.append({'amount': pot_slice, 'eligible': eligible})
        prev_level = level
        remaining  = [p for p in remaining if p['total_contributed'] > level]

    return pots

_holdem_rooms:    Dict[str, HoldemRoom] = {}
_holdem_lock      = asyncio.Lock()

def _holdem_room_code() -> str:
    import string
    return 'P' + ''.join(secure_choice(string.ascii_uppercase + string.digits) for _ in range(5))

def _find_holdem_room(buy_in: float) -> Optional[HoldemRoom]:
    for room in _holdem_rooms.values():
        if (room.phase == 'waiting'
                and room.real_player_count < MAX_PLAYERS
                and abs(room.buy_in - buy_in) / max(room.buy_in, 1) < 0.5):
            return room
    return None


# ============================================================
# TEXAS HOLD'EM HTTP ENDPOINTS
# ============================================================

class HoldemJoinRequest(BaseModel):
    buy_in: float   # chips to sit down with

class HoldemActionRequest(BaseModel):
    room_code: str
    action:    str
    amount:    float = 0

class HoldemLeaveRequest(BaseModel):
    room_code: str

@router.post("/holdem/join")
async def holdem_join(req: HoldemJoinRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("poker")
    buy_in  = clamp_bet(req.buy_in)

    # Acquire the lock before the DB connection to avoid holding a pool
    # connection while suspended waiting for the lock (pool exhaustion risk).
    async with _holdem_lock:
        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                user = await conn.fetchrow(
                    "SELECT username, balance FROM users WHERE user_id=$1", user_id
                )
                if not user or float(user['balance']) < buy_in:
                    raise HTTPException(400, "Insufficient balance")

                room = _find_holdem_room(buy_in)
                if not room:
                    code = _holdem_room_code()
                    room = HoldemRoom(code, buy_in)
                    _holdem_rooms[code] = room

                if user_id in room.players:
                    raise HTTPException(400, "Already at this table")

                seat = room.next_free_seat()
                if seat is None:
                    raise HTTPException(400, "Table is full")

                if not await deduct_balance(user_id, buy_in, conn):
                    raise HTTPException(400, "Insufficient balance")

                player = PokerPlayer(
                    user_id  = user_id,
                    username = user['username'] or f'Player {user_id}',
                    chips    = buy_in,
                    seat     = seat,
                )
                room.players[user_id] = player
                room.seats[seat]      = user_id

                # Fill bots and start if this is first real player
                if room.real_player_count == 1:
                    room.fill_bots()
                    room.task = asyncio.create_task(room.run_game_loop())

    return {
        "success":   True,
        "room_code": room.room_code,
        "seat":      seat,
        "buy_in":    buy_in,
        "chips":     buy_in,
        "small_blind": room.small_blind,
        "big_blind":   room.big_blind,
    }


@router.post("/holdem/action")
async def holdem_action(req: HoldemActionRequest, request: Request):
    """Submit a player action (fold/call/check/raise)."""
    user_id   = await require_auth(request)
    room_code = req.room_code
    action    = req.action.lower()
    amount    = req.amount

    if action not in ('fold', 'call', 'check', 'raise'):
        raise HTTPException(400, "Invalid action")

    async with _holdem_lock:
        room = _holdem_rooms.get(room_code)
    if not room:
        raise HTTPException(404, "Room not found")

    await room.process_human_action(user_id, action, amount)
    return {"success": True}


@router.post("/holdem/leave")
async def holdem_leave(req: HoldemLeaveRequest, request: Request):
    """Cash out and leave the table."""
    user_id   = await require_auth(request)
    room_code = req.room_code

    async with _holdem_lock:
        room = _holdem_rooms.get(room_code)

        if not room:
            raise HTTPException(404, "Room not found")

        player = room.players.get(user_id)
        if not player:
            raise HTTPException(400, "Not at this table")

        remaining = player.chips
        if remaining > 0:
            pool = await get_db()
            async with pool.acquire() as conn:
                vip = await get_vip_status(user_id)
                remaining = apply_vip_boost(remaining, vip)
                await add_balance(user_id, remaining, conn)

        # If the game loop is waiting for this player's action, signal it to
        # move on immediately rather than waiting for the 30-second timeout.
        if room.waiting_for_human and room.action_uid == user_id:
            room.waiting_for_human = False

        del room.players[user_id]
        room.seats = {s: u for s, u in room.seats.items() if u != user_id}

    await room.broadcast({
        'type':     'player_left',
        'user_id':  user_id,
        'username': player.username,
        'chips_returned': remaining,
    })
    return {"success": True, "chips_returned": remaining}


@router.get("/holdem/rooms")
async def holdem_rooms():
    async with _holdem_lock:
        rooms = [
        {
            'room_code':    code,
            'buy_in':       room.buy_in,
            'player_count': room.real_player_count,
            'phase':        room.phase,
            'round':        room.round_num,
        }
            for code, room in _holdem_rooms.items()
        ]
    return rooms


# ============================================================
# TEXAS HOLD'EM WEBSOCKET
# ============================================================

@router.websocket("/holdem/ws/{room_code}")
async def holdem_ws(websocket: WebSocket, room_code: str):
    await websocket.accept()

    token = websocket.cookies.get("session_token")
    session = shared.get_session(token) if token else None
    if not session:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    user_id = session["user_id"]
    async with _holdem_lock:
        room = _holdem_rooms.get(room_code)

    if not room:
        try:
            await websocket.send_json({'type': 'error', 'message': 'Room not found'})
        except Exception:
            pass
        await websocket.close()
        return

    room.add_ws(user_id, websocket)
    player = room.players.get(user_id)
    is_participant = player is not None

    try:
        await websocket.send_json({
            'type':       'table_state',
            'room_code':  room_code,
            'phase':      room.phase,
            'pot':        room.pot,
            'community':  room.community,
            'players':    [p.to_dict(show_cards=(p.user_id == user_id))
                          for p in room.players.values()],
            'your_seat':  player.seat if player else None,
            'your_chips': player.chips if player else 0,
            'small_blind': room.small_blind,
            'big_blind':   room.big_blind,
        })
    except Exception:
        pass

    try:
        while True:
            data     = await websocket.receive_json()
            msg_type = data.get('type')

            if msg_type == 'action' and is_participant:
                action = data.get('action', '').lower()
                amount = float(data.get('amount', 0))
                if action in ('fold', 'call', 'check', 'raise'):
                    await room.process_human_action(user_id, action, amount)

            elif msg_type == 'reaction':
                rxn = data.get('emoji', '')
                if rxn in ('🔥', '😂', '😤', '👑', '💀', '🤔'):
                    await room.broadcast({
                        'type':    'reaction',
                        'user_id': user_id,
                        'emoji':   rxn,
                    })

            elif msg_type == 'ping':
                try:
                    await websocket.send_json({'type': 'pong'})
                except Exception:
                    break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"Holdem WS error: {e}")
    finally:
        room.remove_ws(user_id, websocket)


# ============================================================
# ══════════════════════════════════════════════════════════
#  VIDEO POKER  —  Jacks-or-Better solo mode
# ══════════════════════════════════════════════════════════
# ============================================================
#
# Standard 5-card draw, Jacks-or-Better.
# Player gets 5 cards, chooses which to hold, gets replacement
# cards for the rest. Final hand determines payout.
#
# Paytable (9/6 Jacks-or-Better — best standard pay table):
#   Royal Flush:        800×
#   Straight Flush:     50×
#   Four of a Kind:     25×
#   Full House:          9×
#   Flush:               6×
#   Straight:            4×
#   Three of a Kind:     3×
#   Two Pair:            2×
#   Jacks or Better:     1×
#   (pair of J/Q/K/A)
#   Anything else:       0

VP_PAYTABLE = {
    8: 800,   # Royal Flush (straight flush with A-high)
    7: 50,    # Other Straight Flush
    6: 25,    # Quads
    5: 9,     # Full House
    4: 6,     # Flush
    3: 4,     # Straight
    2: 3,     # Trips
    1: 2,     # Two Pair
    # Pair of JJJ+ = rank 1, but we check specifically below
}

VP_HAND_NAMES = {
    'royal_flush': 'Royal Flush',
    'straight_flush': 'Straight Flush',
    'quads': 'Four of a Kind',
    'full_house': 'Full House',
    'flush': 'Flush',
    'straight': 'Straight',
    'trips': 'Three of a Kind',
    'two_pair': 'Two Pair',
    'jacks_or_better': 'Jacks or Better',
    'nothing': 'No Win',
}

def is_royal_flush(cards: List[Dict]) -> bool:
    rank, _ = evaluate_5(cards)
    if rank != 8:
        return False
    vals = sorted([c['value'] for c in cards])
    return vals == [8, 9, 10, 11, 12]   # 10, J, Q, K, A

def vp_evaluate(cards: List[Dict]) -> Tuple[str, int]:
    """Return (hand_name_key, multiplier)."""
    rank, tbs = evaluate_5(cards)
    if rank == 8:
        if is_royal_flush(cards):
            return 'royal_flush', 800
        return 'straight_flush', 50
    if rank == 7:
        return 'quads', 25
    if rank == 6:
        return 'full_house', 9
    if rank == 5:
        return 'flush', 6
    if rank == 4:
        return 'straight', 4
    if rank == 3:
        return 'trips', 3
    if rank == 2:
        return 'two_pair', 2
    if rank == 1:
        # Check if pair is Jacks or better (J=9, Q=10, K=11, A=12)
        cnt = _counts(cards)
        pairs = [v for v, c in cnt.items() if c == 2]
        if pairs and max(pairs) >= 9:   # J=9 in 0-indexed
            return 'jacks_or_better', 1
        return 'nothing', 0
    return 'nothing', 0

_vp_sessions: Dict[int, Dict] = {}
_vp_locks:    Dict[int, asyncio.Lock] = {}   # Fix 5: per-user locks

def _get_vp_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _vp_locks:
        _vp_locks[user_id] = asyncio.Lock()
    return _vp_locks[user_id]

class VPDealRequest(BaseModel):
    amount: float

class VPDrawRequest(BaseModel):
    hold: List[int]   # indices 0-4 of cards to keep

@router.post("/video/deal")
async def vp_deal(req: VPDealRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("poker")
    bet     = clamp_bet(req.amount)
    lock    = _get_vp_lock(user_id)
    async with lock:
        if _vp_sessions.get(user_id):
            raise HTTPException(400, "Active Video Poker hand in progress — draw first")
        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")

        deck  = new_deck()
        hand  = [deck.pop() for _ in range(5)]
        _vp_sessions[user_id] = {'bet': bet, 'deck': deck, 'hand': hand, 'created_at': datetime.now(timezone.utc)}

        return {
            "success": True,
            "hand":    hand,
            "paytable": VP_PAYTABLE,
        }

@router.post("/video/draw")
async def vp_draw(req: VPDrawRequest, request: Request):
    user_id = await require_auth(request)
    lock    = _get_vp_lock(user_id)
    async with lock:
        # Fix 22: get without popping — only remove after successful DB commit
        sess = _vp_sessions.get(user_id)
        if not sess:
            raise HTTPException(400, "No active Video Poker hand — deal first")

        hold    = [i for i in req.hold if 0 <= i <= 4]
        deck    = sess['deck']
        hand    = sess['hand']
        bet     = sess['bet']

        # Replace non-held cards
        final_hand = []
        for i in range(5):
            if i in hold:
                final_hand.append(hand[i])
            else:
                final_hand.append(deck.pop())

        hand_key, mult = vp_evaluate(final_hand)
        win = apply_house(bet * mult) if mult else 0

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if win:
                    win = await credit_win(user_id, win, conn)
                await log_game(conn, user_id, 'video_poker', bet, win, {
                    'hand':      hand_key,
                    'mult':      mult,
                    'held':      hold,
                })
        # Only remove session AFTER the transaction commits successfully
        _vp_sessions.pop(user_id, None)
        _vp_locks.pop(user_id, None)

        return {
            "success":   True,
            "final_hand": final_hand,
            "held":       hold,
            "hand_name":  VP_HAND_NAMES[hand_key],
            "mult":       mult,
            "win":        win,
            "bet":        bet,
        }


