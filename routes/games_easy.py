# ============================================================
# routes/games_easy.py
# CS2CaseBot | Easy Games Backend
#
# Games: Slots (Classic), CS2 Weapon Slots, Jackpot Slots,
#        Bomb Defuse Slots, Coinflip, Dice, Limbo,
#        Hi-Lo, Dragon Tiger, Keno, Crash
# ============================================================

import asyncio
import math
import secrets
import time
from datetime import datetime, timezone
from typing import Dict, Set, Optional, List, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from pydantic import BaseModel

import shared
from shared import (
    logger, get_db, get_user_id_from_session, require_auth,
    ensure_user_exists, deduct_balance, add_balance,
    convert_decimals, broadcast_to_set,
    SLOT_SYMBOLS, SLOT_PAYOUTS,
    secure_random, secure_randint, secure_choice, secure_shuffle,
    apply_house_edge, HOUSE_EDGE, credit_win, require_game_enabled,
)

router = APIRouter(prefix="/api/games/easy", tags=["games-easy"])

# ============================================================
# HOUSE EDGE & BET LIMITS
# ============================================================

MIN_BET      = 50
MAX_BET      = 750_000
_HILO_SESSION_TTL = 3600   # seconds before an abandoned Hi-Lo session auto-expires

def clamp_bet(amount: float) -> float:
    return shared.clamp_bet(amount, MIN_BET, MAX_BET)

def apply_house(raw_win: float) -> float:
    return shared.apply_house(raw_win, HOUSE_EDGE)

# ============================================================
# GAME LOG HELPER
# ============================================================

async def log_game(conn, user_id: int, game_type: str,
                   bet: float, win: float, meta: dict = None):
    # win_inclusive=False: a push (win == bet) counts as 'loss' here, matching
    # this file's original behavior (differs from medium/hard/poker).
    await shared.log_game(conn, user_id, game_type, bet, win, meta,
                          win_inclusive=False)

# ============================================================
# ══════════════════════════════════════════════════════════
#  SLOTS — CLASSIC (3-reel fruit machine)
# ══════════════════════════════════════════════════════════
# ============================================================

# Weighted reel symbols: (symbol_index, weight)
CLASSIC_REEL_WEIGHTS = [40, 30, 20, 15, 8, 4, 1]   # Cherry→Jackpot
CLASSIC_PAYOUTS = {
    # 3-of-a-kind
    '🍒🍒🍒': 3,   '🍋🍋🍋': 5,   '🍊🍊🍊': 8,
    '🍇🍇🍇': 12,  '💎💎💎': 30,  '7️⃣7️⃣7️⃣': 60,  '🎰🎰🎰': 200,
    # 2-of-a-kind (partial)
    '🍒🍒':   1.5, '🍋🍋':   2,   '💎💎':    8,
}

def spin_classic_reel() -> str:
    total = sum(CLASSIC_REEL_WEIGHTS)
    r = secure_randint(1, total)
    cum = 0
    for i, w in enumerate(CLASSIC_REEL_WEIGHTS):
        cum += w
        if r <= cum:
            return SLOT_SYMBOLS[i]['emoji']
    return SLOT_SYMBOLS[0]['emoji']

def evaluate_classic(symbols: List[str]) -> tuple[float, str]:
    key3 = ''.join(symbols)
    if key3 in CLASSIC_PAYOUTS:
        return CLASSIC_PAYOUTS[key3], '3-of-a-kind'
    # Check 2-of-a-kind with first two
    key2 = ''.join(symbols[:2])
    if key2 in CLASSIC_PAYOUTS:
        return CLASSIC_PAYOUTS[key2], '2-of-a-kind'
    return 0, 'loss'

class SlotsRequest(BaseModel):
    amount: float

