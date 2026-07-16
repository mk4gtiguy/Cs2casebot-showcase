# ============================================================
# routes/games_medium.py
# CS2CaseBot | Medium Games Backend
#
# Games: Mines, Plinko, Tower, Shotgun, Ladder Climb, Roulette
# ============================================================

import json
import asyncio
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

router = APIRouter(prefix="/api/games/medium", tags=["games-medium"])

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

# ============================================================
# ══════════════════════════════════════════════════════════
#  MINES  —  Reveal tiles, avoid bombs
# ══════════════════════════════════════════════════════════
# ============================================================
#
# 5×5 grid (25 tiles). Player picks mine count (1-24).
# Each safe reveal increases the multiplier.
# Multiplier formula: (tiles_total / remaining_safe) accumulated.
# Player can cashout at any time or hit a mine and lose.

SESSION_TTL = 3600   # 1 hour — abandon sessions older than this

def _is_session_expired(sess: dict) -> bool:
    created = sess.get('created_at')
    if not created:
        return False
    return (datetime.now(timezone.utc) - created).total_seconds() > SESSION_TTL

MINES_GRID = 25

def mines_multiplier(total: int, mines: int, revealed: int) -> float:
    """
    Running multiplier after `revealed` safe tiles uncovered.
    Uses hypergeometric probability: product of (safe_remaining / total_remaining)
    inverted for each pick, cumulated and house-edged.
    """
    if revealed == 0:
        return 1.0
    safe_total = total - mines
    mult = 1.0
    for i in range(revealed):
        remaining       = total - i
        safe_remaining  = safe_total - i
        if safe_remaining <= 0 or remaining <= 0:
            break
        # Probability of picking safe tile at step i
        p_safe = safe_remaining / remaining
        mult  *= (1 / p_safe)
    return round(apply_house(mult), 4)

# Active mine sessions: user_id → state
_mine_sessions: Dict[int, Dict] = {}
_mine_locks:    Dict[int, asyncio.Lock] = {}   # Fix 5: per-user locks

def _get_mine_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _mine_locks:
        _mine_locks[user_id] = asyncio.Lock()
    return _mine_locks[user_id]

class MinesStartRequest(BaseModel):
    amount:     float
    mine_count: int = 3   # 1–24

class MinesRevealRequest(BaseModel):
    tile: int   # 0–24

