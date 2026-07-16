# ============================================================
# routes/games_hard.py
# CS2CaseBot | Hard Games Backend
#
# Games: Slide, Mystery Box, Russian Roulette (bot AI),
#        Baccarat, Blackjack
# ============================================================

import json
import asyncio
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

import shared
from shared import (
    logger, get_db, require_auth, ensure_user_exists,
    deduct_balance, add_balance, convert_decimals,
    secure_random, secure_randint, secure_choice, secure_shuffle,
    apply_house_edge, HOUSE_EDGE, credit_win, require_game_enabled,
)

router = APIRouter(prefix="/api/games/hard", tags=["games-hard"])

MIN_BET    = 50
MAX_BET    = 750_000

def clamp_bet(v: float) -> float:
    return shared.clamp_bet(v, MIN_BET, MAX_BET)

def apply_house(raw: float) -> float:
    return shared.apply_house(raw, HOUSE_EDGE)

async def log_game(conn, user_id: int, game_type: str,
                   bet: float, win: float, meta: dict = None):
    await shared.log_game(conn, user_id, game_type, bet, win, meta,
                          win_inclusive=True)

SESSION_TTL = 3600   # seconds before an abandoned in-memory session auto-expires

def _is_session_expired(sess: dict) -> bool:
    created = sess.get('created_at')
    if not created:
        return False
    return (datetime.now(timezone.utc) - created).total_seconds() > SESSION_TTL

# ============================================================
# ══════════════════════════════════════════════════════════
#  SLIDE  —  Launch a puck, land in a multiplier zone
# ══════════════════════════════════════════════════════════
# ============================================================
#
# The player sets a "power" (0.0 – 1.0) which determines
# the launch velocity. The puck slides with physics decay
# and lands in one of 9 zones. Zones near 0 and 1 power
# pay the most; landing perfectly in the middle pays least.
# The server applies slight randomness ("friction jitter")
# so perfect prediction is impossible.
#
# Zone layout (0-8, left to right):
#  [5x][3x][2x][1.5x][0.5x][1.5x][2x][3x][5x]
# The puck's final position maps to these zones.

SLIDE_ZONES = [5.0, 3.0, 2.0, 1.5, 0.5, 1.5, 2.0, 3.0, 5.0]
SLIDE_ZONE_NAMES = [
    '5× PERFECT LEFT', '3× GREAT', '2× GOOD',
    '1.5× OK', '0.5× CENTER', '1.5× OK',
    '2× GOOD', '3× GREAT', '5× PERFECT RIGHT',
]

def simulate_slide(power: float) -> tuple[int, float, float]:
    """
    Simulate the puck using given power (0.0–1.0).
    Returns (zone_index 0-8, final_position 0.0-1.0, actual_power_with_jitter).
    """
    # Clamp power
    power = max(0.0, min(1.0, power))
    # Add friction jitter — up to ±12% deviation
    jitter = -0.12 + secure_random() * 0.24
    actual_power = max(0.0, min(1.0, power + jitter))
    # Map power to zone (0 power → zone 0, 1.0 power → zone 8)
    zone = min(8, int(actual_power * 9))
    return zone, actual_power, power

class SlideRequest(BaseModel):
    amount: float
    power:  float   # 0.0–1.0