@router.post("/slots/spin")
async def slots_spin(req: SlotsRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("slots")
    bet     = clamp_bet(req.amount)

    async with (await get_db()).acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, bet, conn):
                raise HTTPException(400, "Insufficient balance")

            symbols = [spin_classic_reel() for _ in range(3)]
            mult, combo = evaluate_classic(symbols)
            win = apply_house(bet * mult) if mult else 0

            # VIP win boost
            if win:
                win = await credit_win(user_id, win, conn)

            await log_game(conn, user_id, 'slots_classic', bet, win,
                           {'symbols': symbols, 'combo': combo, 'mult': mult})

    return {
        "success":  True,
        "symbols":  symbols,
        "combo":    combo,
        "mult":     mult,
        "win":      win,
        "bet":      bet,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  CS2 WEAPON SLOTS — Rarity-based 3-reel
# ══════════════════════════════════════════════════════════
# ============================================================

# Each reel pulls from CS2 rarity weights
CS2_RARITY_REELS = [
    # (rarity_name, emoji, weight, 3-match-mult)
    ('Blue',   '🟦', 55, 3),
    ('Purple', '🟪', 25, 8),
    ('Pink',   '💗', 12, 20),
    ('Red',    '🔴', 5,  60),
    ('Gold',   '⭐', 3,  200),
    # Bonus symbol -- doesn't participate in CS2_SPECIAL/partial-match logic
    # at all, a single 🎁 anywhere just queues a free bonus mini-round (see
    # /slots/cs2/bonus/spin). Weight 7 of 107 total -> ~6.5% per reel ->
    # 1-(100/107)^3 ~= 18% chance of landing at least once across 3 reels
    # (tuned up from an earlier ~3% so it's felt regularly, not "once a
    # blue moon" -- recompute this comment's math if the weight changes).
    ('Bonus',  '🎁', 7,  0),
]
# Special combos
CS2_SPECIAL = {
    '⭐⭐⭐': (500, 'LEGENDARY TRIPLE'),
    '🔴🔴🔴': (100, 'RED TRIPLE'),
    '💗💗💗': (40,  'PINK TRIPLE'),
    '🟪🟪🟪': (15,  'PURPLE TRIPLE'),
    '🟦🟦🟦': (5,   'BLUE TRIPLE'),
}
CS2_EMOJI_TO_RARITY = {emoji: name for name, emoji, _, _ in CS2_RARITY_REELS}

# Bonus-round tokens: single-use, short-lived, purely in-memory. No crash-
# recovery sweep needed -- the triggering spin's bet is already fully
# resolved (cash paid out or not) before this token exists, so there's no
# money/item held in escrow across a restart, just an unclaimed bonus
# prompt (an acceptable, low-stakes UX loss, unlike money-holding state).
_bonus_round_sessions: Dict[str, Dict] = {}
BONUS_ROUND_TTL_SECS = 300

# Every case's collection has a full Blue/Purple/Pink/Red spread (confirmed
# directly against shared.CASES/COLLECTION_ITEMS -- all 37 cases qualify) and
# Gold always has a global fallback pool in get_random_item_by_rarity, so a
# random case_id here practically never misses; the loop is just defense in
# depth against future case-data changes, not a real expected retry path.
def _grant_cs2_rarity_weapon(rarity: str) -> Optional[Dict]:
    case_ids = secure_shuffle(list(shared.CASES.keys()))
    for case_id in case_ids[:5]:
        item = shared.get_random_item_by_rarity(case_id, rarity)
        if item:
            return item
    return None

async def _insert_granted_weapon(conn, user_id: int, item: dict) -> dict:
    """Same INSERT shape as item_trade_up_duel.py's upgrade-item grant --
    kept consistent so a weapon won from a slot spin looks identical in
    inventory to one won any other way. Returns a small dict for the
    response payload (not the full inventory row)."""
    skin_img_file = item.get('image_filename')
    skin_img_url = f"/static/images/skins/{skin_img_file}" if skin_img_file else None
    row = await conn.fetchrow("""
        INSERT INTO inventory
            (user_id, item_name, item_type, rarity, price, condition,
             is_stattrak, status, float_value, image_url)
        VALUES ($1,$2,'weapon',$3,$4,$5,$6,'kept',$7,$8)
        RETURNING id
    """, user_id, item['name'], item['rarity'], item['price'],
        item['condition'], item['is_stattrak'], item['float'], skin_img_url)
    return {
        "id":          row['id'],
        "name":        item['name'],
        "rarity":      item['rarity'],
        "price":       item['price'],
        "condition":   item['condition'],
        "is_stattrak": item['is_stattrak'],
        "image_url":   skin_img_url,
    }

def spin_cs2_reel() -> tuple[str, str]:
    total = sum(r[2] for r in CS2_RARITY_REELS)
    r = secure_randint(1, total)
    cum = 0
    for name, emoji, weight, _ in CS2_RARITY_REELS:
        cum += weight
        if r <= cum:
            return name, emoji
    return 'Blue', '🟦'

class CS2SlotsRequest(BaseModel):
    amount: float

@router.post("/slots/cs2/spin")
async def cs2_slots_spin(req: CS2SlotsRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("slots-cs2")
    bet     = clamp_bet(req.amount)

    async with (await get_db()).acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, bet, conn):
                raise HTTPException(400, "Insufficient balance")

            spins   = [spin_cs2_reel() for _ in range(3)]
            names   = [s[0] for s in spins]
            emojis  = [s[1] for s in spins]
            key     = ''.join(emojis)

            mult, label = CS2_SPECIAL.get(key, (0, 'miss'))
            is_full_triple = key in CS2_SPECIAL

            # Partial matches: 2 golds = 20x, 2 reds = 8x, etc
            if mult == 0:
                if emojis.count('⭐') == 2: mult, label = 20, 'DOUBLE GOLD'
                elif emojis.count('🔴') == 2: mult, label = 8, 'DOUBLE RED'
                elif emojis.count('💗') == 2: mult, label = 4, 'DOUBLE PINK'
                elif emojis.count('🟪') == 2: mult, label = 2, 'DOUBLE PURPLE'

            win = apply_house(bet * mult) if mult else 0
            if win:
                win = await credit_win(user_id, win, conn)

            # A full 3-of-a-kind ALSO grants a real weapon of that rarity, on
            # top of the cash payout -- partial (2-of-a-kind) matches stay
            # cash-only so the weapon grant stays rare/special.
            item_won = None
            if is_full_triple:
                rarity = CS2_EMOJI_TO_RARITY.get(emojis[0])
                if rarity:
                    granted = _grant_cs2_rarity_weapon(rarity)
                    if granted:
                        item_won = await _insert_granted_weapon(conn, user_id, granted)
                    else:
                        logger.warning(f"CS2 Slots: no case/rarity pool found for {rarity}, paid cash only")

            # Low-probability bonus symbol landing (any single 🎁, doesn't
            # need to match) queues a free bonus mini-round -- see
            # /slots/cs2/bonus/spin below. Token is short-lived and single-use.
            bonus_token = None
            if '🎁' in emojis:
                bonus_token = secrets.token_urlsafe(16)
                _bonus_round_sessions[bonus_token] = {"user_id": user_id, "created_at": time.time()}

            # Log the item's value alongside any cash win so a weapon-only
            # grant still classifies as a 'win' for win_streak/total_games_
            # played (shared.log_game infers win/loss purely from win>bet) --
            # same convention item_trade_up_duel.py already uses for its own
            # item-only payouts. The actual credited cash (returned to the
            # frontend / added to balance) is unaffected, only the logged value.
            logged_win = win + (item_won['price'] if item_won else 0)
            await log_game(conn, user_id, 'slots_cs2', bet, logged_win,
                           {'rarities': names, 'emojis': emojis, 'label': label,
                            'item_won': item_won['name'] if item_won else None})

    return {
        "success": True,
        "rarities": names,
        "emojis":   emojis,
        "label":    label,
        "mult":     mult,
        "win":      win,
        "bet":      bet,
        "item_won": item_won,
        "bonus_triggered": bonus_token is not None,
        "bonus_token": bonus_token,
    }


class CS2BonusSpinRequest(BaseModel):
    bonus_token: str

@router.post("/slots/cs2/bonus/spin")
async def cs2_bonus_spin(req: CS2BonusSpinRequest, request: Request):
    """Free bonus mini-round triggered by a 🎁 landing on the main CS2 Slots
    spin -- no bet, no cash payout, spins 3 rarity-only reels and grants a
    real weapon on any match. Token is single-use (popped immediately) and
    expires after BONUS_ROUND_TTL_SECS."""
    user_id = await require_auth(request)
    sess = _bonus_round_sessions.pop(req.bonus_token, None)
    if not sess or sess["user_id"] != user_id:
        raise HTTPException(400, "Invalid or already-used bonus round")
    if time.time() - sess["created_at"] > BONUS_ROUND_TTL_SECS:
        raise HTTPException(400, "This bonus round has expired")

    spins  = [spin_cs2_reel() for _ in range(3)]
    names  = [s[0] for s in spins if s[0] != 'Bonus']
    emojis = [s[1] for s in spins]

    item_won = None
    matched_rarity = None
    if len(set(emojis)) == 1 and emojis[0] != '🎁':
        matched_rarity = CS2_EMOJI_TO_RARITY.get(emojis[0])
    elif any(emojis.count(e) == 2 for e in set(emojis) if e != '🎁'):
        matched_rarity = CS2_EMOJI_TO_RARITY.get(
            next(e for e in emojis if emojis.count(e) == 2 and e != '🎁')
        )

    if matched_rarity:
        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                granted = _grant_cs2_rarity_weapon(matched_rarity)
                if granted:
                    item_won = await _insert_granted_weapon(conn, user_id, granted)
                # bet=0 (free bonus round) so any positive logged value
                # classifies as a 'win' -- see cs2_slots_spin's comment above
                # for why the item's value (not literal credited cash) is
                # what gets logged here.
                await log_game(conn, user_id, 'slots_cs2_bonus', 0,
                               item_won['price'] if item_won else 0,
                               {'emojis': emojis, 'item_won': item_won['name'] if item_won else None})

    return {
        "success": True,
        "emojis": emojis,
        "item_won": item_won,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  JACKPOT SLOTS — 5-reel progressive
# ══════════════════════════════════════════════════════════
# ============================================================

# In-memory progressive jackpot (resets to seed on win)
_jackpot_pool:  float = 10_000.0
_jackpot_seed:  float = 10_000.0
_jackpot_lock          = asyncio.Lock()
JACKPOT_FEED_RATE      = 0.02   # 2% of each bet feeds the pot

JACKPOT_REEL = [
    ('💀', 'Skull',   40),
    ('🔫', 'Gun',     30),
    ('💣', 'Bomb',    25),
    ('🌟', 'Star',    15),
    ('💎', 'Diamond', 8),
    ('🔑', 'Key',     4),
    ('🏆', 'Trophy',  1),
]

def spin_jackpot_reel() -> str:
    total = sum(r[2] for r in JACKPOT_REEL)
    r = secure_randint(1, total)
    cum = 0
    for emoji, _, weight in JACKPOT_REEL:
        cum += weight
        if r <= cum:
            return emoji
    return '💀'

def evaluate_jackpot_5reel(reels: List[str]) -> tuple[float, str, bool]:
    """Returns (multiplier, label, is_jackpot)"""
    counts = {}
    for e in reels:
        counts[e] = counts.get(e, 0) + 1

    max_count = max(counts.values())
    top_emoji  = max(counts, key=counts.get)

    # 5-of-a-kind jackpot trigger
    if max_count == 5:
        if top_emoji == '🏆':
            return 0, 'JACKPOT', True  # full jackpot pool
        if top_emoji == '🔑': return 500, '5x KEY', False
        if top_emoji == '💎': return 200, '5x DIAMOND', False
        if top_emoji == '🌟': return 80,  '5x STAR', False
        if top_emoji == '💣': return 30,  '5x BOMB', False
        if top_emoji == '🔫': return 15,  '5x GUN', False
        return 8, '5x SKULL', False

    if max_count == 4:
        mult_map = {'🏆':150,'🔑':60,'💎':30,'🌟':15,'💣':8,'🔫':4,'💀':2}
        return mult_map.get(top_emoji, 2), f'4x {top_emoji}', False

    if max_count == 3:
        mult_map = {'🏆':30,'🔑':15,'💎':8,'🌟':5,'💣':3,'🔫':2,'💀':1.5}
        return mult_map.get(top_emoji, 1.5), f'3x {top_emoji}', False

    # Scatter: 2 trophies
    if counts.get('🏆', 0) >= 2:
        return 5, 'DOUBLE TROPHY', False

    return 0, 'miss', False

class JackpotSlotsRequest(BaseModel):
    amount: float

@router.post("/slots/jackpot/spin")
async def jackpot_slots_spin(req: JackpotSlotsRequest, request: Request):
    global _jackpot_pool
    user_id = await require_auth(request)
    await require_game_enabled("slots-jackpot")
    bet     = clamp_bet(req.amount)

    reels = [spin_jackpot_reel() for _ in range(5)]
    mult, label, is_jackpot = evaluate_jackpot_5reel(reels)

    # Atomically claim the jackpot before any DB work so two concurrent
    # winners cannot both receive the same pool amount.
    jackpot_won = 0.0
    async with _jackpot_lock:
        if is_jackpot:
            jackpot_won   = _jackpot_pool
            _jackpot_pool = _jackpot_seed   # claimed — reset immediately

    win = apply_house(jackpot_won) if jackpot_won else (apply_house(bet * mult) if mult else 0.0)

    try:
        async with (await get_db()).acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")

                if win:
                    win = await credit_win(user_id, win, conn)

                await log_game(conn, user_id, 'slots_jackpot', bet, win,
                               {'reels': reels, 'label': label, 'jackpot': bool(jackpot_won)})

                if jackpot_won:
                    await conn.execute("""
                        UPDATE quests SET progress = progress + 1
                        WHERE user_id=$1 AND quest_type='jackpot_win' AND completed=FALSE
                    """, user_id)
                    await conn.execute("""
                        UPDATE quests SET completed=TRUE
                        WHERE user_id=$1 AND quest_type='jackpot_win' AND progress >= required AND completed=FALSE
                    """, user_id)
    except Exception:
        # DB failed — restore the claimed jackpot so it isn't lost.
        # Use additive restore: pool may have grown since the claim (other
        # concurrent bets feed in after the reset), so preserve those additions.
        if jackpot_won:
            async with _jackpot_lock:
                _jackpot_pool = round(_jackpot_pool + jackpot_won - _jackpot_seed, 2)
        raise

    # DB committed — feed this bet's contribution into the pool
    async with _jackpot_lock:
        _jackpot_pool = round(_jackpot_pool + bet * JACKPOT_FEED_RATE, 2)

    return {
        "success":      True,
        "reels":        reels,
        "label":        label,
        "mult":         mult,
        "is_jackpot":   is_jackpot,
        "jackpot_won":  jackpot_won,
        "win":          win,
        "bet":          bet,
        "current_pool": round(_jackpot_pool, 2),
    }

@router.get("/slots/jackpot/pool")
async def get_jackpot_pool():
    return {"pool": round(_jackpot_pool, 2)}

# ============================================================
# ══════════════════════════════════════════════════════════
#  BOMB DEFUSE SLOTS — 3-reel CT/T theme
# ══════════════════════════════════════════════════════════
# ============================================================

BOMB_REEL = [
    ('🔵', 'CT',      35),   # Counter-Terrorist
    ('🔴', 'T',       35),   # Terrorist
    ('🔫', 'Rifle',   20),   # Rifle
    ('💣', 'Bomb',    6),    # Bomb  — 3x = BUST
    ('🛡️', 'Defuse',  3),    # Defuse kit — 3x = JACKPOT
    ('💎', 'Diamond', 1),    # Diamond — ultra rare
]

BOMB_PAYOUTS = {
    '💎💎💎': (300, 'DIAMOND JACKPOT', False),
    '🛡️🛡️🛡️': (100, '🛡️ DEFUSE JACKPOT', False),
    '🔫🔫🔫': (20,  'TRIPLE RIFLE', False),
    '🔵🔵🔵': (10,  'CT SWEEP', False),
    '🔴🔴🔴': (8,   'T SWEEP', False),
    # CT + Defuse combos
    '🔵🔵🛡️': (6,   'CT DEFUSE', False),
    '🛡️🔵🔵': (6,   'CT DEFUSE', False),
    '🔵🛡️🔵': (6,   'CT DEFUSE', False),
    # Bomb = bust (lose extra 50% of bet on top)
    '💣💣💣': (0,   '💣 TRIPLE BOMB — BUST!', True),
}

def spin_bomb_reel() -> str:
    total = sum(r[2] for r in BOMB_REEL)
    r = secure_randint(1, total)
    cum = 0
    for emoji, _, weight in BOMB_REEL:
        cum += weight
        if r <= cum:
            return emoji
    return '🔵'

def evaluate_bomb(symbols: List[str]) -> tuple[float, str, bool]:
    key = ''.join(symbols)
    if key in BOMB_PAYOUTS:
        return BOMB_PAYOUTS[key]
    # 2-bomb partial: lose 25% extra
    if symbols.count('💣') == 2:
        return 0, '💣💣 DOUBLE BOMB — partial bust!', True
    # 2-defuse
    if symbols.count('🛡️') == 2:
        return 5, 'DOUBLE DEFUSE', False
    # 2-diamond
    if symbols.count('💎') == 2:
        return 30, 'DOUBLE DIAMOND', False
    return 0, 'miss', False

class BombSlotsRequest(BaseModel):
    amount: float

@router.post("/slots/bomb/spin")
async def bomb_slots_spin(req: BombSlotsRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("slots-bomb")
    bet     = clamp_bet(req.amount)

    async with (await get_db()).acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, bet, conn):
                raise HTTPException(400, "Insufficient balance")

            symbols = [spin_bomb_reel() for _ in range(3)]
            mult, label, is_bust = evaluate_bomb(symbols)

            win = 0.0
            extra_loss = 0.0
            if is_bust:
                # Double bomb: extra 25% penalty; triple: extra 50%
                penalty_rate = 0.50 if symbols.count('💣') == 3 else 0.25
                extra_loss_target = round(bet * penalty_rate, 2)
                success = await deduct_balance(user_id, extra_loss_target, conn)
                extra_loss = extra_loss_target if success else 0.0
            elif mult:
                win = apply_house(bet * mult)
                win = await credit_win(user_id, win, conn)

            await log_game(conn, user_id, 'slots_bomb', bet, win,
                           {'symbols': symbols, 'label': label, 'bust': is_bust,
                            'extra_loss': extra_loss})

    return {
        "success":     True,
        "symbols":     symbols,
        "label":       label,
        "mult":        mult,
        "is_bust":     is_bust,
        "extra_loss":  extra_loss,
        "win":         win,
        "bet":         bet,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  SKIN SPIN — 5-reel, rarity reels, real skins only (no cash payouts
#  on a win -- this game's entire premise is skins, not cash multipliers)
# ══════════════════════════════════════════════════════════
# ============================================================

SKIN_SPIN_REEL_COUNT = 5
# A literal 5-of-5 match with CS2_RARITY_REELS' weights would be
# astronomically rare for anything above Blue (e.g. Gold at 3% per reel is
# 0.03^5 -- effectively never), which would make "always grants a real skin"
# meaningless for most rarities. Matching this codebase's own CS2 Slots
# philosophy (a 3-of-3 full match is the win condition there, not a partial
# threshold that happens to be the max), Skin Spin's win condition is
# "3 or more of the 5 reels show the same rarity" -- achievable across all
# rarities, and still a real 60%-majority threshold, not a low bar.
SKIN_SPIN_COST = 500   # flat entry cost in game money -- paid from the same
                       # balance every other game bets with, not tickets, so
                       # this game (real skins on a win) has no link to any
                       # real-money-purchasable currency.

@router.post("/slots/skinspin/spin")
async def skin_spin_spin(request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("skin-spin")

    async with (await get_db()).acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, SKIN_SPIN_COST, conn):
                raise HTTPException(400, "Not enough balance")

            spins  = [spin_cs2_reel() for _ in range(SKIN_SPIN_REEL_COUNT)]
            names  = [s[0] for s in spins]
            emojis = [s[1] for s in spins]

            # Best (highest-rarity) name with a count >= 3, ignoring the
            # Bonus symbol (Skin Spin has no bonus-round hook of its own --
            # a stray 🎁 here is just a miss, same as any other non-matching
            # symbol, matching the plan's "keep this game's mechanic simple").
            rarity_order = [r[0] for r in CS2_RARITY_REELS if r[0] != 'Bonus']
            matched_rarity = None
            for rarity in reversed(rarity_order):  # highest rarity first
                if names.count(rarity) >= 3:
                    matched_rarity = rarity
                    break

            item_won = None
            if matched_rarity:
                granted = _grant_cs2_rarity_weapon(matched_rarity)
                if granted:
                    item_won = await _insert_granted_weapon(conn, user_id, granted)
                else:
                    logger.warning(f"Skin Spin: no case/rarity pool found for {matched_rarity}")

            # A miss is just a miss -- no cash consolation, matching every
            # arcade minigame's own economy (a whiff there just costs the
            # entry fee too, no partial refund). bet=0 here (this file's own
            # log_game() wrapper always passes win_inclusive=False through to
            # shared.log_game) classifies this purely on "did a weapon get
            # granted" (win>0) rather than relative to the SKIN_SPIN_COST
            # entry fee already deducted above -- an item's dollar value has
            # nothing to do with whether the spin "won".
            logged_win = item_won['price'] if item_won else 0.0
            await log_game(conn, user_id, 'slots_skinspin', 0, logged_win,
                           {'rarities': names, 'emojis': emojis,
                            'item_won': item_won['name'] if item_won else None})

    return {
        "success":  True,
        "rarities": names,
        "emojis":   emojis,
        "item_won": item_won,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  COINFLIP — vs computer
# ══════════════════════════════════════════════════════════
# ============================================================

class CoinflipRequest(BaseModel):
    amount: float
    call: str = "heads"   # "heads" | "tails"

@router.post("/coinflip")
async def coinflip(req: CoinflipRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("coinflip")
    bet     = clamp_bet(req.amount)
    call    = req.call.lower()
    if call not in ('heads', 'tails'):
        raise HTTPException(400, "Call must be 'heads' or 'tails'")

    result   = secure_choice(['heads', 'tails'])
    user_wins = (result == call)

    async with (await get_db()).acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, bet, conn):
                raise HTTPException(400, "Insufficient balance")

            win = apply_house(bet * 2) if user_wins else 0
            if win:
                win = await credit_win(user_id, win, conn)

            await log_game(conn, user_id, 'coinflip', bet, win,
                           {'call': call, 'result': result})

    return {
        "success":    True,
        "call":       call,
        "result":     result,
        "user_wins":  user_wins,
        "win":        win,
        "bet":        bet,
        "profit":     round(win - bet, 2) if user_wins else -bet,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  DICE — over/under 1-100
# ══════════════════════════════════════════════════════════
# ============================================================

class DiceRequest(BaseModel):
    amount:     float
    bet_type:   str    # "over" | "under"
    target:     int    # 2–98

@router.post("/dice/roll")
async def dice_roll(req: DiceRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("dice")
    bet     = clamp_bet(req.amount)
    btype   = req.bet_type.lower()
    target  = req.target

    if btype not in ('over', 'under'):
        raise HTTPException(400, "bet_type must be 'over' or 'under'")
    if not 2 <= target <= 98:
        raise HTTPException(400, "target must be 2–98")

    roll = secure_randint(1, 100)
    win_condition = (roll > target) if btype == 'over' else (roll < target)

    # Payout = (100 / winning_chance) × (1 - house_edge)
    if btype == 'over':
        winning_range = 100 - target
    else:
        winning_range = target - 1

    if winning_range <= 0:
        raise HTTPException(400, "Invalid target")

    mult = round((100 / winning_range) * (1 - HOUSE_EDGE), 4)

    async with (await get_db()).acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, bet, conn):
                raise HTTPException(400, "Insufficient balance")

            win = round(bet * mult, 2) if win_condition else 0
            if win:
                win = await credit_win(user_id, win, conn)

            await log_game(conn, user_id, 'dice', bet, win,
                           {'roll': roll, 'bet_type': btype, 'target': target,
                            'mult': mult})

    return {
        "success":   True,
        "roll":      roll,
        "bet_type":  btype,
        "target":    target,
        "user_wins": win_condition,
        "mult":      mult,
        "win":       win,
        "bet":       bet,
        "chance":    round(winning_range, 2),
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  LIMBO — set a target multiplier, beat it
# ══════════════════════════════════════════════════════════
# ============================================================

class LimboRequest(BaseModel):
    amount: float
    target: float   # minimum 1.01

@router.post("/limbo")
async def limbo(req: LimboRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("limbo")
    bet     = clamp_bet(req.amount)
    target  = max(1.01, round(float(req.target), 2))
    if target > 750_000:
        raise HTTPException(400, "Target too high")

    # Server generates a result multiplier using same crash formula
    result = shared.generate_crash_point(house_edge=HOUSE_EDGE)
    # Ensure result can reach very high values sometimes (Fix 10: apply house edge to moon shot)
    if secure_random() < 0.001:    # 0.1% — moon shot
        raw = 100 + secure_random() * 900
        result = apply_house_edge(raw)

    user_wins = result >= target

    async with (await get_db()).acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, bet, conn):
                raise HTTPException(400, "Insufficient balance")

            win = round(bet * target, 2) if user_wins else 0
            if win:
                win = await credit_win(user_id, win, conn)

            await log_game(conn, user_id, 'limbo', bet, win,
                           {'target': target, 'result': result})

    return {
        "success":   True,
        "target":    target,
        "result":    result,
        "user_wins": user_wins,
        "win":       win,
        "bet":       bet,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  HI-LO — chain card guesses for multiplier
# ══════════════════════════════════════════════════════════
# ============================================================

# Active Hi-Lo sessions: user_id → game state
_hilo_sessions: Dict[int, Dict] = {}
_hilo_locks:    Dict[int, asyncio.Lock] = {}   # Fix 5: per-user locks

def _get_hilo_lock(user_id: int) -> asyncio.Lock:
    """Return (and lazily create) the asyncio.Lock for this user's Hi-Lo session."""
    if user_id not in _hilo_locks:
        _hilo_locks[user_id] = asyncio.Lock()
    return _hilo_locks[user_id]

CARD_VALUES = list(range(1, 14))  # A=1, 2-10, J=11, Q=12, K=13
CARD_NAMES  = {1:'A',2:'2',3:'3',4:'4',5:'5',6:'6',
               7:'7',8:'8',9:'9',10:'10',11:'J',12:'Q',13:'K'}
CARD_SUITS  = ['♠','♥','♦','♣']

def new_card() -> dict:
    val  = secure_choice(CARD_VALUES)
    suit = secure_choice(CARD_SUITS)
    return {'value': val, 'name': CARD_NAMES[val], 'suit': suit,
            'display': CARD_NAMES[val] + suit}

def hilo_mult_for_guess(current: int, guess: str) -> float:
    """Calculate payout multiplier based on probability."""
    if guess == 'higher':
        cards_that_win = sum(1 for v in CARD_VALUES if v > current)
    else:
        cards_that_win = sum(1 for v in CARD_VALUES if v < current)
    if cards_that_win == 0:
        return 0
    prob = cards_that_win / len(CARD_VALUES)
    return round((1 / prob) * (1 - HOUSE_EDGE), 3)

class HiLoStartRequest(BaseModel):
    amount: float

class HiLoGuessRequest(BaseModel):
    guess: str   # "higher" | "lower"

class HiloCashoutRequest(BaseModel):
    pass

@router.post("/hilo/start")
async def hilo_start(req: HiLoStartRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("hilo")
    bet     = clamp_bet(req.amount)
    lock    = _get_hilo_lock(user_id)
    async with lock:
        existing_hilo = _hilo_sessions.get(user_id)
        if existing_hilo and existing_hilo.get('active'):
            created = existing_hilo.get('created_at')
            expired = (
                created is not None and
                (datetime.now(timezone.utc) - created).total_seconds() > _HILO_SESSION_TTL
            )
            if expired:
                _hilo_sessions.pop(user_id, None)
            else:
                raise HTTPException(400, "You already have an active Hi-Lo game — cashout or bust first")

        async with (await get_db()).acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")

        card = new_card()
        _hilo_sessions[user_id] = {
            'bet':        bet,
            'current':    card,
            'multiplier': 1.0,
            'chain':      0,
            'history':    [card],
            'active':     True,
            'created_at': datetime.now(timezone.utc),   # Fix 9: TTL tracking
        }

        return {
            "success":    True,
            "card":       card,
            "multiplier": 1.0,
            "chain":      0,
            "higher_mult": hilo_mult_for_guess(card['value'], 'higher'),
            "lower_mult":  hilo_mult_for_guess(card['value'], 'lower'),
        }

@router.post("/hilo/guess")
async def hilo_guess(req: HiLoGuessRequest, request: Request):
    user_id = await require_auth(request)
    lock    = _get_hilo_lock(user_id)
    async with lock:
        sess    = _hilo_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Hi-Lo game — start one first")

        guess   = req.guess.lower()
        if guess not in ('higher', 'lower'):
            raise HTTPException(400, "guess must be 'higher' or 'lower'")

        current = sess['current']['value']
        mult    = hilo_mult_for_guess(current, guess)
        if mult == 0:
            sess['active'] = False
            async with (await get_db()).acquire() as conn:
                await log_game(conn, user_id, 'hilo', sess['bet'], 0,
                               {'chain': sess['chain'], 'bust': True,
                                'reason': 'impossible_guess'})
            _hilo_sessions.pop(user_id, None)  # pop AFTER log
            _hilo_locks.pop(user_id, None)
            raise HTTPException(400, "That guess is impossible from this card")

        new = new_card()
        if guess == 'higher':
            correct = new['value'] > current
        else:
            correct = new['value'] < current

        sess['current'] = new
        sess['history'].append(new)

        if correct:
            sess['multiplier'] = round(sess['multiplier'] * mult, 4)
            sess['chain']     += 1
            return {
                "success":    True,
                "correct":    True,
                "new_card":   new,
                "multiplier": sess['multiplier'],
                "chain":      sess['chain'],
                "higher_mult": hilo_mult_for_guess(new['value'], 'higher'),
                "lower_mult":  hilo_mult_for_guess(new['value'], 'lower'),
            }
        else:
            # Bust — lose bet (already deducted at start)
            bet = sess['bet']
            sess['active'] = False
            async with (await get_db()).acquire() as conn:
                await log_game(conn, user_id, 'hilo', bet, 0,
                               {'chain': sess['chain'], 'bust': True})
            _hilo_sessions.pop(user_id, None)  # pop AFTER log
            _hilo_locks.pop(user_id, None)
            return {
                "success":   True,
                "correct":   False,
                "new_card":  new,
                "bust":      True,
                "chain":     sess['chain'],
            }

@router.post("/hilo/cashout")
async def hilo_cashout(request: Request):
    user_id = await require_auth(request)
    lock    = _get_hilo_lock(user_id)
    async with lock:
        sess    = _hilo_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Hi-Lo game")

        bet  = sess['bet']
        mult = sess['multiplier']
        win  = round(bet * mult, 2)
        sess['active'] = False

        async with (await get_db()).acquire() as conn:
            async with conn.transaction():
                win = await credit_win(user_id, win, conn)
                await log_game(conn, user_id, 'hilo', bet, win,
                               {'chain': sess['chain'], 'multiplier': mult})
        _hilo_sessions.pop(user_id, None)
        _hilo_locks.pop(user_id, None)

        return {
            "success":    True,
            "win":        win,
            "multiplier": mult,
            "chain":      sess['chain'],
        }

# ============================================================
# ══════════════════════════════════════════════════════════
#  DRAGON TIGER — two cards, bet on Dragon / Tiger / Tie
# ══════════════════════════════════════════════════════════
# ============================================================

class DragonTigerRequest(BaseModel):
    amount: float
    bet_on: str   # "dragon" | "tiger" | "tie"

@router.post("/dragon-tiger")
async def dragon_tiger(req: DragonTigerRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("dragon-tiger")
    bet     = clamp_bet(req.amount)
    bet_on  = req.bet_on.lower()
    if bet_on not in ('dragon', 'tiger', 'tie'):
        raise HTTPException(400, "bet_on must be dragon, tiger, or tie")

    dragon = new_card()
    tiger  = new_card()

    if dragon['value'] > tiger['value']:
        outcome = 'dragon'
    elif tiger['value'] > dragon['value']:
        outcome = 'tiger'
    else:
        outcome = 'tie'

    # Payout multipliers
    if outcome == 'tie' and bet_on == 'tie':
        mult = 8.0
        user_wins = True
    elif outcome == bet_on and outcome != 'tie':
        mult = 2.0
        user_wins = True
    elif outcome == 'tie' and bet_on != 'tie':
        # Tie returns half the bet
        mult = 0.5
        user_wins = False  # partial return
    else:
        mult = 0
        user_wins = False

    async with (await get_db()).acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, bet, conn):
                raise HTTPException(400, "Insufficient balance")

            if mult == 0.5:
                # Half return (tie pushback) — not a win, skip VIP boost
                win = round(bet * 0.5, 2)
                await add_balance(user_id, win, conn)
            elif user_wins:
                win = apply_house(bet * mult)
                win = await credit_win(user_id, win, conn)
            else:
                win = 0

            await log_game(conn, user_id, 'dragon_tiger', bet, win,
                           {'dragon': dragon, 'tiger': tiger,
                            'outcome': outcome, 'bet_on': bet_on})

    return {
        "success":   True,
        "dragon":    dragon,
        "tiger":     tiger,
        "outcome":   outcome,
        "bet_on":    bet_on,
        "user_wins": user_wins,
        "mult":      mult,
        "win":       win,
        "bet":       bet,
        "tie_push":  (outcome == 'tie' and bet_on != 'tie'),
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  KENO — pick numbers, watch balls drop
# ══════════════════════════════════════════════════════════
# ============================================================

# Keno payout table: (picks, hits) → multiplier
KENO_PAYOUTS = {
    1:  {1: 3.5},
    2:  {1: 1.5, 2: 10},
    3:  {2: 3,   3: 25},
    4:  {2: 1.5, 3: 6,  4: 60},
    5:  {3: 3,   4: 12, 5: 100},
    6:  {3: 2,   4: 6,  5: 30, 6: 250},
    7:  {4: 4,   5: 15, 6: 60, 7: 500},
    8:  {4: 3,   5: 8,  6: 30, 7: 200, 8: 1000},
    9:  {4: 2,   5: 5,  6: 15, 7: 80,  8: 500,  9: 2000},
    10: {5: 4,   6: 10, 7: 40, 8: 200, 9: 1000, 10:5000},
}

class KenoRequest(BaseModel):
    amount: float
    picks:  List[int]   # 1-80, pick 1-10

@router.post("/keno/play")
async def keno_play(req: KenoRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("keno")
    bet     = clamp_bet(req.amount)
    picks   = list(set(req.picks))   # deduplicate

    if not 1 <= len(picks) <= 10:
        raise HTTPException(400, "Pick 1–10 numbers")
    if any(n < 1 or n > 80 for n in picks):
        raise HTTPException(400, "Numbers must be 1–80")

    # Draw 20 numbers
    drawn = secure_shuffle(list(range(1, 81)))[:20]
    hits  = [n for n in picks if n in drawn]
    n_hits = len(hits)
    n_picks = len(picks)

    payout_table = KENO_PAYOUTS.get(n_picks, {})
    mult = payout_table.get(n_hits, 0)

    async with (await get_db()).acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, bet, conn):
                raise HTTPException(400, "Insufficient balance")

            win = apply_house(bet * mult) if mult else 0
            if win:
                win = await credit_win(user_id, win, conn)

            await log_game(conn, user_id, 'keno', bet, win,
                           {'picks': picks, 'drawn': drawn,
                            'hits': hits, 'mult': mult})

    return {
        "success": True,
        "picks":   picks,
        "drawn":   drawn,
        "hits":    hits,
        "n_hits":  n_hits,
        "mult":    mult,
        "win":     win,
        "bet":     bet,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  CRASH — multiplayer shared rounds via WebSocket
# ══════════════════════════════════════════════════════════
# ============================================================

class CrashRoom:
    """Manages a single Crash round for up to 4 real players + bots."""
    MAX_PLAYERS   = 4
    BETTING_SECS  = 10   # lobby countdown before round starts
    BOT_NAMES     = ['🤖 CrashBot_X', '🤖 AceBot', '🤖 DareBot']

    def __init__(self, room_id: str):
        self.room_id     = room_id
        self.players:    Dict[int, Dict] = {}   # user_id → {bet, cashed_out, cashout_at, username, is_bot}
        self.ws_map:     Dict[int, WebSocket] = {}
        self.ws_set:     Set[WebSocket] = set()
        self.phase       = 'betting'    # betting | running | ended
        self.crash_at    = 1.0
        self.current_mult= 1.0
        self.start_time  = 0.0
        self.task:       Optional[asyncio.Task] = None
        self.speed       = 0.06         # multiplier growth rate

    async def broadcast(self, msg: dict):
        dead = await broadcast_to_set(self.ws_set, msg)
        self.ws_set -= dead

    async def run(self):
        """Full round lifecycle: betting → launch → crash → settle."""
        self.crash_at = shared.generate_crash_point(house_edge=HOUSE_EDGE)

        # Broadcast betting phase
        await self.broadcast({
            'type':        'betting_open',
            'room_id':     self.room_id,
            'betting_secs': self.BETTING_SECS,
        })

        # Betting countdown ticks
        for sec in range(self.BETTING_SECS, 0, -1):
            await asyncio.sleep(1)
            await self.broadcast({'type': 'betting_tick', 'seconds': sec - 1})
            if sec == 4:
                self._fill_bots()

        # Add bots if < 2 real players
        if sum(1 for p in self.players.values() if not p['is_bot']) < 1:
            self._fill_bots(force=True)

        # Launch
        self.phase      = 'running'
        self.start_time = time.time()
        await self.broadcast({'type': 'round_start', 'crash_at_hidden': True})

        # Tick every 100ms
        while True:
            await asyncio.sleep(0.1)
            elapsed          = time.time() - self.start_time
            self.current_mult = round(math.e ** (self.speed * elapsed), 2)

            # Auto cashout — bots and any player with an auto-cashout target set.
            # Checked against the server's own current_mult (same clock the round
            # itself runs on), so this fires at exactly the requested multiplier
            # instead of drifting late due to client round-trip/timer delay.
            for uid, p in self.players.items():
                if p['cashed_out']:
                    continue
                if p['is_bot']:
                    bot_target = p.get('bot_cashout', 2.0)
                    if self.current_mult >= bot_target:
                        await self._cashout_player(uid, self.current_mult)
                elif p.get('auto_cashout') and self.current_mult >= p['auto_cashout']:
                    await self._cashout_player(uid, self.current_mult)

            await self.broadcast({
                'type': 'tick',
                'mult': self.current_mult,
            })

            if self.current_mult >= self.crash_at:
                break

        # Crash!
        self.phase = 'ended'
        await self.broadcast({
            'type':     'crashed',
            'crash_at': self.crash_at,
        })

        # Settle: everyone still in loses
        pool = await get_db()
        async with pool.acquire() as conn:
            for uid, p in self.players.items():
                if not p['cashed_out'] and not p['is_bot']:
                    await log_game(conn, uid, 'crash', p['bet'], 0,
                                   {'room': self.room_id,
                                    'crash_at': self.crash_at,
                                    'cashed_out': False})

        await asyncio.sleep(3)
        self.phase = 'ended'
        async with _crash_room_lock:
            _crash_rooms.pop(self.room_id, None)

    def _fill_bots(self, force=False):
        """Add bot players with random strategies."""
        n_real = sum(1 for p in self.players.values() if not p['is_bot'])
        n_bots = min(self.MAX_PLAYERS - n_real, 3)
        for i in range(n_bots):
            bid = -(i + 1)
            if bid in self.players:
                continue
            bot_cashout = round(1.2 + secure_random() * 3.3, 2)
            self.players[bid] = {
                'bet':        secure_choice([100, 250, 500, 1000]),
                'cashed_out': False,
                'cashout_at': None,
                'username':   self.BOT_NAMES[i % len(self.BOT_NAMES)],
                'is_bot':     True,
                'bot_cashout': bot_cashout,
            }

    async def _cashout_player(self, user_id: int, at_mult: float):
        p = self.players.get(user_id)
        if not p or p['cashed_out']:
            return
        # Mark immediately to block a concurrent cashout attempt. If the DB
        # credit later fails we roll this back so the player can retry.
        p['cashed_out'] = True
        p['cashout_at'] = at_mult
        win = 0.0

        if not p['is_bot']:
            win = apply_house(p['bet'] * at_mult)
            pool = await get_db()
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        win = await credit_win(user_id, win, conn)
                        await log_game(conn, user_id, 'crash', p['bet'], win,
                                       {'room': self.room_id,
                                        'crash_at': self.crash_at,
                                        'cashed_out_at': at_mult})
            except Exception as e:
                logger.error(f"Crash cashout DB error user {user_id}: {e}")
                p['cashed_out'] = False   # Allow player to retry
                return 0.0

        await self.broadcast({
            'type':       'cashout',
            'user_id':    user_id,
            'username':   p['username'],
            'at_mult':    at_mult,
            'win':        win,
            'is_bot':     p['is_bot'],
            'bot_name':   shared.BOT_NAMES.get(user_id) if p['is_bot'] else None,
        })
        return win


# Global crash room registry
_crash_rooms:     Dict[str, CrashRoom] = {}
_crash_room_lock  = asyncio.Lock()

def _get_or_create_room() -> CrashRoom:
    """Return an open room or create a new one."""
    for room in _crash_rooms.values():
        real_players = sum(1 for p in room.players.values() if not p['is_bot'])
        if room.phase == 'betting' and real_players < CrashRoom.MAX_PLAYERS:
            return room
    room_id  = f"crash_{int(time.time() * 1000) % 999999}"
    room     = CrashRoom(room_id)
    _crash_rooms[room_id] = room
    return room


class CrashBetRequest(BaseModel):
    amount: float
    auto_cashout: Optional[float] = None

class CrashCashoutRequest(BaseModel):
    room_id: str

@router.post("/crash/bet")
async def crash_bet(req: CrashBetRequest, request: Request):
    """Place a bet and get assigned to a room."""
    user_id = await require_auth(request)
    await require_game_enabled("crash")
    bet     = clamp_bet(req.amount)

    auto_cashout = None
    if req.auto_cashout is not None:
        auto_cashout = max(1.01, round(float(req.auto_cashout), 2))

    async with _crash_room_lock:
        for existing_room in _crash_rooms.values():
            if user_id in existing_room.players and existing_room.phase in ('betting', 'running'):
                raise HTTPException(400, "Already in an active round")

        room = _get_or_create_room()

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")
                user_row = await conn.fetchrow(
                    "SELECT username FROM users WHERE user_id=$1", user_id
                )

        username = user_row['username'] if user_row else f'Player {user_id}'
        room.players[user_id] = {
            'bet':          bet,
            'cashed_out':   False,
            'cashout_at':   None,
            'username':     username,
            'is_bot':       False,
            'auto_cashout': auto_cashout,
        }

        # Start the room task if not running
        if room.task is None or room.task.done():
            room.task = asyncio.create_task(room.run())

    return {
        "success":      True,
        "room_id":      room.room_id,
        "bet":          bet,
        "betting_secs": CrashRoom.BETTING_SECS,
    }


@router.post("/crash/cashout")
async def crash_cashout(req: CrashCashoutRequest, request: Request):
    """Cash out during an active round."""
    user_id = await require_auth(request)
    room_id = req.room_id
    async with _crash_room_lock:
        room = _crash_rooms.get(room_id)
        if not room or room.phase != 'running':
            raise HTTPException(400, "No active round to cash out from")

    p = room.players.get(user_id)
    if not p:
        raise HTTPException(400, "Not in this round")
    if p['cashed_out']:
        raise HTTPException(400, "Already cashed out")

    win = await room._cashout_player(user_id, room.current_mult)
    return {"success": True, "cashed_out_at": room.current_mult, "win": win}


@router.websocket("/crash/ws/{room_id}")
async def crash_ws(websocket: WebSocket, room_id: str):
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
    async with _crash_room_lock:
        room = _crash_rooms.get(room_id)
    if not room:
        try:
            await websocket.send_json({'type': 'no_room', 'room_id': room_id})
        except Exception:
            pass
        await websocket.close()
        return

    is_player = user_id in room.players
    if is_player:
        room.ws_map[user_id] = websocket
    room.ws_set.add(websocket)

    # Send current state
    try:
        await websocket.send_json({
            'type':         'room_state',
            'room_id':      room_id,
            'phase':        room.phase,
            'current_mult': room.current_mult,
            'players': [
                {
                    'username':    p['username'],
                    'bet':         p['bet'],
                    'cashed_out':  p['cashed_out'],
                    'cashout_at':  p['cashout_at'],
                    'is_bot':      p['is_bot'],
                }
                for p in room.players.values()
            ],
        })
    except Exception:
        pass

    try:
        while True:
            data = await websocket.receive_json()
            # Only 'ping' allowed — all actions via HTTP
            if data.get('type') == 'ping':
                await websocket.send_json({'type': 'pong'})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        room.ws_set.discard(websocket)
        room.ws_map.pop(user_id, None)


@router.get("/crash/rooms")
async def crash_rooms():
    """List open rooms for the lobby."""
    async with _crash_room_lock:
        rooms = [
        {
            'room_id':     rid,
            'phase':       r.phase,
            'current_mult': r.current_mult,
            'player_count': len(r.players),
        }
            for rid, r in _crash_rooms.items()
            if r.phase in ('betting', 'running')
        ]
    return rooms