@router.post("/mines/start")
async def mines_start(req: MinesStartRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("mines")
    bet     = clamp_bet(req.amount)
    mines   = max(1, min(24, req.mine_count))

    async with _get_mine_lock(user_id):
        existing = _mine_sessions.get(user_id, {})
        if existing.get('active'):
            if _is_session_expired(existing):
                _mine_sessions.pop(user_id, None)   # discard stale session; bet is forfeit
            else:
                raise HTTPException(400, "You already have an active Mines game — cashout or finish first")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")

        # Place mines randomly — hidden from player
        positions = secure_shuffle(list(range(MINES_GRID)))[:mines]
        _mine_sessions[user_id] = {
            'bet':        bet,
            'mines':      mines,
            'positions':  positions,
            'revealed':   [],
            'active':     True,
            'created_at': datetime.now(timezone.utc),
        }

    return {
        "success":    True,
        "mine_count": mines,
        "grid_size":  MINES_GRID,
        "multiplier": 1.0,
        "revealed":   [],
    }

@router.post("/mines/reveal")
async def mines_reveal(req: MinesRevealRequest, request: Request):
    user_id = await require_auth(request)
    async with _get_mine_lock(user_id):
        sess = _mine_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Mines game — start one first")

        tile = req.tile
        if tile < 0 or tile >= MINES_GRID:
            raise HTTPException(400, f"Tile must be 0–{MINES_GRID - 1}")
        if tile in sess['revealed']:
            raise HTTPException(400, "Tile already revealed")

        sess['revealed'].append(tile)

        if tile in sess['positions']:
            # Hit a mine — game over, lose bet
            sess['active'] = False
            all_mines = sess['positions']
            bet = sess['bet']
            mines_count = sess['mines']
            revealed_count = len(sess['revealed'])
            pool = await get_db()
            async with pool.acquire() as conn:
                await log_game(conn, user_id, 'mines', bet, 0, {
                    'mines': mines_count, 'revealed': revealed_count,
                    'bust_tile': tile,
                })
            _mine_sessions.pop(user_id, None)
            return {
                "success":   True,
                "hit_mine":  True,
                "tile":      tile,
                "all_mines": all_mines,
                "revealed":  sess['revealed'],
            }

        # Safe tile
        mult = mines_multiplier(MINES_GRID, sess['mines'], len(sess['revealed']))
        potential_win = round(sess['bet'] * mult, 2)

        return {
            "success":     True,
            "hit_mine":    False,
            "tile":        tile,
            "multiplier":  mult,
            "potential_win": potential_win,
            "revealed":    sess['revealed'],
        }

@router.post("/mines/cashout")
async def mines_cashout(request: Request):
    user_id = await require_auth(request)
    async with _get_mine_lock(user_id):
        sess = _mine_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Mines game")
        if not sess['revealed']:
            raise HTTPException(400, "Reveal at least one tile before cashing out")

        bet    = sess['bet']
        mines  = sess['mines']
        n_safe = len(sess['revealed'])
        mult   = mines_multiplier(MINES_GRID, mines, n_safe)
        win    = round(bet * mult, 2)
        revealed_tiles = list(sess['revealed'])
        mine_positions = list(sess['positions'])
        sess['active'] = False

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                win = await credit_win(user_id, win, conn)
                await log_game(conn, user_id, 'mines', bet, win, {
                    'mines': mines, 'revealed': n_safe, 'multiplier': mult,
                })
        _mine_sessions.pop(user_id, None)

    return {
        "success":     True,
        "win":         win,
        "multiplier":  mult,
        "revealed":    revealed_tiles,
        "mines":       mine_positions,
    }

@router.get("/mines/state")
async def mines_state(request: Request):
    user_id = await require_auth(request)
    async with _mine_locks.setdefault(user_id, asyncio.Lock()):
        sess = _mine_sessions.get(user_id)
    if not sess or not sess['active']:
        return {"active": False}
    mult = mines_multiplier(MINES_GRID, sess['mines'], len(sess['revealed']))
    return {
        "active":        True,
        "mine_count":    sess['mines'],
        "revealed":      sess['revealed'],
        "multiplier":    mult,
        "potential_win": round(sess['bet'] * mult, 2),
        "bet":           sess['bet'],
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  PLINKO  —  Ball drop through peg board
# ══════════════════════════════════════════════════════════
# ============================================================
#
# 8-row peg board. Ball starts at top-center.
# At each row, ball goes left or right (50/50).
# Final bucket position (0-8) determines multiplier.
# Bucket 0 and 8 = highest risk/reward extremes.

PLINKO_ROWS = 8

# Bucket multipliers (9 buckets, 0-8)
# Outer buckets pay most; center pays least (most likely)
PLINKO_PAYOUTS_LOW  = [5.6, 2.1, 1.1, 0.5, 0.3, 0.5, 1.1, 2.1, 5.6]   # low risk
PLINKO_PAYOUTS_MED  = [13,  3,   1.4, 0.7, 0.4, 0.7, 1.4, 3,   13]     # medium
PLINKO_PAYOUTS_HIGH = [29,  4,   1.5, 0.3, 0.2, 0.3, 1.5, 4,   29]     # high risk

RISK_TABLES = {
    'low':    PLINKO_PAYOUTS_LOW,
    'medium': PLINKO_PAYOUTS_MED,
    'high':   PLINKO_PAYOUTS_HIGH,
}

def simulate_plinko(rows: int = PLINKO_ROWS) -> tuple[int, list]:
    """Simulate ball path. Returns (bucket, path_as_L/R_list)."""
    pos  = 0
    path = []
    for _ in range(rows):
        go_right = secure_random() < 0.5
        path.append('R' if go_right else 'L')
        if go_right:
            pos += 1
    return pos, path

class PlinkoRequest(BaseModel):
    amount: float
    risk:   str = 'medium'   # 'low' | 'medium' | 'high'

@router.post("/plinko/drop")
async def plinko_drop(req: PlinkoRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("plinko")
    bet     = clamp_bet(req.amount)
    risk    = req.risk.lower() if req.risk.lower() in RISK_TABLES else 'medium'

    bucket, path = simulate_plinko(PLINKO_ROWS)
    table        = RISK_TABLES[risk]
    raw_mult     = table[bucket]
    win          = apply_house(bet * raw_mult) if raw_mult > 0 else 0

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, bet, conn):
                raise HTTPException(400, "Insufficient balance")
            if win:
                win = await credit_win(user_id, win, conn)
            await log_game(conn, user_id, 'plinko', bet, win, {
                'risk': risk, 'bucket': bucket, 'mult': raw_mult,
            })

    return {
        "success": True,
        "bucket":  bucket,
        "path":    path,
        "mult":    raw_mult,
        "win":     win,
        "bet":     bet,
        "risk":    risk,
        "payouts": table,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  TOWER  —  Climb floors, pick the safe box
# ══════════════════════════════════════════════════════════
# ============================================================
#
# Each floor has N boxes, one of which is a bomb.
# Player picks a box; if safe, floor cleared + multiplier grows.
# If bomb, game over.
# Difficulty options: easy (3 boxes, 1 bomb),
#                     medium (3 boxes, 1 bomb, harder reward),
#                     hard (2 boxes, 1 bomb — 50/50 each floor),
#                     expert (2 boxes, 1 bomb, taller tower)

TOWER_CONFIG = {
    'easy':   {'floors': 8,  'boxes': 3, 'bombs': 1},
    'medium': {'floors': 10, 'boxes': 3, 'bombs': 1},
    'hard':   {'floors': 12, 'boxes': 2, 'bombs': 1},
    'expert': {'floors': 16, 'boxes': 2, 'bombs': 1},
}

def tower_multiplier(floor: int, boxes: int, bombs: int) -> float:
    """Multiplier for clearing `floor` floors."""
    if floor == 0:
        return 1.0
    safe   = boxes - bombs
    p_safe = safe / boxes
    mult   = (1 / p_safe) ** floor
    return round(apply_house(mult), 3)

_tower_sessions: Dict[int, Dict] = {}
_tower_locks:    Dict[int, asyncio.Lock] = {}   # Fix 5: per-user locks

def _get_tower_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _tower_locks:
        _tower_locks[user_id] = asyncio.Lock()
    return _tower_locks[user_id]

class TowerStartRequest(BaseModel):
    amount:     float
    difficulty: str = 'medium'

class TowerPickRequest(BaseModel):
    box: int   # 0-indexed

@router.post("/tower/start")
async def tower_start(req: TowerStartRequest, request: Request):
    user_id    = await require_auth(request)
    await require_game_enabled("tower")
    bet        = clamp_bet(req.amount)
    difficulty = req.difficulty if req.difficulty in TOWER_CONFIG else 'medium'
    cfg        = TOWER_CONFIG[difficulty]

    async with _get_tower_lock(user_id):
        existing = _tower_sessions.get(user_id, {})
        if existing.get('active'):
            if _is_session_expired(existing):
                _tower_sessions.pop(user_id, None)
            else:
                raise HTTPException(400, "You already have an active Tower game — cashout or finish first")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")

        # Pre-generate bomb position for every floor
        floors_layout = []
        for _ in range(cfg['floors']):
            bomb_positions = secure_shuffle(list(range(cfg["boxes"])))[:cfg["bombs"]]
            floors_layout.append(bomb_positions)

        _tower_sessions[user_id] = {
            'bet':        bet,
            'difficulty': difficulty,
            'cfg':        cfg,
            'layout':     floors_layout,
            'floor':      0,
            'active':     True,
            'created_at': datetime.now(timezone.utc),
        }

    return {
        "success":    True,
        "difficulty": difficulty,
        "floors":     cfg['floors'],
        "boxes":      cfg['boxes'],
        "bombs":      cfg['bombs'],
        "floor":      0,
        "multiplier": 1.0,
    }

@router.post("/tower/pick")
async def tower_pick(req: TowerPickRequest, request: Request):
    user_id = await require_auth(request)
    async with _get_tower_lock(user_id):
        sess    = _tower_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Tower game — start one first")

        cfg   = sess['cfg']
        box   = req.box
        if box < 0 or box >= cfg['boxes']:
            raise HTTPException(400, f"Box must be 0–{cfg['boxes'] - 1}")

        floor_idx    = sess['floor']
        bomb_slots   = sess['layout'][floor_idx]
        hit_bomb     = box in bomb_slots

        if hit_bomb:
            sess['active'] = False
            pool = await get_db()
            async with pool.acquire() as conn:
                await log_game(conn, user_id, 'tower', sess['bet'], 0, {
                    'difficulty': sess['difficulty'],
                    'floors_cleared': floor_idx,
                    'bust_floor': floor_idx,
                })
            _tower_sessions.pop(user_id, None)
            return {
                "success":      True,
                "hit_bomb":     True,
                "box":          box,
                "bomb_slots":   bomb_slots,
                "floor":        floor_idx,
                "floors_cleared": floor_idx,
            }

        # Safe — advance floor
        sess['floor'] += 1
        new_floor = sess['floor']
        mult      = tower_multiplier(new_floor, cfg['boxes'], cfg['bombs'])
        pot_win   = round(sess['bet'] * mult, 2)
        at_top    = (new_floor >= cfg['floors'])

        if at_top:
            # Auto-cashout at top
            sess['active'] = False
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    pot_win = await credit_win(user_id, pot_win, conn)
                    await log_game(conn, user_id, 'tower', sess['bet'], pot_win, {
                        'difficulty': sess['difficulty'],
                        'floors_cleared': new_floor,
                        'max_floor': True,
                    })
            _tower_sessions.pop(user_id, None)
            return {
                "success":        True,
                "hit_bomb":       False,
                "box":            box,
                "bomb_slots":     bomb_slots,
                "floor":          new_floor,
                "multiplier":     mult,
                "potential_win":  pot_win,
                "at_top":         True,
                "auto_win":       pot_win,
            }

        return {
            "success":       True,
            "hit_bomb":      False,
            "box":           box,
            "bomb_slots":    bomb_slots,
            "floor":         new_floor,
            "multiplier":    mult,
            "potential_win": pot_win,
            "at_top":        False,
        }

@router.post("/tower/cashout")
async def tower_cashout(request: Request):
    user_id = await require_auth(request)
    async with _get_tower_lock(user_id):
        sess    = _tower_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Tower game")
        if sess['floor'] == 0:
            raise HTTPException(400, "Clear at least one floor before cashing out")

        cfg    = sess['cfg']
        mult   = tower_multiplier(sess['floor'], cfg['boxes'], cfg['bombs'])
        win    = round(sess['bet'] * mult, 2)
        bet    = sess['bet']
        diff   = sess['difficulty']
        floors = sess['floor']
        sess['active'] = False

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                win = await credit_win(user_id, win, conn)
                await log_game(conn, user_id, 'tower', bet, win, {
                    'difficulty': diff,
                    'floors_cleared': floors,
                    'multiplier': mult,
                })
        _tower_sessions.pop(user_id, None)

    return {"success": True, "win": win, "multiplier": mult, "floors": floors}

# ============================================================
# ══════════════════════════════════════════════════════════
#  SHOTGUN  —  CS2 themed chamber gamble
# ══════════════════════════════════════════════════════════
# ============================================================
#
# A shotgun has N chambers (default 6), one is loaded.
# Player pulls the trigger one chamber at a time.
# Each successful pull increases the multiplier.
# If the loaded chamber fires — bust.
# Player can stop and cashout at any time.

SHOTGUN_CHAMBERS_DEFAULT = 6
SHOTGUN_LOADED           = 1

def shotgun_multiplier(chambers: int, loaded: int, pulled: int) -> float:
    """Multiplier after `pulled` safe triggers."""
    if pulled == 0:
        return 1.0
    mult = 1.0
    for i in range(pulled):
        remaining   = chambers - i
        danger      = loaded
        p_safe      = (remaining - danger) / remaining
        if p_safe <= 0:
            break
        mult *= (1 / p_safe)
    return round(apply_house(mult), 3)

_shotgun_sessions: Dict[int, Dict] = {}
_shotgun_locks:    Dict[int, asyncio.Lock] = {}   # Fix 5: per-user locks

def _get_shotgun_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _shotgun_locks:
        _shotgun_locks[user_id] = asyncio.Lock()
    return _shotgun_locks[user_id]

class ShotgunStartRequest(BaseModel):
    amount:   float
    chambers: int = 6   # 3-12

@router.post("/shotgun/start")
async def shotgun_start(req: ShotgunStartRequest, request: Request):
    user_id  = await require_auth(request)
    await require_game_enabled("shotgun")
    bet      = clamp_bet(req.amount)
    chambers = max(3, min(12, req.chambers))

    async with _get_shotgun_lock(user_id):
        existing = _shotgun_sessions.get(user_id, {})
        if existing.get('active'):
            if _is_session_expired(existing):
                _shotgun_sessions.pop(user_id, None)
            else:
                raise HTTPException(400, "You already have an active Shotgun game — cashout or finish first")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")

        # Loaded chamber position (0-indexed), hidden from player
        loaded_pos = secure_randint(0, chambers - 1)
        _shotgun_sessions[user_id] = {
            'bet':        bet,
            'chambers':   chambers,
            'loaded_pos': loaded_pos,
            'pulled':     0,
            'active':     True,
            'created_at': datetime.now(timezone.utc),
        }

    return {
        "success":    True,
        "chambers":   chambers,
        "loaded":     SHOTGUN_LOADED,
        "pulled":     0,
        "multiplier": 1.0,
    }

@router.post("/shotgun/pull")
async def shotgun_pull(request: Request):
    user_id = await require_auth(request)
    async with _get_shotgun_lock(user_id):
        sess    = _shotgun_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Shotgun game — start one first")

        pull_num    = sess['pulled']
        is_loaded   = (pull_num == sess['loaded_pos'])

        if is_loaded:
            sess['active'] = False
            pool = await get_db()
            async with pool.acquire() as conn:
                await log_game(conn, user_id, 'shotgun', sess['bet'], 0, {
                    'chambers': sess['chambers'],
                    'survived': pull_num,
                    'fired_at': pull_num,
                })
            _shotgun_sessions.pop(user_id, None)
            return {
                "success":   True,
                "fired":     True,
                "chamber":   pull_num,
                "survived":  pull_num,
                "loaded_at": sess['loaded_pos'],
            }

        # Survived
        sess['pulled'] += 1
        mult      = shotgun_multiplier(sess['chambers'], SHOTGUN_LOADED, sess['pulled'])
        pot_win   = round(sess['bet'] * mult, 2)
        last_safe = (sess['pulled'] >= sess['chambers'] - SHOTGUN_LOADED)

        if last_safe:
            # All safe chambers survived — auto cashout
            sess['active'] = False
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    pot_win = await credit_win(user_id, pot_win, conn)
                    await log_game(conn, user_id, 'shotgun', sess['bet'], pot_win, {
                        'chambers': sess['chambers'],
                        'survived': sess['pulled'],
                        'cleared': True,
                    })
            _shotgun_sessions.pop(user_id, None)
            return {
                "success":       True,
                "fired":         False,
                "chamber":       pull_num,
                "survived":      sess['pulled'],
                "multiplier":    mult,
                "potential_win": pot_win,
                "cleared":       True,
                "auto_win":      pot_win,
            }

        return {
            "success":       True,
            "fired":         False,
            "chamber":       pull_num,
            "survived":      sess['pulled'],
            "multiplier":    mult,
            "potential_win": pot_win,
            "cleared":       False,
        }

@router.post("/shotgun/cashout")
async def shotgun_cashout(request: Request):
    user_id = await require_auth(request)
    async with _get_shotgun_lock(user_id):
        sess    = _shotgun_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Shotgun game")
        if sess['pulled'] == 0:
            raise HTTPException(400, "Pull at least once before cashing out")

        mult     = shotgun_multiplier(sess['chambers'], SHOTGUN_LOADED, sess['pulled'])
        win      = round(sess['bet'] * mult, 2)
        bet      = sess['bet']
        chambers = sess['chambers']
        survived = sess['pulled']
        sess['active'] = False

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                win = await credit_win(user_id, win, conn)
                await log_game(conn, user_id, 'shotgun', bet, win, {
                    'chambers': chambers,
                    'survived': survived,
                    'multiplier': mult,
                })
        _shotgun_sessions.pop(user_id, None)

    return {"success": True, "win": win, "multiplier": mult, "survived": survived}

# ============================================================
# ══════════════════════════════════════════════════════════
#  LADDER CLIMB  —  Risk a rung, climb for multiplier
# ══════════════════════════════════════════════════════════
# ============================================================
#
# Each rung has a risk/reward profile. Player chooses whether
# to attempt each rung. Safe = multiplier grows. Fail = bust.
# Risk per rung decreases as you climb (server-side random).
# Player can cashout before any rung attempt.

LADDER_RUNGS = [
    # (fail_chance, multiplier_if_safe)
    (0.10, 1.25),   # Rung 1  — 10% fail
    (0.12, 1.35),   # Rung 2
    (0.15, 1.50),   # Rung 3
    (0.18, 1.70),   # Rung 4
    (0.20, 1.95),   # Rung 5
    (0.23, 2.30),   # Rung 6
    (0.26, 2.80),   # Rung 7
    (0.28, 3.50),   # Rung 8
    (0.30, 4.50),   # Rung 9
    (0.33, 6.00),   # Rung 10
    (0.36, 8.50),   # Rung 11
    (0.38, 12.0),   # Rung 12  — 38% fail, 12× base
    (0.40, 18.0),   # Rung 13
    (0.42, 28.0),   # Rung 14
    (0.45, 50.0),   # Rung 15  — top rung, 45% fail, 50× base
]

_ladder_sessions: Dict[int, Dict] = {}
_ladder_locks:    Dict[int, asyncio.Lock] = {}   # Fix 5: per-user locks

def _get_ladder_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _ladder_locks:
        _ladder_locks[user_id] = asyncio.Lock()
    return _ladder_locks[user_id]

class LadderStartRequest(BaseModel):
    amount: float

@router.post("/ladder/start")
async def ladder_start(req: LadderStartRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("ladder-climb")
    bet     = clamp_bet(req.amount)

    async with _get_ladder_lock(user_id):
        existing = _ladder_sessions.get(user_id, {})
        if existing.get('active'):
            if _is_session_expired(existing):
                _ladder_sessions.pop(user_id, None)
            else:
                raise HTTPException(400, "You already have an active Ladder Climb game — cashout or finish first")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")

        _ladder_sessions[user_id] = {
            'bet':        bet,
            'rung':       0,
            'mult':       1.0,
            'active':     True,
            'created_at': datetime.now(timezone.utc),
        }

    return {
        "success":     True,
        "rung":        0,
        "multiplier":  1.0,
        "next_rung":   LADDER_RUNGS[0],
        "total_rungs": len(LADDER_RUNGS),
    }

@router.post("/ladder/climb")
async def ladder_climb(request: Request):
    user_id = await require_auth(request)
    async with _get_ladder_lock(user_id):
        sess    = _ladder_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Ladder Climb game — start one first")

        rung_idx = sess['rung']
        if rung_idx >= len(LADDER_RUNGS):
            raise HTTPException(400, "Already at the top")

        fail_chance, rung_mult = LADDER_RUNGS[rung_idx]
        failed = secure_random() < fail_chance

        if failed:
            sess['active'] = False
            pool = await get_db()
            async with pool.acquire() as conn:
                await log_game(conn, user_id, 'ladder', sess['bet'], 0, {
                    'rungs_climbed': rung_idx,
                    'bust_rung': rung_idx,
                })
            _ladder_sessions.pop(user_id, None)  # pop AFTER log
            return {
                "success":       True,
                "climbed":       False,
                "rung":          rung_idx,
                "rungs_climbed": rung_idx,
            }

        # Climbed safely — accumulate raw multiplier; apply house edge only at cashout
        sess['mult']  = round(sess['mult'] * rung_mult, 4)
        sess['rung'] += 1
        new_rung      = sess['rung']
        pot_win       = round(sess['bet'] * apply_house(sess['mult']), 2)
        at_top        = (new_rung >= len(LADDER_RUNGS))

        if at_top:
            # Auto-cashout at summit
            sess['active'] = False
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    pot_win = await credit_win(user_id, pot_win, conn)
                    await log_game(conn, user_id, 'ladder', sess['bet'], pot_win, {
                        'rungs_climbed': new_rung, 'summit': True,
                        'multiplier': sess['mult'],
                    })
            _ladder_sessions.pop(user_id, None)
            return {
                "success":       True,
                "climbed":       True,
                "rung":          new_rung,
                "multiplier":    sess['mult'],
                "potential_win": pot_win,
                "at_top":        True,
                "auto_win":      pot_win,
            }

        next_rung = LADDER_RUNGS[new_rung] if new_rung < len(LADDER_RUNGS) else None
        return {
            "success":       True,
            "climbed":       True,
            "rung":          new_rung,
            "multiplier":    sess['mult'],
            "potential_win": pot_win,
            "at_top":        False,
            "next_rung":     next_rung,
        }

@router.post("/ladder/cashout")
async def ladder_cashout(request: Request):
    user_id = await require_auth(request)
    async with _get_ladder_lock(user_id):
        sess    = _ladder_sessions.get(user_id)
        if not sess or not sess['active']:
            raise HTTPException(400, "No active Ladder Climb game")
        if sess['rung'] == 0:
            raise HTTPException(400, "Climb at least one rung before cashing out")

        win  = round(sess['bet'] * apply_house(sess['mult']), 2)
        bet  = sess['bet']
        mult = sess['mult']
        rung = sess['rung']
        sess['active'] = False

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                win = await credit_win(user_id, win, conn)
                await log_game(conn, user_id, 'ladder', bet, win, {
                    'rungs_climbed': rung,
                    'multiplier':    mult,
                })
        _ladder_sessions.pop(user_id, None)

    return {
        "success":    True,
        "win":        win,
        "multiplier": mult,
        "rungs":      rung,
    }

# ============================================================
# ══════════════════════════════════════════════════════════
#  ROULETTE  —  Full wheel with multiple bet types
# ══════════════════════════════════════════════════════════
# ============================================================
#
# Standard 37-number European roulette (0-36).
# Supports: straight (35:1), red/black (1:1), odd/even (1:1),
#           high/low (1:1), dozen (2:1), column (2:1),
#           split (17:1), street (11:1), corner (8:1).
# Multiple simultaneous bets per spin supported.

ROULETTE_RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
ROULETTE_NUMBERS = list(range(37))  # 0-36

def roulette_spin() -> int:
    return secure_choice(ROULETTE_NUMBERS)

def _parse_multi_number_bet(bet_value: Any, expected_count: int) -> list:
    """Parse 'n1,n2,...' and validate count and range. (Fix 27)"""
    try:
        nums = [int(x.strip()) for x in str(bet_value).split(',')]
    except (ValueError, AttributeError):
        raise HTTPException(400, f"bet_value must be comma-separated integers, got: {bet_value!r}")
    if len(nums) != expected_count:
        raise HTTPException(400, f"Expected {expected_count} numbers, got {len(nums)}")
    for n in nums:
        if not (0 <= n <= 36):
            raise HTTPException(400, f"Roulette number {n} is out of range (0–36)")
    return nums

def evaluate_roulette_bet(bet_type: str, bet_value: Any, result: int) -> float:
    """
    Returns multiplier (including stake return) or 0 on loss.
    All payouts assume European single-zero roulette.
    """
    r = result
    # Straight-up single number
    if bet_type == 'straight':
        try:
            n = int(bet_value)
        except (ValueError, TypeError):
            raise HTTPException(400, f"straight bet_value must be an integer 0–36, got {bet_value!r}")
        if not 0 <= n <= 36:
            raise HTTPException(400, f"Straight number {n} out of range (0–36)")
        return 36.0 if r == n else 0

    # Colours
    if bet_type == 'red':
        return 2.0 if r in ROULETTE_RED and r != 0 else 0
    if bet_type == 'black':
        return 2.0 if r not in ROULETTE_RED and r != 0 else 0

    # Odd / Even
    if bet_type == 'odd':
        return 2.0 if r != 0 and r % 2 == 1 else 0
    if bet_type == 'even':
        return 2.0 if r != 0 and r % 2 == 0 else 0

    # 1-18 / 19-36
    if bet_type == 'low':
        return 2.0 if 1 <= r <= 18 else 0
    if bet_type == 'high':
        return 2.0 if 19 <= r <= 36 else 0

    # Dozens: 1st (1-12), 2nd (13-24), 3rd (25-36)
    if bet_type == 'dozen':
        try:
            d = int(bet_value)
        except (ValueError, TypeError):
            raise HTTPException(400, f"dozen bet_value must be 1, 2, or 3, got {bet_value!r}")
        if d not in (1, 2, 3):
            raise HTTPException(400, "dozen must be 1, 2, or 3")
        if d == 1 and 1 <= r <= 12:   return 3.0
        if d == 2 and 13 <= r <= 24:  return 3.0
        if d == 3 and 25 <= r <= 36:  return 3.0
        return 0

    # Columns: col 1 = 1,4,7,...34; col 2 = 2,5,8,...35; col 3 = 3,6,9,...36
    if bet_type == 'column':
        try:
            col = int(bet_value)
        except (ValueError, TypeError):
            raise HTTPException(400, f"column bet_value must be 1, 2, or 3, got {bet_value!r}")
        if col not in (1, 2, 3):
            raise HTTPException(400, "column must be 1, 2, or 3")
        if r != 0 and r % 3 == col % 3:
            return 3.0
        return 0

    # Split (2 adjacent numbers) — Fix 27: validated parsing
    if bet_type == 'split':
        nums = _parse_multi_number_bet(bet_value, 2)
        return 18.0 if r in nums else 0

    # Street (3 numbers in a row) — Fix 27: validated parsing
    if bet_type == 'street':
        nums = _parse_multi_number_bet(bet_value, 3)
        return 12.0 if r in nums else 0

    # Corner (4 numbers) — Fix 27: validated parsing
    if bet_type == 'corner':
        nums = _parse_multi_number_bet(bet_value, 4)
        return 9.0 if r in nums else 0

    # Bug 164 fix: reject unknown bet types so the player gets an error rather
    # than silently losing their stake on a bet the house doesn't recognise.
    raise HTTPException(400, f"Unknown roulette bet type: {bet_type!r}")

class RouletteBet(BaseModel):
    type:   str     # bet type
    value:  Any     # bet-type-specific value
    amount: float   # bet amount

class RouletteRequest(BaseModel):
    bets: List[RouletteBet]   # support multiple simultaneous bets

@router.post("/roulette/spin")
async def roulette_spin_endpoint(req: RouletteRequest, request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("roulette")

    if not req.bets:
        raise HTTPException(400, "Place at least one bet")
    if len(req.bets) > 10:
        raise HTTPException(400, "Maximum 10 simultaneous bets")

    total_bet = sum(clamp_bet(b.amount) for b in req.bets)

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, total_bet, conn):
                raise HTTPException(400, "Insufficient balance")

            result = roulette_spin()
            total_win = 0.0
            bet_results = []

            for b in req.bets:
                amt   = clamp_bet(b.amount)
                mult  = evaluate_roulette_bet(b.type, b.value, result)
                win   = apply_house(amt * mult) if mult else 0
                total_win += win
                bet_results.append({
                    'type':   b.type,
                    'value':  b.value,
                    'amount': amt,
                    'mult':   mult,
                    'win':    win,
                    'won':    mult > 0,
                })

            if total_win:
                total_win = await credit_win(user_id, total_win, conn)

            await log_game(conn, user_id, 'roulette', total_bet, total_win, {
                'result': result,
                'bets':   [{'type': b.type, 'value': str(b.value), 'amount': clamp_bet(b.amount)}
                           for b in req.bets],
            })

    is_red   = result in ROULETTE_RED and result != 0
    is_black = result not in ROULETTE_RED and result != 0
    is_zero  = result == 0

    return {
        "success":     True,
        "result":      result,
        "color":       'red' if is_red else ('black' if is_black else 'green'),
        "is_odd":      result % 2 == 1 if result != 0 else False,
        "total_bet":   total_bet,
        "total_win":   total_win,
        "profit":      round(total_win - total_bet, 2),
        "bet_results": bet_results,
    }