@router.post("/slide")
async def slide(req: SlideRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("slide")
    bet     = clamp_bet(req.amount)
    power   = max(0.0, min(1.0, float(req.power)))

    zone, actual_power, input_power = simulate_slide(power)
    mult = SLIDE_ZONES[zone]
    win  = apply_house(bet * mult) if mult > 1 else (
           round(bet * mult, 2) if mult > 0 else 0
    )

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, bet, conn):
                raise HTTPException(400, "Insufficient balance")
            if win:
                win = await credit_win(user_id, win, conn)
            await log_game(conn, user_id, 'slide', bet, win, {
                'input_power': round(input_power, 3),
                'actual_power': round(actual_power, 3),
                'zone': zone,
                'mult': mult,
            })

    return {
        "success":      True,
        "zone":         zone,
        "zone_name":    SLIDE_ZONE_NAMES[zone],
        "input_power":  round(input_power, 3),
        "actual_power": round(actual_power, 3),
        "mult":         mult,
        "win":          win,
        "bet":          bet,
        "zones":        SLIDE_ZONES,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  MYSTERY BOX  —  CS2 boxes: open for multiplier or bomb
# ══════════════════════════════════════════════════════════
# ============================================================
#
# Player sees a grid of N boxes (default 9).
# Each box hides either a multiplier or a bomb.
# Opening multipliers accumulates their values (additive).
# Opening a bomb ends the game — lose everything accumulated.
# At any time the player can cashout total accumulated mult × bet.
# Harder tiers have more bombs; rarer boxes pay more.

# Box contents for a 9-box grid: (content_type, value, weight)
MYSTERY_BOX_CONTENTS = [
    ('mult',  0.5,   20),   # small multiplier
    ('mult',  1.0,   18),   # break-even
    ('mult',  1.5,   15),   # slight profit
    ('mult',  2.0,   12),
    ('mult',  3.0,   8),
    ('mult',  5.0,   5),
    ('mult',  10.0,  3),
    ('mult',  25.0,  1),    # rare big hit
    ('bomb',  0,     18),   # bomb!
]

def generate_mystery_grid(size: int = 9, bombs: int = 2) -> List[Dict]:
    """Generate a grid with given bombs count, rest are multipliers."""
    mults = [c for c in MYSTERY_BOX_CONTENTS if c[0] == 'mult']
    grid  = []
    bomb_positions = set(secure_shuffle(list(range(size)))[:bombs])
    for i in range(size):
        if i in bomb_positions:
            grid.append({'type': 'bomb', 'value': 0, 'skin': secure_choice(['💣', '⚠️', '🔴'])})
        else:
            # Weighted random multiplier
            total  = sum(m[2] for m in mults)
            r      = secure_randint(1, total)
            cum    = 0
            chosen = mults[0]
            for m in mults:
                cum += m[2]
                if r <= cum:
                    chosen = m
                    break
            # CS2 skin names for flavour
            skin = secure_choice(['📦', '🔫', '🗡️', '💎', '⭐', '🎯'])
            grid.append({'type': 'mult', 'value': chosen[1], 'skin': skin})
    return grid

MYSTERY_DIFFICULTIES = {
    'easy':   {'size': 9,  'bombs': 1},
    'medium': {'size': 9,  'bombs': 2},
    'hard':   {'size': 12, 'bombs': 3},
    'expert': {'size': 16, 'bombs': 5},
}

_mystery_sessions: Dict[int, Dict] = {}
_mystery_locks:    Dict[int, asyncio.Lock] = {}   # Fix 5: per-user locks

def _get_mystery_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _mystery_locks:
        _mystery_locks[user_id] = asyncio.Lock()
    return _mystery_locks[user_id]

class MysteryStartRequest(BaseModel):
    amount:     float
    difficulty: str = 'medium'

class MysteryOpenRequest(BaseModel):
    box: int

@router.post("/mystery/start")
async def mystery_start(req: MysteryStartRequest, request: Request):
    user_id    = await require_auth(request)
    await require_game_enabled("mystery-box")
    bet        = clamp_bet(req.amount)
    difficulty = req.difficulty if req.difficulty in MYSTERY_DIFFICULTIES else 'medium'
    cfg        = MYSTERY_DIFFICULTIES[difficulty]

    async with _get_mystery_lock(user_id):
        existing_m = _mystery_sessions.get(user_id)
        if existing_m and existing_m.get('active'):
            if _is_session_expired(existing_m):
                _mystery_sessions.pop(user_id, None)
            else:
                raise HTTPException(400, "You already have an active Mystery Box game — cashout or finish first")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")

        grid = generate_mystery_grid(cfg['size'], cfg['bombs'])
        _mystery_sessions[user_id] = {
            'bet':        bet,
            'difficulty': difficulty,
            'grid':       grid,
            'opened':     [],
            'total_mult': 0.0,
            'active':     True,
            'created_at': datetime.now(timezone.utc),
        }

    return {
        "success":    True,
        "size":       cfg['size'],
        "bombs":      cfg['bombs'],
        "difficulty": difficulty,
        "opened":     [],
        "total_mult": 0.0,
    }

@router.post("/mystery/open")
async def mystery_open(req: MysteryOpenRequest, request: Request):
    user_id = await require_auth(request)
    async with _get_mystery_lock(user_id):
        sess    = _mystery_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Mystery Box game — start one first")

        box = req.box
        if box < 0 or box >= len(sess['grid']):
            raise HTTPException(400, f"Box {box} out of range")
        if box in sess['opened']:
            raise HTTPException(400, "Box already opened")

        sess['opened'].append(box)
        content = sess['grid'][box]

        if content['type'] == 'bomb':
            sess['active'] = False
            pool = await get_db()
            async with pool.acquire() as conn:
                await log_game(conn, user_id, 'mystery_box', sess['bet'], 0, {
                    'difficulty': sess['difficulty'],
                    'boxes_opened': len(sess['opened']),
                    'bomb_hit': box,
                    'total_mult': sess['total_mult'],
                })
            _mystery_sessions.pop(user_id, None)
            return {
                "success":    True,
                "type":       "bomb",
                "box":        box,
                "skin":       content['skin'],
                "opened":     sess['opened'],
                "total_mult": sess['total_mult'],
                "all_grid":   sess['grid'],
            }

        # Safe — multiplier box
        sess['total_mult'] = round(sess['total_mult'] + content['value'], 2)
        pot_win = round(sess['bet'] * max(sess['total_mult'], 0), 2)

        # Check if all safe boxes opened
        safe_count  = sum(1 for b in sess['grid'] if b['type'] == 'mult')
        opened_safe = sum(1 for i in sess['opened'] if sess['grid'][i]['type'] == 'mult')
        all_cleared = (opened_safe >= safe_count)

        if all_cleared:
            sess['active'] = False
            win = apply_house(pot_win)
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    win = await credit_win(user_id, win, conn)
                    await log_game(conn, user_id, 'mystery_box', sess['bet'], win, {
                        'difficulty': sess['difficulty'],
                        'boxes_opened': len(sess['opened']),
                        'total_mult': sess['total_mult'],
                        'cleared': True,
                    })
            _mystery_sessions.pop(user_id, None)
            return {
                "success":       True,
                "type":          "mult",
                "box":           box,
                "value":         content['value'],
                "skin":          content['skin'],
                "opened":        sess['opened'],
                "total_mult":    sess['total_mult'],
                "potential_win": win,
                "cleared":       True,
                "auto_win":      win,
            }

        return {
            "success":       True,
            "type":          "mult",
            "box":           box,
            "value":         content['value'],
            "skin":          content['skin'],
            "opened":        sess['opened'],
            "total_mult":    sess['total_mult'],
            "potential_win": pot_win,
            "cleared":       False,
        }

@router.post("/mystery/cashout")
async def mystery_cashout(request: Request):
    user_id = await require_auth(request)
    async with _get_mystery_lock(user_id):
        sess    = _mystery_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Mystery Box game")
        if not sess['opened']:
            raise HTTPException(400, "Open at least one box before cashing out")
        if sess['total_mult'] <= 0:
            raise HTTPException(400, "No positive multiplier to cash out")

        win        = apply_house(sess['bet'] * sess['total_mult'])
        bet        = sess['bet']
        difficulty = sess['difficulty']
        opened     = list(sess['opened'])
        total_mult = sess['total_mult']
        sess['active'] = False

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                win = await credit_win(user_id, win, conn)
                await log_game(conn, user_id, 'mystery_box', bet, win, {
                    'difficulty': difficulty,
                    'boxes_opened': len(opened),
                    'total_mult': total_mult,
                })
        _mystery_sessions.pop(user_id, None)

    return {
        "success":    True,
        "win":        win,
        "total_mult": total_mult,
        "opened":     opened,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  RUSSIAN ROULETTE  —  You vs AI with personality
# ══════════════════════════════════════════════════════════
# ============================================================
#
# 6-chamber revolver, 1 bullet. You and the AI alternate.
# Each pull: server checks if the current chamber is loaded.
# AI has three personalities that affect bluffing text and
# risk tolerance (but NOT the actual probability — fair game).
# Surviving more pulls dramatically increases the payout.
# If you're still alive when the AI fires the bullet — you win.

RR_CHAMBERS = 6

BOT_PERSONALITIES = {
    'calm': {
        'name':   'The Iceman',
        'avatar': '🧊',
        'taunt_safe': [
            "Cool as ever.",
            "Still breathing. Barely interesting.",
            "Your hands are shaking. Mine aren't.",
            "This is nothing to me.",
        ],
        'taunt_tension': [
            "Getting closer now. You feel it?",
            "Every pull narrows the odds. You know the math.",
            "I've done this before. Have you?",
        ],
        'taunt_bust': [
            "I told you.",
            "The math was never in your favour.",
        ],
        'bluff_chance': 0.3,   # 30% chance of a false tell before pulling
    },
    'aggressive': {
        'name':   'The Berserker',
        'avatar': '😤',
        'taunt_safe': [
            "COME ON! IS THAT ALL YOU'VE GOT?",
            "You pulled it! Lucky dog.",
            "I LIVE for this! YOUR TURN.",
            "Hah! Not today!",
        ],
        'taunt_tension': [
            "I can feel it. Can you?! PULL!",
            "The odds are against us BOTH now. BEAUTIFUL.",
            "One of us isn't walking out. I'm FINE with that.",
        ],
        'taunt_bust': [
            "FINALLY! The chaos I craved!",
            "Glorious.",
        ],
        'bluff_chance': 0.6,
    },
    'nervous': {
        'name':   'The Rookie',
        'avatar': '😰',
        'taunt_safe': [
            "Oh thank god… your turn.",
            "I-I thought that was it.",
            "Please… let's just stop here.",
            "My hands won't stop trembling.",
        ],
        'taunt_tension': [
            "I really don't want to do this anymore.",
            "Can we… can we just call it even?",
            "I'm not cut out for this.",
        ],
        'taunt_bust': [
            "I knew it. I always knew.",
            "Tell my squad I said hi.",
        ],
        'bluff_chance': 0.8,   # nervous bot bluffs most — red herring
    },
}

def rr_payout_mult(pulls_survived: int) -> float:
    """
    Payout multiplier for surviving `pulls_survived` player pulls.
    Accounts for increasing probability of the bullet each pull.
    """
    if pulls_survived == 0:
        return 0
    # P(surviving k player pulls out of 6 total, alternating)
    # Simplified: each of your pulls has 1/remaining_chambers chance
    chambers = RR_CHAMBERS
    mult     = 1.0
    remaining = chambers
    for i in range(pulls_survived):
        # Your pull at this step
        if remaining <= 0:
            break
        p_safe = (remaining - 1) / remaining
        mult  *= (1 / p_safe)
        remaining -= 1
        # AI also pulled (and survived) between your pulls
        if remaining > 0:
            remaining -= 1
    return round(apply_house(mult * 1.5), 3)   # 1.5× bonus for courage

_rr_sessions: Dict[int, Dict] = {}
_rr_locks:    Dict[int, asyncio.Lock] = {}   # Fix 5: per-user locks

def _get_rr_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _rr_locks:
        _rr_locks[user_id] = asyncio.Lock()
    return _rr_locks[user_id]

class RRStartRequest(BaseModel):
    amount:      float
    personality: str = 'calm'   # 'calm' | 'aggressive' | 'nervous'

@router.post("/russian-roulette/start")
async def rr_start(req: RRStartRequest, request: Request):
    user_id     = await require_auth(request)
    await require_game_enabled("russian-roulette")
    bet         = clamp_bet(req.amount)
    personality = req.personality if req.personality in BOT_PERSONALITIES else 'calm'
    bot         = BOT_PERSONALITIES[personality]

    async with _get_rr_lock(user_id):
        existing_rr = _rr_sessions.get(user_id)
        if existing_rr and existing_rr.get('active'):
            if _is_session_expired(existing_rr):
                _rr_sessions.pop(user_id, None)
            else:
                raise HTTPException(400, "You already have an active Russian Roulette game — cashout or finish first")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")

        bullet_chamber = secure_randint(1, RR_CHAMBERS)
        _rr_sessions[user_id] = {
            'bet':            bet,
            'personality':    personality,
            'bullet_chamber': bullet_chamber,
            'current_pull':   1,
            'player_pulls':   0,
            'active':         True,
            'turn':           'player',
            'created_at':     datetime.now(timezone.utc),
        }

    return {
        "success":      True,
        "chambers":     RR_CHAMBERS,
        "bot_name":     bot['name'],
        "bot_avatar":   bot['avatar'],
        "turn":         "player",
        "player_pulls": 0,
        "taunt":        secure_choice(bot['taunt_safe']),
        "multiplier":   1.0,
    }

@router.post("/russian-roulette/pull")
async def rr_pull(request: Request):
    """Player pulls the trigger for their turn."""
    user_id = await require_auth(request)
    async with _get_rr_lock(user_id):
        sess    = _rr_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Russian Roulette game")
        if sess['turn'] != 'player':
            raise HTTPException(400, "It's the bot's turn, not yours")

        pull_num = sess['current_pull']
        fired    = (pull_num == sess['bullet_chamber'])

        if fired:
            # Player hits the bullet — bust
            sess['active'] = False
            pool = await get_db()
            async with pool.acquire() as conn:
                await log_game(conn, user_id, 'russian_roulette', sess['bet'], 0, {
                    'personality': sess['personality'],
                    'player_pulls': sess['player_pulls'],
                    'fired_at': pull_num,
                    'bullet_was': sess['bullet_chamber'],
                })
            _rr_sessions.pop(user_id, None)
            bot = BOT_PERSONALITIES[sess['personality']]
            return {
                "success":      True,
                "fired":        True,
                "chamber":      pull_num,
                "player_pulls": sess['player_pulls'],
                "taunt":        secure_choice(bot['taunt_bust']),
            }

        # Player survived — advance
        sess['player_pulls'] += 1
        sess['current_pull'] += 1
        sess['turn']          = 'bot'
        mult = rr_payout_mult(sess['player_pulls'])
        pot  = round(sess['bet'] * mult, 2)
        bot  = BOT_PERSONALITIES[sess['personality']]

        is_bluff   = secure_random() < bot["bluff_chance"]
        tension    = sess['player_pulls'] >= 2
        taunt_pool = bot['taunt_tension'] if tension else bot['taunt_safe']
        taunt      = secure_choice(taunt_pool)

        return {
            "success":       True,
            "fired":         False,
            "chamber":       pull_num,
            "player_pulls":  sess['player_pulls'],
            "multiplier":    mult,
            "potential_win": pot,
            "turn":          "bot",
            "bot_bluff":     is_bluff,
            "taunt":         taunt,
        }

@router.post("/russian-roulette/bot-pull")
async def rr_bot_pull(request: Request):
    """Simulate the bot's pull. Call after player's turn resolves."""
    user_id = await require_auth(request)
    async with _get_rr_lock(user_id):
        sess    = _rr_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Russian Roulette game")
        if sess['turn'] != 'bot':
            raise HTTPException(400, "It's the player's turn, not the bot's")

        pull_num = sess['current_pull']
        fired    = (pull_num == sess['bullet_chamber'])
        bot      = BOT_PERSONALITIES[sess['personality']]

        if fired:
            # Bot hits the bullet — PLAYER WINS
            sess['active'] = False
            mult = rr_payout_mult(sess['player_pulls'])
            win  = round(sess['bet'] * mult, 2)
            # Bug 178 fix: pop session AFTER DB commit so a DB failure doesn't
            # silently discard the player's win with no recovery path.
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    win = await credit_win(user_id, win, conn)
                    await log_game(conn, user_id, 'russian_roulette', sess['bet'], win, {
                        'personality': sess['personality'],
                        'player_pulls': sess['player_pulls'],
                        'bot_fired_at': pull_num,
                    })
            _rr_sessions.pop(user_id, None)
            return {
                "success":      True,
                "bot_fired":    True,
                "chamber":      pull_num,
                "player_pulls": sess['player_pulls'],
                "multiplier":   mult,
                "win":          win,
                "taunt":        secure_choice(bot['taunt_bust']),
            }

        # Bot survived — back to player
        sess['current_pull'] += 1
        sess['turn']          = 'player'
        mult    = rr_payout_mult(sess['player_pulls'])
        pot     = round(sess['bet'] * mult, 2)
        tension = sess['player_pulls'] >= 2
        taunt   = secure_choice(bot['taunt_tension'] if tension else bot['taunt_safe'])

        return {
            "success":       True,
            "bot_fired":     False,
            "chamber":       pull_num,
            "turn":          "player",
            "multiplier":    mult,
            "potential_win": pot,
            "taunt":         taunt,
        }

@router.post("/russian-roulette/cashout")
async def rr_cashout(request: Request):
    user_id = await require_auth(request)
    async with _get_rr_lock(user_id):
        sess    = _rr_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Russian Roulette game")
        if sess['player_pulls'] == 0:
            raise HTTPException(400, "Survive at least one pull before cashing out")
        if sess['turn'] != 'player':
            raise HTTPException(400, "Wait for the bot's turn to resolve first")

        mult         = rr_payout_mult(sess['player_pulls'])
        win          = round(sess['bet'] * mult, 2)
        bet          = sess['bet']
        personality  = sess['personality']
        player_pulls = sess['player_pulls']
        sess['active'] = False

        # Bug 177 fix: do DB commit inside the lock and pop session only after
        # the commit succeeds, so a DB failure cannot silently lose the win.
        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                win = await credit_win(user_id, win, conn)
                await log_game(conn, user_id, 'russian_roulette', bet, win, {
                    'personality': personality,
                    'player_pulls': player_pulls,
                    'cashout': True,
                    'multiplier': mult,
                })
        _rr_sessions.pop(user_id, None)

    return {
        "success":    True,
        "win":        win,
        "multiplier": mult,
        "pulls":      player_pulls,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  BACCARAT  —  Player vs Banker, closest to 9
# ══════════════════════════════════════════════════════════
# ============================================================
#
# Standard punto banco baccarat rules.
# Bet on Player (1:1), Banker (0.95:1 after 5% commission),
# or Tie (8:1). Pairs side bets (11:1) also supported.
# Third-card drawing rules fully implemented.

BACCARAT_CARD_VALUES = {
    'A': 1, '2': 2, '3': 3, '4': 4, '5': 5,
    '6': 6, '7': 7, '8': 8, '9': 9,
    '10': 0, 'J': 0, 'Q': 0, 'K': 0,
}
BACCARAT_RANKS = ['A','2','3','4','5','6','7','8','9','10','J','Q','K']
BACCARAT_SUITS = ['♠','♥','♦','♣']

def new_baccarat_shoe() -> List[Dict]:
    """Create 8-deck shoe shuffled."""
    shoe = []
    for _ in range(8):
        for rank in BACCARAT_RANKS:
            for suit in BACCARAT_SUITS:
                shoe.append({'rank': rank, 'suit': suit,
                             'value': BACCARAT_CARD_VALUES[rank],
                             'display': rank + suit})
    shoe[:] = secure_shuffle(shoe)
    return shoe

def hand_value(cards: List[Dict]) -> int:
    return sum(c['value'] for c in cards) % 10

def baccarat_deal(shoe: List[Dict]) -> tuple:
    """Deal initial 2 cards each to player and banker."""
    p = [shoe.pop(), shoe.pop()]
    b = [shoe.pop(), shoe.pop()]
    return p, b

def baccarat_third_card(player_hand: List[Dict],
                        banker_hand: List[Dict],
                        shoe: List[Dict]) -> tuple:
    """Apply standard third-card drawing rules."""
    pv = hand_value(player_hand)
    bv = hand_value(banker_hand)

    # Natural (8 or 9) — no more cards
    if pv >= 8 or bv >= 8:
        return player_hand, banker_hand

    player_drew = False
    player_third_val = None

    # Player draws on 0-5
    if pv <= 5:
        card = shoe.pop()
        player_hand.append(card)
        player_third_val = card['value']
        player_drew = True

    # Banker drawing rules
    if not player_drew:
        # Player stood on 6 or 7 — banker draws on 0-5
        if bv <= 5:
            banker_hand.append(shoe.pop())
    else:
        # Banker draws based on their value and player's third card
        p3 = player_third_val
        draw_banker = False
        if bv <= 2:
            draw_banker = True
        elif bv == 3 and p3 != 8:
            draw_banker = True
        elif bv == 4 and p3 in [2,3,4,5,6,7]:
            draw_banker = True
        elif bv == 5 and p3 in [4,5,6,7]:
            draw_banker = True
        elif bv == 6 and p3 in [6,7]:
            draw_banker = True
        if draw_banker:
            banker_hand.append(shoe.pop())

    return player_hand, banker_hand

class BaccaratRequest(BaseModel):
    amount:       float
    bet_on:       str   # 'player' | 'banker' | 'tie'
    player_pair:  bool = False
    banker_pair:  bool = False
    side_bet:     float = 0.0   # Fix 26: configurable side bet amount (default 0 = disabled)

@router.post("/baccarat")
async def baccarat(req: BaccaratRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("baccarat")
    bet     = clamp_bet(req.amount)
    bet_on  = req.bet_on.lower()
    if bet_on not in ('player', 'banker', 'tie'):
        raise HTTPException(400, "bet_on must be 'player', 'banker', or 'tie'")

    # Fix 26: side bet can't exceed main bet; default 0 means no side bet
    side_bet = max(0.0, min(float(req.side_bet), bet))

    shoe = new_baccarat_shoe()
    ph, bh = baccarat_deal(shoe)
    ph, bh = baccarat_third_card(ph, bh, shoe)

    pv  = hand_value(ph)
    bv  = hand_value(bh)

    if pv > bv:   outcome = 'player'
    elif bv > pv: outcome = 'banker'
    else:         outcome = 'tie'

    # Payout calculation
    total_bet = bet
    win       = 0.0

    if bet_on == 'player' and outcome == 'player':
        win += apply_house(bet * 2)
    elif bet_on == 'banker' and outcome == 'banker':
        # 5% commission on banker wins IS the house edge — don't double-apply
        win += round(bet * 1.95, 2)
    elif bet_on == 'tie' and outcome == 'tie':
        win += apply_house(bet * 9)
    elif outcome == 'tie' and bet_on in ('player', 'banker'):
        # Push on tie — return bet
        win += bet

    # Side bets — only charged if side_bet > 0
    p_has_pair = (ph[0]['rank'] == ph[1]['rank'])
    b_has_pair = (bh[0]['rank'] == bh[1]['rank'])

    if req.player_pair and side_bet > 0:
        total_bet += side_bet
        if p_has_pair:
            win += apply_house(side_bet * 12)
    if req.banker_pair and side_bet > 0:
        total_bet += side_bet
        if b_has_pair:
            win += apply_house(side_bet * 12)

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, total_bet, conn):
                raise HTTPException(400, "Insufficient balance")
            # High Roller: bets >= $25,000 require 1 ticket
            if total_bet >= 25000:
                from routes.premium import deduct_ticket as _deduct_ticket
                ok = await _deduct_ticket(user_id, 'spend_game',
                                          {'game': 'baccarat', 'bet': total_bet}, conn)
                if not ok:
                    raise HTTPException(400, "High Roller bets require 1 ticket")
            if win:
                win = await credit_win(user_id, win, conn)
            await log_game(conn, user_id, 'baccarat', total_bet, win, {
                'bet_on': bet_on, 'outcome': outcome,
                'player_value': pv, 'banker_value': bv,
                'side_bet': side_bet,
            })

    return {
        "success":       True,
        "player_hand":   ph,
        "banker_hand":   bh,
        "player_value":  pv,
        "banker_value":  bv,
        "outcome":       outcome,
        "bet_on":        bet_on,
        "win":           round(win, 2),
        "total_bet":     total_bet,
        "profit":        round(win - total_bet, 2),
        "player_pair":   p_has_pair,
        "banker_pair":   b_has_pair,
        "natural":       pv >= 8 or bv >= 8,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  BLACKJACK  —  Beat the dealer, full rules
# ══════════════════════════════════════════════════════════
# ============================================================
#
# Standard Vegas rules: 6-deck shoe, dealer stands on soft 17,
# blackjack pays 3:2, double down on any 2 cards,
# split up to 3 times (no re-split aces), insurance offered.
# Player can: hit, stand, double down, split, buy insurance.
# Stateful session supports multi-hand splits.

BJ_RANKS  = ['A','2','3','4','5','6','7','8','9','10','J','Q','K']
BJ_SUITS  = ['♠','♥','♦','♣']
BJ_VALUES = {'A':11,'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,
             '8':8,'9':9,'10':10,'J':10,'Q':10,'K':10}

def new_bj_shoe(decks: int = 6) -> List[Dict]:
    shoe = []
    for _ in range(decks):
        for rank in BJ_RANKS:
            for suit in BJ_SUITS:
                shoe.append({'rank': rank, 'suit': suit,
                             'display': rank + suit})
    shoe[:] = secure_shuffle(shoe)
    return shoe

def bj_hand_value(hand: List[Dict]) -> tuple[int, bool]:
    """Returns (best_value, is_soft)."""
    total  = 0
    aces   = 0
    for c in hand:
        v = BJ_VALUES[c['rank']]
        if c['rank'] == 'A':
            aces += 1
        total += v
    # Reduce aces from 11→1 to avoid bust
    is_soft = aces > 0
    while total > 21 and aces:
        total -= 10
        aces  -= 1
        is_soft = aces > 0
    return total, is_soft

def is_blackjack(hand: List[Dict]) -> bool:
    return (len(hand) == 2 and
            bj_hand_value(hand)[0] == 21)

def dealer_play(hand: List[Dict], shoe: List[Dict]) -> List[Dict]:
    """Dealer draws until 17+. Stands on soft 17."""
    while True:
        val, soft = bj_hand_value(hand)
        if val > 17:
            break
        if val == 17:
            break   # stand on all 17s (soft or hard)
        hand.append(shoe.pop())
    return hand

_bj_sessions: Dict[int, Dict] = {}
_bj_locks:    Dict[int, asyncio.Lock] = {}   # Fix 5: per-user locks

def _get_bj_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _bj_locks:
        _bj_locks[user_id] = asyncio.Lock()
    return _bj_locks[user_id]

class BJStartRequest(BaseModel):
    amount: float

class BJActionRequest(BaseModel):
    action:    str   # 'hit' | 'stand' | 'double' | 'split' | 'insurance'
    hand_idx:  int = 0   # for splits

@router.post("/blackjack/deal")
async def bj_deal(req: BJStartRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("blackjack")
    bet     = clamp_bet(req.amount)

    async with _get_bj_lock(user_id):
        existing_bj = _bj_sessions.get(user_id)
        if existing_bj and existing_bj.get('active'):
            if _is_session_expired(existing_bj):
                _bj_sessions.pop(user_id, None)
            else:
                raise HTTPException(400, "You already have an active Blackjack game — finish or abandon it first")

        shoe   = new_bj_shoe()
        p_hand = [shoe.pop(), shoe.pop()]
        d_hand = [shoe.pop(), shoe.pop()]

        # Check immediate blackjack
        p_bj = is_blackjack(p_hand)
        d_bj = is_blackjack(d_hand)

        session = {
            'bet':         bet,
            'shoe':        shoe,
            'hands':       [p_hand],
            'bets':        [bet],
            'dealer':      d_hand,
            'active_hand': 0,
            'doubled':     [False],
            'stood':       [False],
            'insurance':   0.0,
            'active':      True,
            'done':        False,
            'created_at':  datetime.now(timezone.utc),
        }

        # Use a single connection for both the bet deduction and any immediate
        # blackjack win credit so a pool-exhaustion error can never permanently
        # lose the player's bet after it has already been deducted.
        _immediate_result = None
        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")
                # High Roller: bets >= $25,000 require 1 ticket
                if bet >= 25000:
                    from routes.premium import deduct_ticket as _deduct_ticket
                    ok = await _deduct_ticket(user_id, 'spend_game',
                                              {'game': 'blackjack', 'bet': bet}, conn)
                    if not ok:
                        raise HTTPException(400, "High Roller bets require 1 ticket")

                # Immediate resolution if either has blackjack
                if p_bj or d_bj:
                    session['active'] = False
                    session['done']   = True
                    win = 0.0
                    result = ''
                    if p_bj and d_bj:
                        win    = bet   # push
                        result = 'push'
                    elif p_bj:
                        win    = apply_house(bet * 2.5)   # 3:2 payout
                        result = 'blackjack'
                    else:
                        win    = 0
                        result = 'dealer_blackjack'

                    if win:
                        win = await credit_win(user_id, win, conn)
                    await log_game(conn, user_id, 'blackjack', bet, win, {
                        'result': result, 'player_bj': p_bj, 'dealer_bj': d_bj,
                    })
                    _immediate_result = {
                        "success":       True,
                        "player_hands":  [p_hand],
                        "dealer_hand":   d_hand,
                        "player_values": [bj_hand_value(p_hand)[0]],
                        "dealer_value":  bj_hand_value(d_hand)[0],
                        "blackjack":     True,
                        "result":        result,
                        "win":           round(win, 2),
                        "done":          True,
                    }

        if _immediate_result is not None:
            _bj_sessions.pop(user_id, None)
            return _immediate_result

        # Insurance offer when dealer shows Ace
        offer_insurance = (d_hand[0]['rank'] == 'A')
        _bj_sessions[user_id] = session

    return {
        "success":          True,
        "player_hands":     [p_hand],
        "dealer_hand":      [d_hand[0], {'rank':'?','suit':'?','display':'??'}],
        "player_values":    [bj_hand_value(p_hand)[0]],
        "dealer_shown":     bj_hand_value([d_hand[0]])[0],
        "blackjack":        False,
        "offer_insurance":  offer_insurance,
        "can_split":        (p_hand[0]['rank'] == p_hand[1]['rank']),
        "can_double":       True,
        "done":             False,
    }

@router.post("/blackjack/action")
async def bj_action(req: BJActionRequest, request: Request):
    user_id = await require_auth(request)
    async with _get_bj_lock(user_id):
        sess    = _bj_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Blackjack game — deal first")

        action   = req.action.lower()
        hand_idx = req.hand_idx
        shoe     = sess['shoe']

        if hand_idx < 0 or hand_idx >= len(sess['hands']):
            raise HTTPException(400, "Invalid hand index")

        hand = sess['hands'][hand_idx]
        bet  = sess['bets'][hand_idx]

        # ── INSURANCE ──────────────────────────────────────
        if action == 'insurance':
            if sess['insurance']:
                raise HTTPException(400, "Insurance already purchased")
            # Bug 180 fix: insurance only valid when dealer shows Ace and before
            # any action (all player hands still at 2 cards, no splits yet).
            if sess['dealer'][0]['rank'] != 'A':
                raise HTTPException(400, "Insurance only available when dealer shows an Ace")
            if len(sess['hands']) > 1 or len(sess['hands'][0]) > 2:
                raise HTTPException(400, "Insurance must be purchased before any action")
            ins_cost = round(bet / 2, 2)
            pool = await get_db()
            async with pool.acquire() as conn:
                if not await deduct_balance(user_id, ins_cost, conn):
                    raise HTTPException(400, "Insufficient balance for insurance")
            sess['insurance'] = ins_cost
            return {"success": True, "action": "insurance", "cost": ins_cost}

        # ── HIT ────────────────────────────────────────────
        if action == 'hit':
            hand.append(shoe.pop())
            val, soft = bj_hand_value(hand)
            if val > 21:
                sess['stood'][hand_idx] = True
                return await _check_bj_completion(user_id, sess, hand_idx)
            return {
                "success":    True,
                "action":     "hit",
                "hand":       hand,
                "value":      val,
                "soft":       soft,
                "bust":       False,
                "can_double": False,
            }

        # ── STAND ──────────────────────────────────────────
        if action == 'stand':
            sess['stood'][hand_idx] = True
            return await _check_bj_completion(user_id, sess, hand_idx)

        # ── DOUBLE DOWN ────────────────────────────────────
        if action == 'double':
            if len(hand) != 2:
                raise HTTPException(400, "Can only double on first two cards")
            pool = await get_db()
            async with pool.acquire() as conn:
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance to double")
            sess['bets'][hand_idx]    = bet * 2
            sess['doubled'][hand_idx] = True
            hand.append(shoe.pop())
            sess['stood'][hand_idx] = True
            return await _check_bj_completion(user_id, sess, hand_idx)

        # ── SPLIT ──────────────────────────────────────────
        if action == 'split':
            if len(hand) != 2:
                raise HTTPException(400, "Can only split on first two cards")
            if hand[0]['rank'] != hand[1]['rank']:
                raise HTTPException(400, "Can only split matching ranks")
            if len(sess['hands']) >= 4:
                raise HTTPException(400, "Maximum 4 hands after splits")
            pool = await get_db()
            async with pool.acquire() as conn:
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance to split")

            splitting_aces = (hand[0]['rank'] == 'A')
            c1, c2 = hand[0], hand[1]
            new_card_1 = shoe.pop()
            new_card_2 = shoe.pop()
            sess['hands'][hand_idx] = [c1, new_card_1]
            sess['hands'].append([c2, new_card_2])
            sess['bets'].append(bet)
            sess['doubled'].append(False)

            if splitting_aces:
                sess['stood'].append(True)
                sess['stood'][hand_idx] = True
            else:
                sess['stood'].append(False)

            vals = [bj_hand_value(h)[0] for h in sess['hands']]

            if splitting_aces:
                return await _check_bj_completion(user_id, sess, hand_idx)

            return {
                "success":     True,
                "action":      "split",
                "hands":       sess['hands'],
                "values":      vals,
                "hand_count":  len(sess['hands']),
                "active_hand": hand_idx,
            }

        raise HTTPException(400, f"Unknown action: {action}")


async def _check_bj_completion(user_id: int, sess: Dict, current_hand: int) -> Dict:
    """Check if all hands are done and resolve if so."""
    # Find the next hand that hasn't been stood yet
    # Start searching AFTER current_hand to avoid re-triggering same hand
    for i in range(len(sess['stood'])):
        if not sess['stood'][i]:
            sess['active_hand'] = i
            hand = sess['hands'][i]
            val, soft = bj_hand_value(hand)
            # Check if this hand is 21 on split (auto-stand it)
            if val == 21:
                sess['stood'][i] = True
                continue   # keep scanning for next unfinished hand
            return {
                "success":    True,
                "action":     "next_hand",
                "hand_idx":   i,
                "hand":       hand,
                "value":      val,
                "soft":       soft,
                "all_done":   False,
                "can_double": len(hand) == 2,
                "can_split":  (len(hand) == 2 and
                               hand[0]['rank'] == hand[1]['rank'] and
                               len(sess['hands']) < 4),
            }

    # All hands resolved — dealer plays
    sess['active'] = False
    dealer_played  = dealer_play(sess['dealer'], sess['shoe'])
    dv, _          = bj_hand_value(dealer_played)
    d_bust         = dv > 21
    d_bj           = is_blackjack(dealer_played)

    total_win = 0.0
    results   = []
    for i, hand in enumerate(sess['hands']):
        pv, _  = bj_hand_value(hand)
        bet_i  = sess['bets'][i]
        p_bust = pv > 21

        # Blackjack on original deal (not split) pays 3:2
        p_bj = is_blackjack(hand) and i == 0 and len(sess['hands']) == 1

        if p_bust:
            win_i = 0
            res   = 'bust'
        elif d_bj and not p_bj:
            win_i = 0
            res   = 'loss'
        elif p_bj and not d_bj:
            win_i = apply_house(bet_i * 2.5)   # 3:2
            res   = 'blackjack'
        elif p_bj and d_bj:
            win_i = bet_i   # push
            res   = 'push'
        elif d_bust or pv > dv:
            win_i = apply_house(bet_i * 2)
            res   = 'win'
        elif pv == dv:
            win_i = bet_i
            res   = 'push'
        else:
            win_i = 0
            res   = 'loss'

        total_win += win_i
        results.append({'hand': hand, 'value': pv, 'bet': bet_i,
                        'win': win_i, 'result': res})

    # Insurance payout
    if sess['insurance'] and d_bj:
        total_win += sess['insurance'] * 3

    # Bug 176 fix: pop session AFTER DB commit so a DB failure doesn't silently
    # discard a pending win with no recovery path.
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if total_win:
                total_win = await credit_win(user_id, total_win, conn)
            total_bet = sum(sess['bets']) + sess['insurance']
            await log_game(conn, user_id, 'blackjack', total_bet, total_win, {
                'hands': len(sess['hands']),
                'dealer_value': dv,
                'results': [r['result'] for r in results],
            })
    _bj_sessions.pop(user_id, None)

    return {
        "success":       True,
        "all_done":      True,
        "dealer_hand":   dealer_played,
        "dealer_value":  dv,
        "dealer_bust":   d_bust,
        "results":       results,
        "total_win":     round(total_win, 2),
        "total_bet":     sum(sess['bets']),
        "profit":        round(total_win - sum(sess['bets']), 2),
        "insurance_win": sess['insurance'] * 3 if sess['insurance'] and d_bj else 0,
    }

@router.get("/blackjack/state")
async def bj_state(request: Request):
    user_id = await require_auth(request)
    async with _bj_locks.setdefault(user_id, asyncio.Lock()):
        sess = _bj_sessions.get(user_id)
    if not sess or not sess['active']:
        return {"active": False}
    return {
        "active":       True,
        "hands":        sess['hands'],
        "bets":         sess['bets'],
        "dealer_shown": sess['dealer'][0],
        "active_hand":  sess['active_hand'],
        "can_split":    (len(sess['hands'][0]) == 2 and
                         sess['hands'][0][0]['rank'] == sess['hands'][0][1]['rank']
                         and len(sess['hands']) < 4),
    }


