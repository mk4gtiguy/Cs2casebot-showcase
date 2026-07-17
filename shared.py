# ============================================================
# SHARED.PY — Single Source of Truth
# CS2CaseBot | All constants, DB, sessions, helpers, bot data
# ============================================================

import secrets
import time
import logging
import re
import os
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Set, Any, Tuple
from fastapi import HTTPException, Request
import asyncpg

# ── Per-user sliding-window rate limiter ─────────────────────
from collections import defaultdict, deque

class _SlidingWindowLimiter:
    """In-memory per-key sliding window rate limiter (asyncio-safe, no locks needed)."""
    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._windows: dict = defaultdict(deque)

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.period
        dq = self._windows[key]
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= self.max_calls:
            return False
        dq.append(now)
        return True

# Tiers: adjust numbers here to tune without touching endpoint code
RATE_CASE    = _SlidingWindowLimiter(max_calls=30, period=60.0)  # case/sticker opens
RATE_MARKET  = _SlidingWindowLimiter(max_calls=20, period=60.0)  # market list/buy
RATE_WRITE   = _SlidingWindowLimiter(max_calls=30, period=60.0)  # sell/trade/upgrade
RATE_AUTH    = _SlidingWindowLimiter(max_calls=10, period=60.0)  # auth endpoints
RATE_PAYMENT = _SlidingWindowLimiter(max_calls=5,  period=60.0)  # Stripe checkout session creation

async def check_rate_limit(request: "Request", limiter: _SlidingWindowLimiter) -> None:
    """Raise 429 if the authenticated user (or IP) exceeds the limiter's quota."""
    uid = await get_user_id_from_session(request)
    key = str(uid) if uid else (request.client.host if request.client else "anon")
    if not limiter.is_allowed(key):
        raise HTTPException(status_code=429, detail="Too many requests — please slow down")

# ── Secure RNG helpers (Fix 1) ───────────────────────────────

def secure_randint(a: int, b: int) -> int:
    """Return a random int in [a, b] inclusive, using OS entropy."""
    return a + secrets.randbelow(b - a + 1)

def secure_choice(seq):
    """Choose a random element from a non-empty sequence, using OS entropy."""
    return seq[secrets.randbelow(len(seq))]

def secure_random() -> float:
    """Return a random float in [0.0, 1.0) using OS entropy."""
    return secrets.randbelow(2**32) / 2**32

def secure_shuffle(lst: list) -> list:
    """Return a shuffled copy of lst using OS entropy (Fisher-Yates)."""
    lst = list(lst)
    for i in range(len(lst) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        lst[i], lst[j] = lst[j], lst[i]
    return lst

# ─── Logger ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("cs2casebot")

# ============================================================
# DATABASE
# ============================================================

db_pool: Optional[asyncpg.Pool] = None

async def get_db() -> asyncpg.Pool:
    if db_pool is None:
        raise RuntimeError("Database pool not initialized")
    return db_pool

async def init_db(database_url: str) -> asyncpg.Pool:
    """Initialize and return the global DB pool."""
    global db_pool

    async def _init_conn(conn):
        for pg_type in ('json', 'jsonb'):
            await conn.set_type_codec(
                pg_type,
                encoder=json.dumps,
                decoder=json.loads,
                schema='pg_catalog',
            )

    db_pool = await asyncpg.create_pool(database_url, min_size=2, max_size=20, init=_init_conn)
    logger.info("✅ Database pool initialized")
    return db_pool

# ============================================================
# SESSIONS
# ============================================================

# ============================================================
# SESSIONS — Redis-backed (falls back to in-memory if no REDIS_URL)
# ============================================================

SESSION_TTL = 7 * 24 * 3600  # 7 days in seconds

# Try to connect to Redis; fall back to in-memory dict if unavailable
_redis_client = None
_sessions_fallback: Dict[str, Any] = {}  # in-memory fallback

def _get_redis():
    global _redis_client
    # If we have a cached client, verify it's still alive before returning it
    if _redis_client is not None:
        try:
            _redis_client.ping()
            return _redis_client
        except Exception:
            # Redis dropped — clear cached client so we retry below
            _redis_client = None
            logger.warning("⚠️ Redis connection lost — attempting reconnect")
    redis_url = os.getenv("REDIS_URL", "")
    if not redis_url:
        return None
    try:
        import redis as _redis_lib
        client = _redis_lib.Redis.from_url(redis_url, decode_responses=True, socket_timeout=2)
        client.ping()  # verify connection
        _redis_client = client
        logger.info("✅ Redis session store connected")
        return _redis_client
    except Exception as e:
        logger.warning(f"⚠️ Redis unavailable, using in-memory sessions: {e}")
        return None

# Public sessions dict kept for backward compat with code that reads sessions[token]
# When Redis is active this is a no-op shim; when falling back it IS the store.
sessions: Dict[str, Any] = _sessions_fallback

def _session_key(token: str) -> str:
    return f"session:{token}"

def create_session(token: str, data: dict) -> None:
    """Store a session. data must be JSON-serialisable."""
    r = _get_redis()
    if r:
        try:
            r.setex(_session_key(token), SESSION_TTL, json.dumps(data, default=str))
            return
        except Exception as e:
            logger.warning(f"Redis set failed: {e}")
    # fallback
    data["created_at"] = data.get("created_at", datetime.now(timezone.utc).isoformat())
    _sessions_fallback[token] = data

def get_session(token: str) -> Optional[dict]:
    """Retrieve a session dict or None if missing/expired."""
    r = _get_redis()
    if r:
        try:
            raw = r.get(_session_key(token))
            if raw:
                return json.loads(raw)
            return None
        except Exception as e:
            logger.warning(f"Redis get failed: {e}")
    return _sessions_fallback.get(token)

def delete_session(token: str) -> None:
    """Delete a session."""
    r = _get_redis()
    if r:
        try:
            r.delete(_session_key(token))
            return
        except Exception as e:
            logger.warning(f"Redis delete failed: {e}")
    _sessions_fallback.pop(token, None)

def clean_expired_sessions():
    """Only needed for the in-memory fallback; Redis handles TTL natively."""
    now = datetime.now(timezone.utc)
    expired = []
    for k, v in list(_sessions_fallback.items()):
        try:
            created = v.get("created_at")
            if isinstance(created, str):
                created = datetime.fromisoformat(created)
            if created and (now - created).total_seconds() > SESSION_TTL:
                expired.append(k)
        except Exception:
            pass
    for k in expired:
        _sessions_fallback.pop(k, None)

async def get_user_id_from_session(request: Request) -> Optional[int]:
    # Discord Activity proxy may strip Set-Cookie headers; clients send token as header instead
    candidates = [request.headers.get("X-Activity-Token")]
    candidates += [request.cookies.get(n) for n in ("session_token", "activity_session")]
    for token in candidates:
        if not token:
            continue
        data = get_session(token)
        if not data:
            continue
        uid = data.get("user_id")
        if uid is None:
            continue
        try:
            return int(uid)
        except (ValueError, TypeError):
            continue
    return None

async def require_auth(request: Request) -> int:
    user_id = await get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_banned, ban_expires FROM users WHERE user_id=$1", user_id
        )
    if row and row["is_banned"]:
        ban_expires = row["ban_expires"]
        if ban_expires is None:
            raise HTTPException(status_code=403, detail="Your account has been permanently banned.")
        now = datetime.now(timezone.utc)
        exp = ban_expires if ban_expires.tzinfo else ban_expires.replace(tzinfo=timezone.utc)
        if now < exp:
            raise HTTPException(status_code=403, detail=f"Your account is banned until {exp.isoformat()}.")
        # Ban expired — auto-lift it
        async with pool.acquire() as conn2:
            await conn2.execute(
                "UPDATE users SET is_banned=FALSE, ban_reason=NULL, ban_expires=NULL WHERE user_id=$1",
                user_id
            )
    return user_id

# ============================================================
# ADMIN / MODERATOR
# ============================================================

ADMIN_USER_IDS: Set[int] = set()
MODERATOR_USER_IDS: Set[int] = set()

async def _admin_check(request: Request) -> int:
    """Common: return user_id or raise 403 (identical response for no-session vs not-admin)."""
    user_id = await get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id

async def require_admin(request: Request) -> int:
    user_id = await _admin_check(request)
    if user_id not in ADMIN_USER_IDS:
        raise HTTPException(status_code=403, detail="Admin access required")
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_banned FROM users WHERE user_id=$1", user_id
        )
    if row and row["is_banned"]:
        raise HTTPException(status_code=403, detail="Your account has been banned.")
    return user_id

async def require_admin_or_moderator(request: Request) -> int:
    user_id = await _admin_check(request)
    if user_id not in ADMIN_USER_IDS and user_id not in MODERATOR_USER_IDS:
        raise HTTPException(status_code=403, detail="Admin or Moderator access required")
    return user_id

# ============================================================
# BOT ACCOUNTS  (battles + games)
# ============================================================

BOT_IDS = {
    'normal': -1,
    'hard':   -2,
    'expert': -3,
}

BOT_NAMES = {
    -1: '🤖 Bot [Normal]',
    -2: '😈 Bot [Hard]',
    -3: '👹 Bot [Expert]',
}

BOT_STATS = {
    -1: {
        'balance': 250_000, 'total_opens': 800,  'total_golds': 80,
        'total_games_played': 1_200, 'win_streak': 15,
        'coinflip_wins': 100, 'dice_wins': 90, 'mines_wins': 70, 'slots_wins': 60,
    },
    -2: {
        'balance': 500_000, 'total_opens': 1_500, 'total_golds': 200,
        'total_games_played': 2_500, 'win_streak': 30,
        'coinflip_wins': 200, 'dice_wins': 180, 'mines_wins': 150, 'slots_wins': 120,
    },
    -3: {
        'balance': 1_000_000, 'total_opens': 3_000, 'total_golds': 500,
        'total_games_played': 5_000, 'win_streak': 50,
        'coinflip_wins': 450, 'dice_wins': 400, 'mines_wins': 350, 'slots_wins': 300,
    },
}

# CS2 Agent characters for Live Race & other games
RACE_AGENTS = [
    {'id': 'sas',       'name': 'SAS Operator',      'emoji': '🟢', 'color': '#4caf50'},
    {'id': 'phoenix',   'name': 'Phoenix Operative',  'emoji': '🔴', 'color': '#f44336'},
    {'id': 'swat',      'name': 'SWAT Commander',     'emoji': '🔵', 'color': '#2196f3'},
    {'id': 'guerrilla', 'name': 'Guerrilla Warfare',  'emoji': '🟡', 'color': '#ffd700'},
    {'id': 'ksk',       'name': 'KSK Operator',       'emoji': '🟣', 'color': '#9c27b0'},
    {'id': 'seal',      'name': 'SEAL Frogman',       'emoji': '🩵', 'color': '#00bcd4'},
    {'id': 'ksm',       'name': 'Sabre CT',           'emoji': '🟠', 'color': '#ff9800'},
    {'id': 'ground',    'name': 'Ground Rebel',       'emoji': '⚪', 'color': '#9e9e9e'},
]

async def ensure_bot_users(pool: asyncpg.Pool):
    """Insert or refresh all bot users in the DB."""
    async with pool.acquire() as conn:
        for bot_id, name in BOT_NAMES.items():
            stats = BOT_STATS[bot_id]
            exists = await conn.fetchval(
                "SELECT 1 FROM users WHERE user_id = $1", bot_id
            )
            if not exists:
                await conn.execute("""
                    INSERT INTO users (
                        user_id, username, balance, total_opens, total_golds,
                        total_games_played, win_streak, coinflip_wins, dice_wins,
                        mines_wins, slots_wins, created_at, updated_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW(),NOW())
                """,
                    bot_id, name,
                    stats['balance'], stats['total_opens'], stats['total_golds'],
                    stats['total_games_played'], stats['win_streak'],
                    stats['coinflip_wins'], stats['dice_wins'],
                    stats['mines_wins'], stats['slots_wins'],
                )
                logger.info(f"✅ Created bot user: {name}")
            else:
                await conn.execute("""
                    UPDATE users SET
                        balance=$1, total_opens=$2, total_golds=$3,
                        total_games_played=$4, win_streak=$5,
                        coinflip_wins=$6, dice_wins=$7,
                        mines_wins=$8, slots_wins=$9, updated_at=NOW()
                    WHERE user_id=$10
                """,
                    stats['balance'], stats['total_opens'], stats['total_golds'],
                    stats['total_games_played'], stats['win_streak'],
                    stats['coinflip_wins'], stats['dice_wins'],
                    stats['mines_wins'], stats['slots_wins'], bot_id,
                )

# ============================================================
# RARITY & VALUE CONSTANTS
# ============================================================

RARITY_EMOJIS      = {"Blue": "🟦", "Purple": "🟪", "Pink": "💗", "Red": "🔴", "Gold": "⭐"}
RARITY_COLORS      = {"Blue": "#4488ff", "Purple": "#aa00ff", "Pink": "#ff69b4", "Red": "#ff4444", "Gold": "#ffd700"}
WEAPON_BASE_VALUES = {"Blue": 0.25, "Purple": 1.00, "Pink": 4.00, "Red": 20.00}
GOLD_VALUES        = {"Common": 150, "Rare": 300, "Epic": 600, "Legendary": 1000, "Mythic": 2500}
CONDITION_MULTIPLIERS = {
    "Factory New": 2.0, "Minimal Wear": 1.5,
    "Field-Tested": 1.0, "Well-Worn": 0.75, "Battle-Scarred": 0.5
}
DROP_RATES         = {"Gold": 2.6, "Red": 2.5, "Pink": 2.5, "Purple": 5.0, "Blue": 87.4}
GOLD_TIER_PROGRESSION  = ["Common", "Rare", "Epic", "Legendary", "Mythic"]
TRADE_UP_PROGRESSION   = {"Blue": "Purple", "Purple": "Pink", "Pink": "Red", "Red": "Gold"}
STICKER_TRADE_PROGRESSION = {
    "⭐": "✨", "✨": "💫", "💫": "🔥",
    "🔥": "👑 Common", "👑 Common": "👑 Rare",
    "👑 Rare": "👑 Epic", "👑 Epic": "👑 Legendary"
}

# ============================================================
# BOT-SPECIFIC CONSTANTS (kept in sync with web)
# ============================================================

CAPSULE_EMOJIS = {
    "recoil": "⭐", "dreams": "🌙⭐", "cs20": "🎂⭐",
    "championship": "🏆", "legends": "👑"
}

# Bot sticker capsules (simplified 5 capsules for Discord commands)
BOT_STICKER_CAPSULES = {
    "recoil": {
        "name": "Recoil Sticker Capsule", "emoji": CAPSULE_EMOJIS["recoil"], "price": 0.50,
        "stickers": [
            {"name": "CS2 Logo", "rarity": "⭐"}, {"name": "AWP Sniper", "rarity": "✨"},
            {"name": "Headshot", "rarity": "💫"}, {"name": "Clutch King", "rarity": "🔥"}
        ]
    },
    "dreams": {
        "name": "Dreams Sticker Capsule", "emoji": CAPSULE_EMOJIS["dreams"], "price": 1.00,
        "stickers": [
            {"name": "Phoenix Rising", "rarity": "⭐"}, {"name": "Dragon Lore", "rarity": "✨"},
            {"name": "Royal Crown", "rarity": "👑 Common"}, {"name": "Knight's Oath", "rarity": "👑 Rare"}
        ]
    },
    "cs20": {
        "name": "CS20 Sticker Capsule", "emoji": CAPSULE_EMOJIS["cs20"], "price": 1.00,
        "stickers": [
            {"name": "Counter-Terrorist Elite", "rarity": "⭐"}, {"name": "Terrorist Elite", "rarity": "✨"},
            {"name": "20 Years", "rarity": "💫"}, {"name": "Legends", "rarity": "👑 Epic"}
        ]
    },
    "championship": {
        "name": "Championship Sticker Capsule", "emoji": CAPSULE_EMOJIS["championship"], "price": 2.00,
        "stickers": [
            {"name": "Victory", "rarity": "✨"}, {"name": "Champion", "rarity": "💫"},
            {"name": "Golden Trophy", "rarity": "👑 Epic"}, {"name": "Hall of Fame", "rarity": "👑 Legendary"}
        ]
    },
    "legends": {
        "name": "Legends Sticker Capsule", "emoji": CAPSULE_EMOJIS["legends"], "price": 3.00,
        "stickers": [
            {"name": "s1mple", "rarity": "🔥"}, {"name": "ZyWoo", "rarity": "🔥"},
            {"name": "NiKo", "rarity": "👑 Rare"}, {"name": "KennyS", "rarity": "👑 Epic"}
        ]
    }
}

STICKER_VALUES = {
    "⭐": 0.10, "✨": 0.50, "💫": 2.00, "🔥": 10.00,
    "👑 Common": 30, "👑 Rare": 75, "👑 Epic": 150, "👑 Legendary": 300
}

# ============================================================
# FEATURED CASES
# ============================================================

FEATURED_CASES = [
    "kilowatt_case", "gallery_case", "fever_case",
    "cs20_case", "spectrum_2_case",
    "operation_riptide_case", "dreams_and_nightmares_case",
]

# ============================================================
# CASES DATA — 37 Real CS2 Cases
# ============================================================
CASES = {
    "cs:go_weapon_case": {
        "name": "CS:GO Weapon Case",
        "emoji": "📦",
        "price": 2.0,
        "collection": 'The Arms Deal Collection'
    },
    "esports_2013_case": {
        "name": "eSports 2013 Case",
        "emoji": "🎯",
        "price": 2.0,
        "collection": 'The eSports 2013 Collection'
    },
    "operation_phoenix_weapon_case": {
        "name": "Operation Phoenix Weapon Case",
        "emoji": "⚡",
        "price": 2.5,
        "collection": 'The Phoenix Collection'
    },
    "huntsman_weapon_case": {
        "name": "Huntsman Weapon Case",
        "emoji": "🔥",
        "price": 2.5,
        "collection": 'The Huntsman Collection'
    },
    "operation_breakout_weapon_case": {
        "name": "Operation Breakout Weapon Case",
        "emoji": "💎",
        "price": 2.5,
        "collection": 'The Breakout Collection'
    },
    "esports_2014_summer_case": {
        "name": "eSports 2014 Summer Case",
        "emoji": "🌟",
        "price": 2.0,
        "collection": 'The eSports 2014 Summer Collection'
    },
    "operation_vanguard_weapon_case": {
        "name": "Operation Vanguard Weapon Case",
        "emoji": "🎨",
        "price": 2.5,
        "collection": 'The Vanguard Collection'
    },
    "chroma_case": {
        "name": "Chroma Case",
        "emoji": "🌈",
        "price": 2.0,
        "collection": 'The Chroma Collection'
    },
    "chroma_2_case": {
        "name": "Chroma 2 Case",
        "emoji": "💥",
        "price": 2.0,
        "collection": 'The Chroma 2 Collection'
    },
    "falchion_case": {
        "name": "Falchion Case",
        "emoji": "🌅",
        "price": 2.0,
        "collection": 'The Falchion Collection'
    },
    "shadow_case": {
        "name": "Shadow Case",
        "emoji": "⚠️",
        "price": 2.0,
        "collection": 'The Shadow Collection'
    },
    "revolver_case": {
        "name": "Revolver Case",
        "emoji": "🤲",
        "price": 2.0,
        "collection": 'The Revolver Case Collection'
    },
    "operation_wildfire_case": {
        "name": "Operation Wildfire Case",
        "emoji": "🎪",
        "price": 3.0,
        "collection": 'The Wildfire Collection'
    },
    "chroma_3_case": {
        "name": "Chroma 3 Case",
        "emoji": "🏹",
        "price": 2.0,
        "collection": 'The Chroma 3 Collection'
    },
    "gamma_case": {
        "name": "Gamma Case",
        "emoji": "🗡️",
        "price": 2.5,
        "collection": 'The 2021 Train Collection'
    },
    "gamma_2_case": {
        "name": "Gamma 2 Case",
        "emoji": "🛡️",
        "price": 2.5,
        "collection": 'The 2021 Train Collection'
    },
    "glove_case": {
        "name": "Glove Case",
        "emoji": "👑",
        "price": 4.0,
        "collection": 'The Glove Collection'
    },
    "spectrum_case": {
        "name": "Spectrum Case",
        "emoji": "🎰",
        "price": 2.5,
        "collection": 'The Spectrum Collection'
    },
    "operation_hydra_case": {
        "name": "Operation Hydra Case",
        "emoji": "🎲",
        "price": 2.0,
        "collection": 'The Horizon Collection'
    },
    "spectrum_2_case": {
        "name": "Spectrum 2 Case",
        "emoji": "🎳",
        "price": 2.5,
        "collection": 'The Prisma 2 Collection'
    },
    "clutch_case": {
        "name": "Clutch Case",
        "emoji": "🎭",
        "price": 2.0,
        "collection": 'The Horizon Collection'
    },
    "horizon_case": {
        "name": "Horizon Case",
        "emoji": "🎪",
        "price": 2.0,
        "collection": 'The Horizon Collection'
    },
    "danger_zone_case": {
        "name": "Danger Zone Case",
        "emoji": "🎯",
        "price": 2.0,
        "collection": 'The 2021 Dust 2 Collection'
    },
    "prisma_case": {
        "name": "Prisma Case",
        "emoji": "🎱",
        "price": 2.5,
        "collection": 'The Prisma Collection'
    },
    "shattered_web_case": {
        "name": "Shattered Web Case",
        "emoji": "🔫",
        "price": 4.0,
        "collection": 'The Shattered Web Collection'
    },
    "cs20_case": {
        "name": "CS20 Case",
        "emoji": "🌙",
        "price": 2.5,
        "collection": 'The CS20 Collection'
    },
    "prisma_2_case": {
        "name": "Prisma 2 Case",
        "emoji": "🎂",
        "price": 2.0,
        "collection": 'The Prisma 2 Collection'
    },
    "fracture_case": {
        "name": "Fracture Case",
        "emoji": "💎",
        "price": 2.0,
        "collection": 'The Fracture Collection'
    },
    "operation_broken_fang_case": {
        "name": "Operation Broken Fang Case",
        "emoji": "⚡",
        "price": 3.5,
        "collection": 'The Operation Broken Fang Collection'
    },
    "snakebite_case": {
        "name": "Snakebite Case",
        "emoji": "🌊",
        "price": 2.5,
        "collection": 'The Snakebite Collection'
    },
    "operation_riptide_case": {
        "name": "Operation Riptide Case",
        "emoji": "🌪️",
        "price": 3.0,
        "collection": 'The 2021 Train Collection'
    },
    "dreams_and_nightmares_case": {
        "name": "Dreams & Nightmares Case",
        "emoji": "🎇",
        "price": 2.5,
        "collection": 'The 2021 Train Collection'
    },
    "recoil_case": {
        "name": "Recoil Case",
        "emoji": "📦",
        "price": 2.0,
        "collection": 'The Recoil Collection'
    },
    "revolution_case": {
        "name": "Revolution Case",
        "emoji": "🎯",
        "price": 2.5,
        "collection": 'The Revolution Collection'
    },
    "kilowatt_case": {
        "name": "Kilowatt Case",
        "emoji": "⚡",
        "price": 3.5,
        "collection": 'The Kilowatt Collection'
    },
    "gallery_case": {
        "name": "Gallery Case",
        "emoji": "🔥",
        "price": 3.0,
        "collection": 'The Gallery Collection'
    },
    "fever_case": {
        "name": "Fever Case",
        "emoji": "💎",
        "price": 4.0,
        "collection": 'The Fever Collection'
    },
}


# ============================================================
# CONTAINER IMAGE MAPPING (by case name)
# ============================================================
CONTAINERS_JSON_PATH = os.path.join(os.path.dirname(__file__), "containers.json")

def build_container_image_map() -> Dict[str, str]:
    """
    Build mapping from our case_id (e.g., "cs:go_weapon_case") 
    to the container image filename (e.g., "172.webp") by matching the case name.
    """
    if not os.path.exists(CONTAINERS_JSON_PATH):
        print(f"⚠️ {CONTAINERS_JSON_PATH} not found – container images will fallback")
        return {}

    with open(CONTAINERS_JSON_PATH, "r", encoding="utf-8") as f:
        containers = json.load(f)

    # Build dict: normalized name -> filename
    container_by_name = {}
    for entry in containers:
        name = entry.get("name")
        image = entry.get("containerImage")
        if name and image:
            normalized = " ".join(name.lower().split())   # normalize whitespace & case
            filename = os.path.basename(image)            # "172.webp"
            container_by_name[normalized] = filename

    # Map our case IDs by matching names
    mapping = {}
    for case_id, case_data in CASES.items():
        case_name = case_data.get("name")
        if not case_name:
            continue
        normalized_case = " ".join(case_name.lower().split())
        if normalized_case in container_by_name:
            mapping[case_id] = container_by_name[normalized_case]
        else:
            print(f"⚠️ No container image found for case: {case_name} (id: {case_id})")

    return mapping

# Build the global map
CONTAINER_IMAGE_MAP = build_container_image_map()

# ============================================================
# SKINS DATA LOADER
# ============================================================
SKINS_JSON_PATH = os.path.join(os.path.dirname(__file__), "skins.json")
SKINS_DATA = []

if os.path.exists(SKINS_JSON_PATH):
    with open(SKINS_JSON_PATH, 'r', encoding='utf-8') as f:
        SKINS_DATA = json.load(f)
    logger.info(f"✅ Loaded {len(SKINS_DATA)} skins from skins.json")
else:
    logger.warning(f"⚠️ skins.json not found at {SKINS_JSON_PATH}")

# ============================================================
# ITEM ID → PROPER CS2 DISPLAY NAME
# Maps the skins.json `itemId` field to the real CS2 weapon name.
# This is the authoritative source — weaponType is a generic category
# (RIFLE, SHOTGUN, etc.) and must NOT be used for display.
# ============================================================
ITEM_ID_TO_DISPLAY_NAME: Dict[str, str] = {
    'AK_47':          'AK-47',
    'AUG':            'AUG',
    'AWP':            'AWP',
    'BAYONET':        '★ Bayonet',
    'BLOODHOUND':     '★ Bloodhound Gloves',
    'BOWIE':          '★ Bowie Knife',
    'BROKEN_FANG':    '★ Broken Fang Gloves',
    'BUTTERFLY':      '★ Butterfly Knife',
    'CLASSIC':        '★ Classic Knife',
    'CZ75_AUTO':      'CZ75-Auto',
    'DESERT_EAGLE':   'Desert Eagle',
    'DRIVER':         '★ Driver Gloves',
    'DUAL_BERETTAS':  'Dual Berettas',
    'FALCHION':       '★ Falchion Knife',
    'FAMAS':          'FAMAS',
    'FIVE_SEVEN':     'Five-SeveN',
    'FLIP':           '★ Flip Knife',
    'G3SG1':          'G3SG1',
    'GALIL_AR':       'Galil AR',
    'GLOCK_18':       'Glock-18',
    'GUT':            '★ Gut Knife',
    'HAND_WRAPS':     '★ Hand Wraps',
    'HUNTSMAN':       '★ Huntsman Knife',
    'HYDRA':          '★ Hydra Gloves',
    'KARAMBIT':       '★ Karambit',
    'KUKRI':          '★ Kukri Knife',
    'M249':           'M249',
    'M4A1_S':         'M4A1-S',
    'M4A4':           'M4A4',
    'M9_BAYONET':     '★ M9 Bayonet',
    'MAC_10':         'MAC-10',
    'MAG_7':          'MAG-7',
    'MOTO':           '★ Moto Gloves',
    'MP5_SD':         'MP5-SD',
    'MP7':            'MP7',
    'MP9':            'MP9',
    'NAVAJA':         '★ Navaja Knife',
    'NEGEV':          'Negev',
    'NOMAD':          '★ Nomad Knife',
    'NOVA':           'Nova',
    'P2000':          'P2000',
    'P250':           'P250',
    'P90':            'P90',
    'PARACORD':       '★ Paracord Knife',
    'PP_BIZON':       'PP-Bizon',
    'R8_REVOLVER':    'R8 Revolver',
    'SAWED_OFF':      'Sawed-Off',
    'SCAR_20':        'SCAR-20',
    'SG_553':         'SG 553',
    'SHADOW_DAGGERS': '★ Shadow Daggers',
    'SKELETON':       '★ Skeleton Knife',
    'SPECIALIST':     '★ Specialist Gloves',
    'SPORT':          '★ Sport Gloves',
    'SSG_08':         'SSG 08',
    'STILETTO':       '★ Stiletto Knife',
    'SURVIVAL':       '★ Survival Knife',
    'TALON':          '★ Talon Knife',
    'TEC_9':          'Tec-9',
    'UMP_45':         'UMP-45',
    'URSUS':          '★ Ursus Knife',
    'USP_S':          'USP-S',
    'XM1014':         'XM1014',
    'ZEUS_X27':       'Zeus x27',
}

def get_display_weapon_name(item_id: str, fallback: str = '') -> str:
    """Return the proper CS2 display name for a weapon itemId."""
    return ITEM_ID_TO_DISPLAY_NAME.get(item_id, fallback or item_id)

# Build SKIN_NAME_TO_IMAGE from SKINS_DATA.
# Index every skin under multiple key formats so lookups succeed regardless
# of how the name was stored (bare skin name, "WeaponType | Skin", "AK-47 | Skin", etc.)
SKIN_NAME_TO_IMAGE: Dict[str, str] = {}
for _skin in SKINS_DATA:
    _skin_name   = _skin.get("name")         # e.g. "Redline"
    _item_id     = _skin.get("itemId", "")   # e.g. "AK_47"
    _weapon_type = _skin.get("weaponType", "")  # e.g. "RIFLE" (generic, avoid for display)
    _display     = ITEM_ID_TO_DISPLAY_NAME.get(_item_id, _weapon_type)  # e.g. "AK-47"
    _skin_image  = _skin.get("skinImage")
    if not (_skin_name and _skin_image):
        continue
    _filename = os.path.basename(_skin_image)
    # Key formats — all the ways this skin's name may appear in the DB:
    for _key in [
        _skin_name,                                  # "Redline"
        f"{_display} | {_skin_name}",               # "AK-47 | Redline"  ← correct CS2 name
        f"{_weapon_type} | {_skin_name}",           # "RIFLE | Redline"  ← old bad name
        f"StatTrak™ {_skin_name}",                  # "StatTrak™ Redline"
        f"StatTrak™ {_display} | {_skin_name}",    # "StatTrak™ AK-47 | Redline"
        f"StatTrak™ {_weapon_type} | {_skin_name}", # "StatTrak™ RIFLE | Redline"
    ]:
        SKIN_NAME_TO_IMAGE.setdefault(_key, _filename)

logger.info(f"✅ Built SKIN_NAME_TO_IMAGE with {len(SKIN_NAME_TO_IMAGE)} entries")

# ============================================================
# DYNAMIC CASE DATA FROM SKINS.JSON
# ============================================================
SKIN_RARITY_MAP = {
    'CONSUMER': 'Blue',
    'INDUSTRIAL': 'Blue',
    'MIL_SPEC': 'Blue',
    'RESTRICTED': 'Purple',
    'CLASSIFIED': 'Pink',
    'COVERT': 'Red',
    'CONTRABAND': 'Gold',
    'EXTRAORDINARY': 'Gold'
}

COLLECTION_ITEMS = {}
if SKINS_DATA:
    for skin in SKINS_DATA:
        collection = skin.get('collection')
        if not collection:
            continue
        display_rarity = SKIN_RARITY_MAP.get(skin.get('rarity'), 'Blue')
        # Use the proper CS2 weapon display name, not the generic weaponType category
        proper_weapon = ITEM_ID_TO_DISPLAY_NAME.get(skin.get('itemId', ''), skin.get('weaponType', ''))
        entry = {
            'name': f"{proper_weapon} | {skin['name']}",
            'rarity': display_rarity,
            'float_min': skin.get('floatTop', 0.0),
            'float_max': skin.get('floatBottom', 1.0),
            'weapon_type': proper_weapon,   # now the real CS2 name, e.g. "AK-47"
            'skin_name': skin['name'],
            'item_id': skin['itemId'],
            'skin_image': skin.get('skinImage'),
        }
        COLLECTION_ITEMS.setdefault(collection, []).append(entry)
    logger.info(f"✅ Built COLLECTION_ITEMS with {sum(len(v) for v in COLLECTION_ITEMS.values())} skins across {len(COLLECTION_ITEMS)} collections")
else:
    logger.warning("❌ SKINS_DATA not loaded; dynamic case items will not work.")

# ─── Build global pool by rarity (for trade-ups / upgrades) ──
ALL_ITEMS_BY_RARITY = {}
for collection_items in COLLECTION_ITEMS.values():
    for item in collection_items:
        rarity = item['rarity']
        ALL_ITEMS_BY_RARITY.setdefault(rarity, []).append(item)
logger.info(f"✅ Built ALL_ITEMS_BY_RARITY: { {r: len(items) for r, items in ALL_ITEMS_BY_RARITY.items()} }")

def _make_gold_entry(_gskin: dict) -> dict:
    _gweapon = ITEM_ID_TO_DISPLAY_NAME.get(_gskin.get('itemId', ''), _gskin.get('weaponType', ''))
    return {
        'name':        f"{_gweapon} | {_gskin['name']}",
        'rarity':      'Gold',
        'float_min':   _gskin.get('floatTop',    0.06),
        'float_max':   _gskin.get('floatBottom', 0.80),
        'weapon_type': _gweapon,
        'skin_name':   _gskin['name'],
        'item_id':     _gskin.get('itemId'),
        'skin_image':  _gskin.get('skinImage'),
    }

# ─── Build Gold pool: gloves/knives have collection=None so they're excluded
#     from COLLECTION_ITEMS; build their pool directly from SKINS_DATA ──────
GOLD_ITEMS_POOL: list = []
for _gskin in SKINS_DATA:
    # Knives in skins.json are mistagged rarity=COVERT with collection=None
    # (should be gold-tier like gloves) — include by weaponType/itemKind too,
    # not just the CONTRABAND/EXTRAORDINARY rarity string, or they never drop.
    _is_knife = _gskin.get('weaponType') == 'KNIFE' or _gskin.get('itemKind') == 'KNIFE'
    if _gskin.get('rarity') not in ('CONTRABAND', 'EXTRAORDINARY') and not _is_knife:
        continue
    GOLD_ITEMS_POOL.append(_make_gold_entry(_gskin))
logger.info(f"✅ Built GOLD_ITEMS_POOL with {len(GOLD_ITEMS_POOL)} gloves/knives")

# ─── Per-case Gold pool: container_contents.json gives the real, authoritative
#     case → skin-id linkage (incl. which specific knife/glove finishes each
#     real CS2 case actually contains). Joined to CASES the same way
#     CONTAINER_IMAGE_MAP is (normalized name match against containers.json),
#     since that's the only key we have connecting our case_id to a container.
#     Without this, every case shared one global 651-item gold pool, so a
#     $0.50 case had the same odds at the priciest knife as a $50 case. ──────
CONTAINER_CONTENTS_JSON_PATH = os.path.join(os.path.dirname(__file__), "container_contents.json")

def _build_case_gold_items() -> Dict[str, list]:
    if not (os.path.exists(CONTAINER_CONTENTS_JSON_PATH) and os.path.exists(CONTAINERS_JSON_PATH)):
        logger.warning("⚠️ container_contents.json/containers.json missing — cases will share the global gold pool")
        return {}

    with open(CONTAINERS_JSON_PATH, "r", encoding="utf-8") as f:
        _containers = json.load(f)
    with open(CONTAINER_CONTENTS_JSON_PATH, "r", encoding="utf-8") as f:
        _contents = json.load(f)

    _skins_by_id = {s['id']: s for s in SKINS_DATA}
    _skin_ids_by_container_id = {c['containerId']: c.get('skinIds', []) for c in _contents}
    _container_id_by_name = {
        " ".join(c['name'].lower().split()): c['id']
        for c in _containers if c.get('type') == 'CASE' and c.get('name')
    }

    result: Dict[str, list] = {}
    for case_id, case_data in CASES.items():
        case_name = case_data.get('name')
        if not case_name:
            continue
        container_id = _container_id_by_name.get(" ".join(case_name.lower().split()))
        if not container_id:
            continue
        gold_items = [
            _make_gold_entry(_skins_by_id[sid])
            for sid in _skin_ids_by_container_id.get(container_id, [])
            if sid in _skins_by_id and _skins_by_id[sid].get('weaponType') in ('KNIFE', 'GLOVES')
        ]
        if gold_items:
            result[case_id] = gold_items
    return result

CASE_GOLD_ITEMS: Dict[str, list] = _build_case_gold_items()
logger.info(
    f"✅ Built CASE_GOLD_ITEMS for {len(CASE_GOLD_ITEMS)}/{len(CASES)} cases "
    f"({len(CASES) - len(CASE_GOLD_ITEMS)} fall back to the shared global gold pool)"
)

# ============================================================
# STICKER CAPSULES
# ============================================================

STICKER_CAPSULES = {
    "cs20_sticker_capsule": {
        "name": "CS20 Sticker Capsule",
        "emoji": "\ud83c\udf82",
        "price": 1.0,
        "image": "assets/containers/2103.webp",
        "stickers": [{"name": "CS20 Classic (Holo)", "rarity": "👑 Rare", "image": "assets/stickers/4513.webp"}, {"name": "Too Old for This", "rarity": "💫", "image": "assets/stickers/4514.webp"}, {"name": "Pixel Avenger", "rarity": "💫", "image": "assets/stickers/4515.webp"}, {"name": "Aztec", "rarity": "💫", "image": "assets/stickers/4516.webp"}, {"name": "Too Late", "rarity": "💫", "image": "assets/stickers/4517.webp"}, {"name": "Friend Code", "rarity": "💫", "image": "assets/stickers/4518.webp"}, {"name": "Clutchman (Holo)", "rarity": "👑 Rare", "image": "assets/stickers/4519.webp"}, {"name": "All Hail the King (Foil)", "rarity": "🔥", "image": "assets/stickers/4520.webp"}, {"name": "Door Stuck (Foil)", "rarity": "🔥", "image": "assets/stickers/4521.webp"}, {"name": "Dragon Lore (Foil)", "rarity": "🔥", "image": "assets/stickers/4522.webp"}, {"name": "Guinea Pig (Holo)", "rarity": "👑 Rare", "image": "assets/stickers/4523.webp"}, {"name": "Obey SAS", "rarity": "💫", "image": "assets/stickers/4524.webp"}, {"name": "Fire in the Hole (Holo)", "rarity": "👑 Rare", "image": "assets/stickers/4525.webp"}, {"name": "Nuke Beast", "rarity": "💫", "image": "assets/stickers/4526.webp"}, {"name": "Mondays", "rarity": "💫", "image": "assets/stickers/4527.webp"}, {"name": "Boost (Holo)", "rarity": "👑 Rare", "image": "assets/stickers/4528.webp"}, {"name": "Rush 4x20 (Holo)", "rarity": "👑 Rare", "image": "assets/stickers/4529.webp"}, {"name": "Separate Pixels", "rarity": "💫", "image": "assets/stickers/4530.webp"}, {"name": "Surf's Up", "rarity": "💫", "image": "assets/stickers/4531.webp"}, {"name": "Temperance", "rarity": "💫", "image": "assets/stickers/4532.webp"}],
    },
    "recoil_sticker_collection": {
        "name": "Recoil Sticker Collection",
        "emoji": "\u2b50",
        "price": 0.5,
        "image": "assets/containers/2221.webp",
        "stickers": [{"name": "Hello AK-47", "rarity": "💫", "image": "assets/stickers/4649.webp"}, {"name": "Hello AK-47 (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4650.webp"}, {"name": "Hello AUG", "rarity": "💫", "image": "assets/stickers/4651.webp"}, {"name": "Hello AUG (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4652.webp"}, {"name": "Hello AWP", "rarity": "💫", "image": "assets/stickers/4653.webp"}, {"name": "Hello AWP (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4654.webp"}, {"name": "Hello PP-Bizon", "rarity": "💫", "image": "assets/stickers/4655.webp"}, {"name": "Hello PP-Bizon (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4656.webp"}, {"name": "Hello CZ75-Auto", "rarity": "💫", "image": "assets/stickers/4657.webp"}, {"name": "Hello CZ75-Auto (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4658.webp"}, {"name": "Hello FAMAS", "rarity": "💫", "image": "assets/stickers/4659.webp"}, {"name": "Hello FAMAS (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4660.webp"}, {"name": "Hello Galil AR", "rarity": "💫", "image": "assets/stickers/4661.webp"}, {"name": "Hello Galil AR (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4662.webp"}, {"name": "Hello M4A1-S", "rarity": "💫", "image": "assets/stickers/4663.webp"}, {"name": "Hello M4A1-S (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4664.webp"}, {"name": "Hello M4A4", "rarity": "💫", "image": "assets/stickers/4665.webp"}, {"name": "Hello M4A4 (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4666.webp"}, {"name": "Hello MAC-10", "rarity": "💫", "image": "assets/stickers/4667.webp"}, {"name": "Hello MAC-10 (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4668.webp"}, {"name": "Hello MP7", "rarity": "💫", "image": "assets/stickers/4669.webp"}, {"name": "Hello MP7 (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4670.webp"}, {"name": "Hello MP9", "rarity": "💫", "image": "assets/stickers/4671.webp"}, {"name": "Hello MP9 (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4672.webp"}, {"name": "Hello P90", "rarity": "💫", "image": "assets/stickers/4673.webp"}, {"name": "Hello P90 (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4674.webp"}, {"name": "Hello SG 553", "rarity": "💫", "image": "assets/stickers/4675.webp"}, {"name": "Hello SG 553 (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4676.webp"}, {"name": "Hello UMP-45", "rarity": "💫", "image": "assets/stickers/4677.webp"}, {"name": "Hello UMP-45 (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4678.webp"}, {"name": "Hello XM1014", "rarity": "💫", "image": "assets/stickers/4679.webp"}, {"name": "Hello XM1014 (Gold)", "rarity": "👑 Rare", "image": "assets/stickers/4680.webp"}],
    },
    "austin_2025_champions_autograp": {
        "name": "Austin 2025 Champions Autograph",
        "emoji": "\ud83c\udfc6",
        "price": 5.0,
        "image": "assets/containers/1989.webp",
        "stickers": [{"name": "apEX (Champion) | Austin 2025", "rarity": "💫", "image": "assets/stickers/9431.webp"}, {"name": "apEX (Foil, Champion) | Austin 2025", "rarity": "👑 Rare", "image": "assets/stickers/9432.webp"}, {"name": "apEX (Holo, Champion) | Austin 2025", "rarity": "🔥", "image": "assets/stickers/9433.webp"}, {"name": "apEX (Gold, Champion) | Austin 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9434.webp"}, {"name": "ZywOo (Champion) | Austin 2025", "rarity": "💫", "image": "assets/stickers/9435.webp"}, {"name": "ZywOo (Foil, Champion) | Austin 2025", "rarity": "👑 Rare", "image": "assets/stickers/9436.webp"}, {"name": "ZywOo (Holo, Champion) | Austin 2025", "rarity": "🔥", "image": "assets/stickers/9437.webp"}, {"name": "ZywOo (Gold, Champion) | Austin 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9438.webp"}, {"name": "FlameZ (Champion) | Austin 2025", "rarity": "💫", "image": "assets/stickers/9439.webp"}, {"name": "FlameZ (Foil, Champion) | Austin 2025", "rarity": "👑 Rare", "image": "assets/stickers/9440.webp"}, {"name": "FlameZ (Holo, Champion) | Austin 2025", "rarity": "🔥", "image": "assets/stickers/9441.webp"}, {"name": "FlameZ (Gold, Champion) | Austin 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9442.webp"}, {"name": "mezii (Champion) | Austin 2025", "rarity": "💫", "image": "assets/stickers/9443.webp"}, {"name": "mezii (Foil, Champion) | Austin 2025", "rarity": "👑 Rare", "image": "assets/stickers/9444.webp"}, {"name": "mezii (Holo, Champion) | Austin 2025", "rarity": "🔥", "image": "assets/stickers/9445.webp"}, {"name": "mezii (Gold, Champion) | Austin 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9446.webp"}, {"name": "ropz (Champion) | Austin 2025", "rarity": "💫", "image": "assets/stickers/9447.webp"}, {"name": "ropz (Foil, Champion) | Austin 2025", "rarity": "👑 Rare", "image": "assets/stickers/9448.webp"}, {"name": "ropz (Holo, Champion) | Austin 2025", "rarity": "🔥", "image": "assets/stickers/9449.webp"}, {"name": "ropz (Gold, Champion) | Austin 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9450.webp"}],
    },
    "budapest_2025_champions_autogr": {
        "name": "Budapest 2025 Champions Autograph",
        "emoji": "\ud83c\udfc6",
        "price": 5.0,
        "image": "assets/containers/2098.webp",
        "stickers": [{"name": "apEX (Champion) | Budapest 2025", "rarity": "💫", "image": "assets/stickers/10294.webp"}, {"name": "apEX (Embroidered, Champion) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/10295.webp"}, {"name": "apEX (Holo, Champion) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/10296.webp"}, {"name": "apEX (Gold, Champion) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/10297.webp"}, {"name": "FlameZ (Champion) | Budapest 2025", "rarity": "💫", "image": "assets/stickers/10298.webp"}, {"name": "FlameZ (Embroidered, Champion) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/10299.webp"}, {"name": "FlameZ (Holo, Champion) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/10300.webp"}, {"name": "FlameZ (Gold, Champion) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/10301.webp"}, {"name": "mezii (Champion) | Budapest 2025", "rarity": "💫", "image": "assets/stickers/10302.webp"}, {"name": "mezii (Embroidered, Champion) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/10303.webp"}, {"name": "mezii (Holo, Champion) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/10304.webp"}, {"name": "mezii (Gold, Champion) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/10305.webp"}, {"name": "ropz (Champion) | Budapest 2025", "rarity": "💫", "image": "assets/stickers/10306.webp"}, {"name": "ropz (Embroidered, Champion) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/10307.webp"}, {"name": "ropz (Holo, Champion) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/10308.webp"}, {"name": "ropz (Gold, Champion) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/10309.webp"}, {"name": "ZywOo (Champion) | Budapest 2025", "rarity": "💫", "image": "assets/stickers/10310.webp"}, {"name": "ZywOo (Embroidered, Champion) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/10311.webp"}, {"name": "ZywOo (Holo, Champion) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/10312.webp"}, {"name": "ZywOo (Gold, Champion) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/10313.webp"}],
    },
    "copenhagen_2024_champions_auto": {
        "name": "Copenhagen 2024 Champions Autograph",
        "emoji": "\ud83c\udfc6",
        "price": 4.0,
        "image": "assets/containers/2112.webp",
        "stickers": [{"name": "jL (Champion) | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7859.webp"}, {"name": "jL (Glitter, Champion) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7860.webp"}, {"name": "jL (Holo, Champion) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7861.webp"}, {"name": "jL (Gold, Champion) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7862.webp"}, {"name": "Aleksib (Champion) | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7863.webp"}, {"name": "Aleksib (Glitter, Champion) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7864.webp"}, {"name": "Aleksib (Holo, Champion) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7865.webp"}, {"name": "Aleksib (Gold, Champion) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7866.webp"}, {"name": "b1t (Champion) | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7867.webp"}, {"name": "b1t (Glitter, Champion) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7868.webp"}, {"name": "b1t (Holo, Champion) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7869.webp"}, {"name": "b1t (Gold, Champion) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7870.webp"}, {"name": "iM (Champion) | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7871.webp"}, {"name": "iM (Glitter, Champion) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7872.webp"}, {"name": "iM (Holo, Champion) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7873.webp"}, {"name": "iM (Gold, Champion) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7874.webp"}, {"name": "w0nderful (Champion) | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7875.webp"}, {"name": "w0nderful (Glitter, Champion) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7876.webp"}, {"name": "w0nderful (Holo, Champion) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7877.webp"}, {"name": "w0nderful (Gold, Champion) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7878.webp"}],
    },
    "shanghai_2024_champions_autogr": {
        "name": "Shanghai 2024 Champions Autograph",
        "emoji": "\ud83c\udfc6",
        "price": 4.0,
        "image": "assets/containers/2156.webp",
        "stickers": [{"name": "chopper (Champion) | Shanghai 2024", "rarity": "💫", "image": "assets/stickers/8533.webp"}, {"name": "chopper (Glitter, Champion) | Shanghai 2024", "rarity": "👑 Rare", "image": "assets/stickers/8534.webp"}, {"name": "chopper (Holo, Champion) | Shanghai 2024", "rarity": "🔥", "image": "assets/stickers/8535.webp"}, {"name": "chopper (Gold, Champion) | Shanghai 2024", "rarity": "👑 Legendary", "image": "assets/stickers/8536.webp"}, {"name": "magixx (Champion) | Shanghai 2024", "rarity": "💫", "image": "assets/stickers/8537.webp"}, {"name": "magixx (Glitter, Champion) | Shanghai 2024", "rarity": "👑 Rare", "image": "assets/stickers/8538.webp"}, {"name": "magixx (Holo, Champion) | Shanghai 2024", "rarity": "🔥", "image": "assets/stickers/8539.webp"}, {"name": "magixx (Gold, Champion) | Shanghai 2024", "rarity": "👑 Legendary", "image": "assets/stickers/8540.webp"}, {"name": "donk (Champion) | Shanghai 2024", "rarity": "💫", "image": "assets/stickers/8541.webp"}, {"name": "donk (Glitter, Champion) | Shanghai 2024", "rarity": "👑 Rare", "image": "assets/stickers/8542.webp"}, {"name": "donk (Holo, Champion) | Shanghai 2024", "rarity": "🔥", "image": "assets/stickers/8543.webp"}, {"name": "donk (Gold, Champion) | Shanghai 2024", "rarity": "👑 Legendary", "image": "assets/stickers/8544.webp"}, {"name": "sh1ro (Champion) | Shanghai 2024", "rarity": "💫", "image": "assets/stickers/8545.webp"}, {"name": "sh1ro (Glitter, Champion) | Shanghai 2024", "rarity": "👑 Rare", "image": "assets/stickers/8546.webp"}, {"name": "sh1ro (Holo, Champion) | Shanghai 2024", "rarity": "🔥", "image": "assets/stickers/8547.webp"}, {"name": "sh1ro (Gold, Champion) | Shanghai 2024", "rarity": "👑 Legendary", "image": "assets/stickers/8548.webp"}, {"name": "zont1x (Champion) | Shanghai 2024", "rarity": "💫", "image": "assets/stickers/8549.webp"}, {"name": "zont1x (Glitter, Champion) | Shanghai 2024", "rarity": "👑 Rare", "image": "assets/stickers/8550.webp"}, {"name": "zont1x (Holo, Champion) | Shanghai 2024", "rarity": "🔥", "image": "assets/stickers/8551.webp"}, {"name": "zont1x (Gold, Champion) | Shanghai 2024", "rarity": "👑 Legendary", "image": "assets/stickers/8552.webp"}],
    },
    "paris_2023_champions_autograph": {
        "name": "Paris 2023 Champions Autograph",
        "emoji": "\ud83c\udfc6",
        "price": 3.0,
        "image": "assets/containers/2138.webp",
        "stickers": [{"name": "apEX (Champion) | Paris 2023", "rarity": "💫", "image": "assets/stickers/7213.webp"}, {"name": "apEX (Glitter, Champion) | Paris 2023", "rarity": "👑 Rare", "image": "assets/stickers/7214.webp"}, {"name": "apEX (Holo, Champion) | Paris 2023", "rarity": "🔥", "image": "assets/stickers/7215.webp"}, {"name": "apEX (Gold, Champion) | Paris 2023", "rarity": "👑 Legendary", "image": "assets/stickers/7216.webp"}, {"name": "dupreeh (Champion) | Paris 2023", "rarity": "💫", "image": "assets/stickers/7217.webp"}, {"name": "dupreeh (Glitter, Champion) | Paris 2023", "rarity": "👑 Rare", "image": "assets/stickers/7218.webp"}, {"name": "dupreeh (Holo, Champion) | Paris 2023", "rarity": "🔥", "image": "assets/stickers/7219.webp"}, {"name": "dupreeh (Gold, Champion) | Paris 2023", "rarity": "👑 Legendary", "image": "assets/stickers/7220.webp"}, {"name": "Magisk (Champion) | Paris 2023", "rarity": "💫", "image": "assets/stickers/7221.webp"}, {"name": "Magisk (Glitter, Champion) | Paris 2023", "rarity": "👑 Rare", "image": "assets/stickers/7222.webp"}, {"name": "Magisk (Holo, Champion) | Paris 2023", "rarity": "🔥", "image": "assets/stickers/7223.webp"}, {"name": "Magisk (Gold, Champion) | Paris 2023", "rarity": "👑 Legendary", "image": "assets/stickers/7224.webp"}, {"name": "Spinx (Champion) | Paris 2023", "rarity": "💫", "image": "assets/stickers/7225.webp"}, {"name": "Spinx (Glitter, Champion) | Paris 2023", "rarity": "👑 Rare", "image": "assets/stickers/7226.webp"}, {"name": "Spinx (Holo, Champion) | Paris 2023", "rarity": "🔥", "image": "assets/stickers/7227.webp"}, {"name": "Spinx (Gold, Champion) | Paris 2023", "rarity": "👑 Legendary", "image": "assets/stickers/7228.webp"}, {"name": "ZywOo (Champion) | Paris 2023", "rarity": "💫", "image": "assets/stickers/7229.webp"}, {"name": "ZywOo (Glitter, Champion) | Paris 2023", "rarity": "👑 Rare", "image": "assets/stickers/7230.webp"}, {"name": "ZywOo (Holo, Champion) | Paris 2023", "rarity": "🔥", "image": "assets/stickers/7231.webp"}, {"name": "ZywOo (Gold, Champion) | Paris 2023", "rarity": "👑 Legendary", "image": "assets/stickers/7232.webp"}],
    },
    "rio_2022_champions_autograph": {
        "name": "Rio 2022 Champions Autograph",
        "emoji": "\ud83c\udfc6",
        "price": 2.5,
        "image": "assets/containers/2149.webp",
        "stickers": [{"name": "FL1T (Champion) | Rio 2022", "rarity": "💫", "image": "assets/stickers/6566.webp"}, {"name": "FL1T (Glitter, Champion) | Rio 2022", "rarity": "👑 Rare", "image": "assets/stickers/6567.webp"}, {"name": "FL1T (Holo, Champion) | Rio 2022", "rarity": "🔥", "image": "assets/stickers/6568.webp"}, {"name": "FL1T (Gold, Champion) | Rio 2022", "rarity": "👑 Legendary", "image": "assets/stickers/6569.webp"}, {"name": "n0rb3r7 (Champion) | Rio 2022", "rarity": "💫", "image": "assets/stickers/6570.webp"}, {"name": "n0rb3r7 (Glitter, Champion) | Rio 2022", "rarity": "👑 Rare", "image": "assets/stickers/6571.webp"}, {"name": "n0rb3r7 (Holo, Champion) | Rio 2022", "rarity": "🔥", "image": "assets/stickers/6572.webp"}, {"name": "n0rb3r7 (Gold, Champion) | Rio 2022", "rarity": "👑 Legendary", "image": "assets/stickers/6573.webp"}, {"name": "Jame (Champion) | Rio 2022", "rarity": "💫", "image": "assets/stickers/6574.webp"}, {"name": "Jame (Glitter, Champion) | Rio 2022", "rarity": "👑 Rare", "image": "assets/stickers/6575.webp"}, {"name": "Jame (Holo, Champion) | Rio 2022", "rarity": "🔥", "image": "assets/stickers/6576.webp"}, {"name": "Jame (Gold, Champion) | Rio 2022", "rarity": "👑 Legendary", "image": "assets/stickers/6577.webp"}, {"name": "qikert (Champion) | Rio 2022", "rarity": "💫", "image": "assets/stickers/6578.webp"}, {"name": "qikert (Glitter, Champion) | Rio 2022", "rarity": "👑 Rare", "image": "assets/stickers/6579.webp"}, {"name": "qikert (Holo, Champion) | Rio 2022", "rarity": "🔥", "image": "assets/stickers/6580.webp"}, {"name": "qikert (Gold, Champion) | Rio 2022", "rarity": "👑 Legendary", "image": "assets/stickers/6581.webp"}, {"name": "fame (Champion) | Rio 2022", "rarity": "💫", "image": "assets/stickers/6582.webp"}, {"name": "fame (Glitter, Champion) | Rio 2022", "rarity": "👑 Rare", "image": "assets/stickers/6583.webp"}, {"name": "fame (Holo, Champion) | Rio 2022", "rarity": "🔥", "image": "assets/stickers/6584.webp"}, {"name": "fame (Gold, Champion) | Rio 2022", "rarity": "👑 Legendary", "image": "assets/stickers/6585.webp"}],
    },
    "antwerp_2022_champions_autogra": {
        "name": "Antwerp 2022 Champions Autograph",
        "emoji": "\ud83c\udfc6",
        "price": 2.5,
        "image": "assets/containers/1982.webp",
        "stickers": [{"name": "rain (Champion) | Antwerp 2022", "rarity": "💫", "image": "assets/stickers/5876.webp"}, {"name": "rain (Glitter, Champion) | Antwerp 2022", "rarity": "👑 Rare", "image": "assets/stickers/5877.webp"}, {"name": "rain (Holo, Champion) | Antwerp 2022", "rarity": "🔥", "image": "assets/stickers/5878.webp"}, {"name": "rain (Gold, Champion) | Antwerp 2022", "rarity": "👑 Legendary", "image": "assets/stickers/5879.webp"}, {"name": "karrigan (Champion) | Antwerp 2022", "rarity": "💫", "image": "assets/stickers/5880.webp"}, {"name": "karrigan (Glitter, Champion) | Antwerp 2022", "rarity": "👑 Rare", "image": "assets/stickers/5881.webp"}, {"name": "karrigan (Holo, Champion) | Antwerp 2022", "rarity": "🔥", "image": "assets/stickers/5882.webp"}, {"name": "karrigan (Gold, Champion) | Antwerp 2022", "rarity": "👑 Legendary", "image": "assets/stickers/5883.webp"}, {"name": "Twistzz (Champion) | Antwerp 2022", "rarity": "💫", "image": "assets/stickers/5884.webp"}, {"name": "Twistzz (Glitter, Champion) | Antwerp 2022", "rarity": "👑 Rare", "image": "assets/stickers/5885.webp"}, {"name": "Twistzz (Holo, Champion) | Antwerp 2022", "rarity": "🔥", "image": "assets/stickers/5886.webp"}, {"name": "Twistzz (Gold, Champion) | Antwerp 2022", "rarity": "👑 Legendary", "image": "assets/stickers/5887.webp"}, {"name": "broky (Champion) | Antwerp 2022", "rarity": "💫", "image": "assets/stickers/5888.webp"}, {"name": "broky (Glitter, Champion) | Antwerp 2022", "rarity": "👑 Rare", "image": "assets/stickers/5889.webp"}, {"name": "broky (Holo, Champion) | Antwerp 2022", "rarity": "🔥", "image": "assets/stickers/5890.webp"}, {"name": "broky (Gold, Champion) | Antwerp 2022", "rarity": "👑 Legendary", "image": "assets/stickers/5891.webp"}, {"name": "ropz (Champion) | Antwerp 2022", "rarity": "💫", "image": "assets/stickers/5892.webp"}, {"name": "ropz (Glitter, Champion) | Antwerp 2022", "rarity": "👑 Rare", "image": "assets/stickers/5893.webp"}, {"name": "ropz (Holo, Champion) | Antwerp 2022", "rarity": "🔥", "image": "assets/stickers/5894.webp"}, {"name": "ropz (Gold, Champion) | Antwerp 2022", "rarity": "👑 Legendary", "image": "assets/stickers/5895.webp"}],
    },
    "stockholm_2021_champions_autog": {
        "name": "Stockholm 2021 Champions Autograph",
        "emoji": "\ud83c\udfc6",
        "price": 2.0,
        "image": "assets/containers/2173.webp",
        "stickers": [{"name": "s1mple | Stockholm 2021", "rarity": "💫", "image": "assets/stickers/5129.webp"}, {"name": "s1mple (Holo) | Stockholm 2021", "rarity": "👑 Rare", "image": "assets/stickers/5130.webp"}, {"name": "s1mple (Gold) | Stockholm 2021", "rarity": "👑 Legendary", "image": "assets/stickers/5131.webp"}, {"name": "Perfecto | Stockholm 2021", "rarity": "💫", "image": "assets/stickers/5132.webp"}, {"name": "Perfecto (Holo) | Stockholm 2021", "rarity": "👑 Rare", "image": "assets/stickers/5133.webp"}, {"name": "Perfecto (Gold) | Stockholm 2021", "rarity": "👑 Legendary", "image": "assets/stickers/5134.webp"}, {"name": "Boombl4 | Stockholm 2021", "rarity": "💫", "image": "assets/stickers/5135.webp"}, {"name": "Boombl4 (Holo) | Stockholm 2021", "rarity": "👑 Rare", "image": "assets/stickers/5136.webp"}, {"name": "Boombl4 (Gold) | Stockholm 2021", "rarity": "👑 Legendary", "image": "assets/stickers/5137.webp"}, {"name": "b1t | Stockholm 2021", "rarity": "💫", "image": "assets/stickers/5138.webp"}, {"name": "b1t (Holo) | Stockholm 2021", "rarity": "👑 Rare", "image": "assets/stickers/5139.webp"}, {"name": "b1t (Gold) | Stockholm 2021", "rarity": "👑 Legendary", "image": "assets/stickers/5140.webp"}, {"name": "electroNic | Stockholm 2021", "rarity": "💫", "image": "assets/stickers/5141.webp"}, {"name": "electroNic (Holo) | Stockholm 2021", "rarity": "👑 Rare", "image": "assets/stickers/5142.webp"}, {"name": "electroNic (Gold) | Stockholm 2021", "rarity": "👑 Legendary", "image": "assets/stickers/5143.webp"}],
    },
    "boston_2018_legends_autograph": {
        "name": "Boston 2018 Legends Autograph",
        "emoji": "\ud83d\udd25",
        "price": 1.5,
        "image": "assets/containers/2092.webp",
        "stickers": [{"name": "AdreN | Boston 2018", "rarity": "💫", "image": "assets/stickers/2561.webp"}, {"name": "AdreN (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2562.webp"}, {"name": "AdreN (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2563.webp"}, {"name": "Dosia | Boston 2018", "rarity": "💫", "image": "assets/stickers/2564.webp"}, {"name": "Dosia (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2565.webp"}, {"name": "Dosia (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2566.webp"}, {"name": "fitch | Boston 2018", "rarity": "💫", "image": "assets/stickers/2567.webp"}, {"name": "fitch (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2568.webp"}, {"name": "fitch (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2569.webp"}, {"name": "Hobbit | Boston 2018", "rarity": "💫", "image": "assets/stickers/2570.webp"}, {"name": "Hobbit (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2571.webp"}, {"name": "Hobbit (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2572.webp"}, {"name": "mou | Boston 2018", "rarity": "💫", "image": "assets/stickers/2573.webp"}, {"name": "mou (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2574.webp"}, {"name": "mou (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2575.webp"}, {"name": "BIT | Boston 2018", "rarity": "💫", "image": "assets/stickers/2576.webp"}, {"name": "BIT (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2577.webp"}, {"name": "BIT (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2578.webp"}, {"name": "fnx | Boston 2018", "rarity": "💫", "image": "assets/stickers/2579.webp"}, {"name": "fnx (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2580.webp"}, {"name": "fnx (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2581.webp"}, {"name": "HEN1 | Boston 2018", "rarity": "💫", "image": "assets/stickers/2582.webp"}, {"name": "HEN1 (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2583.webp"}, {"name": "HEN1 (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2584.webp"}, {"name": "kNgV- | Boston 2018", "rarity": "💫", "image": "assets/stickers/2585.webp"}, {"name": "kNgV- (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2586.webp"}, {"name": "kNgV- (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2587.webp"}, {"name": "LUCAS1 | Boston 2018", "rarity": "💫", "image": "assets/stickers/2588.webp"}, {"name": "LUCAS1 (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2589.webp"}, {"name": "LUCAS1 (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2590.webp"}, {"name": "device | Boston 2018", "rarity": "💫", "image": "assets/stickers/2591.webp"}, {"name": "device (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2592.webp"}, {"name": "device (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2593.webp"}, {"name": "dupreeh | Boston 2018", "rarity": "💫", "image": "assets/stickers/2594.webp"}, {"name": "dupreeh (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2595.webp"}, {"name": "dupreeh (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2596.webp"}, {"name": "gla1ve | Boston 2018", "rarity": "💫", "image": "assets/stickers/2597.webp"}, {"name": "gla1ve (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2598.webp"}, {"name": "gla1ve (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2599.webp"}, {"name": "Kjaerbye | Boston 2018", "rarity": "💫", "image": "assets/stickers/2600.webp"}, {"name": "Kjaerbye (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2601.webp"}, {"name": "Kjaerbye (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2602.webp"}, {"name": "Xyp9x | Boston 2018", "rarity": "💫", "image": "assets/stickers/2603.webp"}, {"name": "Xyp9x (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2604.webp"}, {"name": "Xyp9x (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2605.webp"}, {"name": "byali | Boston 2018", "rarity": "💫", "image": "assets/stickers/2606.webp"}, {"name": "byali (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2607.webp"}, {"name": "byali (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2608.webp"}, {"name": "NEO | Boston 2018", "rarity": "💫", "image": "assets/stickers/2609.webp"}, {"name": "NEO (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2610.webp"}, {"name": "NEO (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2611.webp"}, {"name": "pashaBiceps | Boston 2018", "rarity": "💫", "image": "assets/stickers/2612.webp"}, {"name": "pashaBiceps (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2613.webp"}, {"name": "pashaBiceps (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2614.webp"}, {"name": "Snax | Boston 2018", "rarity": "💫", "image": "assets/stickers/2615.webp"}, {"name": "Snax (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2616.webp"}, {"name": "Snax (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2617.webp"}, {"name": "TaZ | Boston 2018", "rarity": "💫", "image": "assets/stickers/2618.webp"}, {"name": "TaZ (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2619.webp"}, {"name": "TaZ (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2620.webp"}, {"name": "flusha | Boston 2018", "rarity": "💫", "image": "assets/stickers/2621.webp"}, {"name": "flusha (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2622.webp"}, {"name": "flusha (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2623.webp"}, {"name": "Golden | Boston 2018", "rarity": "💫", "image": "assets/stickers/2624.webp"}, {"name": "Golden (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2625.webp"}, {"name": "Golden (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2626.webp"}, {"name": "JW | Boston 2018", "rarity": "💫", "image": "assets/stickers/2627.webp"}, {"name": "JW (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2628.webp"}, {"name": "JW (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2629.webp"}, {"name": "KRIMZ | Boston 2018", "rarity": "💫", "image": "assets/stickers/2630.webp"}, {"name": "KRIMZ (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2631.webp"}, {"name": "KRIMZ (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2632.webp"}, {"name": "Lekr0 | Boston 2018", "rarity": "💫", "image": "assets/stickers/2633.webp"}, {"name": "Lekr0 (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2634.webp"}, {"name": "Lekr0 (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2635.webp"}, {"name": "coldzera | Boston 2018", "rarity": "💫", "image": "assets/stickers/2636.webp"}, {"name": "coldzera (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2637.webp"}, {"name": "coldzera (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2638.webp"}, {"name": "FalleN | Boston 2018", "rarity": "💫", "image": "assets/stickers/2639.webp"}, {"name": "FalleN (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2640.webp"}, {"name": "FalleN (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2641.webp"}, {"name": "felps | Boston 2018", "rarity": "💫", "image": "assets/stickers/2642.webp"}, {"name": "felps (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2643.webp"}, {"name": "felps (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2644.webp"}, {"name": "fer | Boston 2018", "rarity": "💫", "image": "assets/stickers/2645.webp"}, {"name": "fer (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2646.webp"}, {"name": "fer (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2647.webp"}, {"name": "TACO | Boston 2018", "rarity": "💫", "image": "assets/stickers/2648.webp"}, {"name": "TACO (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2649.webp"}, {"name": "TACO (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2650.webp"}, {"name": "gob b | Boston 2018", "rarity": "💫", "image": "assets/stickers/2651.webp"}, {"name": "gob b (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2652.webp"}, {"name": "gob b (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2653.webp"}, {"name": "keev | Boston 2018", "rarity": "💫", "image": "assets/stickers/2654.webp"}, {"name": "keev (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2655.webp"}, {"name": "keev (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2656.webp"}, {"name": "LEGIJA | Boston 2018", "rarity": "💫", "image": "assets/stickers/2657.webp"}, {"name": "LEGIJA (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2658.webp"}, {"name": "LEGIJA (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2659.webp"}, {"name": "nex | Boston 2018", "rarity": "💫", "image": "assets/stickers/2660.webp"}, {"name": "nex (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2661.webp"}, {"name": "nex (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2662.webp"}, {"name": "tabseN | Boston 2018", "rarity": "💫", "image": "assets/stickers/2663.webp"}, {"name": "tabseN (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2664.webp"}, {"name": "tabseN (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2665.webp"}, {"name": "aizy | Boston 2018", "rarity": "💫", "image": "assets/stickers/2666.webp"}, {"name": "aizy (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2667.webp"}, {"name": "aizy (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2668.webp"}, {"name": "cajunb | Boston 2018", "rarity": "💫", "image": "assets/stickers/2669.webp"}, {"name": "cajunb (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2670.webp"}, {"name": "cajunb (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2671.webp"}, {"name": "k0nfig | Boston 2018", "rarity": "💫", "image": "assets/stickers/2672.webp"}, {"name": "k0nfig (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2673.webp"}, {"name": "k0nfig (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2674.webp"}, {"name": "MSL | Boston 2018", "rarity": "💫", "image": "assets/stickers/2675.webp"}, {"name": "MSL (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2676.webp"}, {"name": "MSL (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2677.webp"}, {"name": "v4lde | Boston 2018", "rarity": "💫", "image": "assets/stickers/2678.webp"}, {"name": "v4lde (Foil) | Boston 2018", "rarity": "👑 Rare", "image": "assets/stickers/2679.webp"}, {"name": "v4lde (Gold) | Boston 2018", "rarity": "👑 Legendary", "image": "assets/stickers/2680.webp"}],
    },
    "london_2018_legends_autograph": {
        "name": "London 2018 Legends Autograph",
        "emoji": "\ud83d\udd25",
        "price": 1.5,
        "image": "assets/containers/2130.webp",
        "stickers": [{"name": "Golden | London 2018", "rarity": "💫", "image": "assets/stickers/3080.webp"}, {"name": "Golden (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3081.webp"}, {"name": "Golden (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3082.webp"}, {"name": "autimatic | London 2018", "rarity": "💫", "image": "assets/stickers/3083.webp"}, {"name": "autimatic (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3084.webp"}, {"name": "autimatic (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3085.webp"}, {"name": "RUSH | London 2018", "rarity": "💫", "image": "assets/stickers/3086.webp"}, {"name": "RUSH (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3087.webp"}, {"name": "RUSH (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3088.webp"}, {"name": "STYKO | London 2018", "rarity": "💫", "image": "assets/stickers/3089.webp"}, {"name": "STYKO (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3090.webp"}, {"name": "STYKO (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3091.webp"}, {"name": "Skadoodle | London 2018", "rarity": "💫", "image": "assets/stickers/3092.webp"}, {"name": "Skadoodle (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3093.webp"}, {"name": "Skadoodle (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3094.webp"}, {"name": "GuardiaN | London 2018", "rarity": "💫", "image": "assets/stickers/3095.webp"}, {"name": "GuardiaN (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3096.webp"}, {"name": "GuardiaN (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3097.webp"}, {"name": "olofmeister | London 2018", "rarity": "💫", "image": "assets/stickers/3098.webp"}, {"name": "olofmeister (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3099.webp"}, {"name": "olofmeister (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3100.webp"}, {"name": "karrigan | London 2018", "rarity": "💫", "image": "assets/stickers/3101.webp"}, {"name": "karrigan (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3102.webp"}, {"name": "karrigan (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3103.webp"}, {"name": "rain | London 2018", "rarity": "💫", "image": "assets/stickers/3104.webp"}, {"name": "rain (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3105.webp"}, {"name": "rain (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3106.webp"}, {"name": "NiKo | London 2018", "rarity": "💫", "image": "assets/stickers/3107.webp"}, {"name": "NiKo (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3108.webp"}, {"name": "NiKo (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3109.webp"}, {"name": "electronic | London 2018", "rarity": "💫", "image": "assets/stickers/3110.webp"}, {"name": "electronic (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3111.webp"}, {"name": "electronic (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3112.webp"}, {"name": "Zeus | London 2018", "rarity": "💫", "image": "assets/stickers/3113.webp"}, {"name": "Zeus (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3114.webp"}, {"name": "Zeus (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3115.webp"}, {"name": "s1mple | London 2018", "rarity": "💫", "image": "assets/stickers/3116.webp"}, {"name": "s1mple (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3117.webp"}, {"name": "s1mple (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3118.webp"}, {"name": "Edward | London 2018", "rarity": "💫", "image": "assets/stickers/3119.webp"}, {"name": "Edward (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3120.webp"}, {"name": "Edward (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3121.webp"}, {"name": "flamie | London 2018", "rarity": "💫", "image": "assets/stickers/3122.webp"}, {"name": "flamie (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3123.webp"}, {"name": "flamie (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3124.webp"}, {"name": "coldzera | London 2018", "rarity": "💫", "image": "assets/stickers/3125.webp"}, {"name": "coldzera (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3126.webp"}, {"name": "coldzera (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3127.webp"}, {"name": "FalleN | London 2018", "rarity": "💫", "image": "assets/stickers/3128.webp"}, {"name": "FalleN (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3129.webp"}, {"name": "FalleN (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3130.webp"}, {"name": "tarik | London 2018", "rarity": "💫", "image": "assets/stickers/3131.webp"}, {"name": "tarik (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3132.webp"}, {"name": "tarik (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3133.webp"}, {"name": "Stewie2K | London 2018", "rarity": "💫", "image": "assets/stickers/3134.webp"}, {"name": "Stewie2K (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3135.webp"}, {"name": "Stewie2K (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3136.webp"}, {"name": "fer | London 2018", "rarity": "💫", "image": "assets/stickers/3137.webp"}, {"name": "fer (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3138.webp"}, {"name": "fer (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3139.webp"}, {"name": "Snax | London 2018", "rarity": "💫", "image": "assets/stickers/3140.webp"}, {"name": "Snax (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3141.webp"}, {"name": "Snax (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3142.webp"}, {"name": "chrisJ | London 2018", "rarity": "💫", "image": "assets/stickers/3143.webp"}, {"name": "chrisJ (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3144.webp"}, {"name": "chrisJ (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3145.webp"}, {"name": "ropz | London 2018", "rarity": "💫", "image": "assets/stickers/3146.webp"}, {"name": "ropz (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3147.webp"}, {"name": "ropz (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3148.webp"}, {"name": "suNny | London 2018", "rarity": "💫", "image": "assets/stickers/3149.webp"}, {"name": "suNny (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3150.webp"}, {"name": "suNny (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3151.webp"}, {"name": "oskar | London 2018", "rarity": "💫", "image": "assets/stickers/3152.webp"}, {"name": "oskar (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3153.webp"}, {"name": "oskar (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3154.webp"}, {"name": "jmqa | London 2018", "rarity": "💫", "image": "assets/stickers/3155.webp"}, {"name": "jmqa (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3156.webp"}, {"name": "jmqa (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3157.webp"}, {"name": "Kvik | London 2018", "rarity": "💫", "image": "assets/stickers/3158.webp"}, {"name": "Kvik (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3159.webp"}, {"name": "Kvik (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3160.webp"}, {"name": "balblna | London 2018", "rarity": "💫", "image": "assets/stickers/3161.webp"}, {"name": "balblna (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3162.webp"}, {"name": "balblna (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3163.webp"}, {"name": "waterfaLLZ | London 2018", "rarity": "💫", "image": "assets/stickers/3164.webp"}, {"name": "waterfaLLZ (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3165.webp"}, {"name": "waterfaLLZ (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3166.webp"}, {"name": "Boombl4 | London 2018", "rarity": "💫", "image": "assets/stickers/3167.webp"}, {"name": "Boombl4 (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3168.webp"}, {"name": "Boombl4 (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3169.webp"}, {"name": "kennyS | London 2018", "rarity": "💫", "image": "assets/stickers/3170.webp"}, {"name": "kennyS (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3171.webp"}, {"name": "kennyS (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3172.webp"}, {"name": "bodyy | London 2018", "rarity": "💫", "image": "assets/stickers/3173.webp"}, {"name": "bodyy (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3174.webp"}, {"name": "bodyy (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3175.webp"}, {"name": "shox | London 2018", "rarity": "💫", "image": "assets/stickers/3176.webp"}, {"name": "shox (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3177.webp"}, {"name": "shox (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3178.webp"}, {"name": "Ex6TenZ | London 2018", "rarity": "💫", "image": "assets/stickers/3179.webp"}, {"name": "Ex6TenZ (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3180.webp"}, {"name": "Ex6TenZ (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3181.webp"}, {"name": "SmithZz | London 2018", "rarity": "💫", "image": "assets/stickers/3182.webp"}, {"name": "SmithZz (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3183.webp"}, {"name": "SmithZz (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3184.webp"}, {"name": "draken | London 2018", "rarity": "💫", "image": "assets/stickers/3185.webp"}, {"name": "draken (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3186.webp"}, {"name": "draken (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3187.webp"}, {"name": "JW | London 2018", "rarity": "💫", "image": "assets/stickers/3188.webp"}, {"name": "JW (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3189.webp"}, {"name": "JW (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3190.webp"}, {"name": "KRIMZ | London 2018", "rarity": "💫", "image": "assets/stickers/3191.webp"}, {"name": "KRIMZ (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3192.webp"}, {"name": "KRIMZ (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3193.webp"}, {"name": "flusha | London 2018", "rarity": "💫", "image": "assets/stickers/3194.webp"}, {"name": "flusha (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3195.webp"}, {"name": "flusha (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3196.webp"}, {"name": "Xizt | London 2018", "rarity": "💫", "image": "assets/stickers/3197.webp"}, {"name": "Xizt (Foil) | London 2018", "rarity": "👑 Rare", "image": "assets/stickers/3198.webp"}, {"name": "Xizt (Gold) | London 2018", "rarity": "👑 Legendary", "image": "assets/stickers/3199.webp"}],
    },
    "katowice_2019_legends_autograp": {
        "name": "Katowice 2019 Legends Autograph",
        "emoji": "\ud83d\udd25",
        "price": 2.0,
        "image": "assets/containers/2125.webp",
        "stickers": [{"name": "Magisk | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3585.webp"}, {"name": "Magisk (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3586.webp"}, {"name": "Magisk (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3587.webp"}, {"name": "device | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3588.webp"}, {"name": "device (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3589.webp"}, {"name": "device (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3590.webp"}, {"name": "Xyp9x | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3591.webp"}, {"name": "Xyp9x (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3592.webp"}, {"name": "Xyp9x (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3593.webp"}, {"name": "dupreeh | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3594.webp"}, {"name": "dupreeh (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3595.webp"}, {"name": "dupreeh (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3596.webp"}, {"name": "gla1ve | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3597.webp"}, {"name": "gla1ve (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3598.webp"}, {"name": "gla1ve (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3599.webp"}, {"name": "gob b | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3615.webp"}, {"name": "gob b (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3616.webp"}, {"name": "gob b (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3617.webp"}, {"name": "tabseN | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3618.webp"}, {"name": "tabseN (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3619.webp"}, {"name": "tabseN (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3620.webp"}, {"name": "tiziaN | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3621.webp"}, {"name": "tiziaN (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3622.webp"}, {"name": "tiziaN (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3623.webp"}, {"name": "XANTARES | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3624.webp"}, {"name": "XANTARES (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3625.webp"}, {"name": "XANTARES (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3626.webp"}, {"name": "smooya | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3627.webp"}, {"name": "smooya (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3628.webp"}, {"name": "smooya (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3629.webp"}, {"name": "n0thing | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3645.webp"}, {"name": "n0thing (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3646.webp"}, {"name": "n0thing (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3647.webp"}, {"name": "Rickeh | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3648.webp"}, {"name": "Rickeh (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3649.webp"}, {"name": "Rickeh (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3650.webp"}, {"name": "stanislaw | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3651.webp"}, {"name": "stanislaw (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3652.webp"}, {"name": "stanislaw (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3653.webp"}, {"name": "dephh | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3654.webp"}, {"name": "dephh (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3655.webp"}, {"name": "dephh (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3656.webp"}, {"name": "ShahZaM | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3657.webp"}, {"name": "ShahZaM (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3658.webp"}, {"name": "ShahZaM (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3659.webp"}, {"name": "GuardiaN | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3675.webp"}, {"name": "GuardiaN (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3676.webp"}, {"name": "GuardiaN (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3677.webp"}, {"name": "olofmeister | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3678.webp"}, {"name": "olofmeister (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3679.webp"}, {"name": "olofmeister (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3680.webp"}, {"name": "rain | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3681.webp"}, {"name": "rain (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3682.webp"}, {"name": "rain (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3683.webp"}, {"name": "AdreN | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3684.webp"}, {"name": "AdreN (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3685.webp"}, {"name": "AdreN (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3686.webp"}, {"name": "NiKo | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3687.webp"}, {"name": "NiKo (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3688.webp"}, {"name": "NiKo (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3689.webp"}, {"name": "DeadFox | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3750.webp"}, {"name": "DeadFox (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3751.webp"}, {"name": "DeadFox (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3752.webp"}, {"name": "ANGE1 | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3753.webp"}, {"name": "ANGE1 (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3754.webp"}, {"name": "ANGE1 (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3755.webp"}, {"name": "Hobbit | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3756.webp"}, {"name": "Hobbit (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3757.webp"}, {"name": "Hobbit (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3758.webp"}, {"name": "ISSAA | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3759.webp"}, {"name": "ISSAA (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3760.webp"}, {"name": "ISSAA (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3761.webp"}, {"name": "woxic | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3762.webp"}, {"name": "woxic (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3763.webp"}, {"name": "woxic (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3764.webp"}, {"name": "FalleN | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3765.webp"}, {"name": "FalleN (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3766.webp"}, {"name": "FalleN (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3767.webp"}, {"name": "felps | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3768.webp"}, {"name": "felps (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3769.webp"}, {"name": "felps (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3770.webp"}, {"name": "fer | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3771.webp"}, {"name": "fer (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3772.webp"}, {"name": "fer (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3773.webp"}, {"name": "TACO | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3774.webp"}, {"name": "TACO (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3775.webp"}, {"name": "TACO (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3776.webp"}, {"name": "coldzera | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3777.webp"}, {"name": "coldzera (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3778.webp"}, {"name": "coldzera (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3779.webp"}, {"name": "Edward | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3780.webp"}, {"name": "Edward (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3781.webp"}, {"name": "Edward (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3782.webp"}, {"name": "Zeus | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3783.webp"}, {"name": "Zeus (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3784.webp"}, {"name": "Zeus (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3785.webp"}, {"name": "s1mple | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3786.webp"}, {"name": "s1mple (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3787.webp"}, {"name": "s1mple (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3788.webp"}, {"name": "electronic | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3789.webp"}, {"name": "electronic (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3790.webp"}, {"name": "electronic (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3791.webp"}, {"name": "flamie | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3792.webp"}, {"name": "flamie (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3793.webp"}, {"name": "flamie (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3794.webp"}, {"name": "nitr0 | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3840.webp"}, {"name": "nitr0 (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3841.webp"}, {"name": "nitr0 (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3842.webp"}, {"name": "Stewie2K | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3843.webp"}, {"name": "Stewie2K (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3844.webp"}, {"name": "Stewie2K (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3845.webp"}, {"name": "NAF | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3846.webp"}, {"name": "NAF (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3847.webp"}, {"name": "NAF (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3848.webp"}, {"name": "Twistzz | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3849.webp"}, {"name": "Twistzz (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3850.webp"}, {"name": "Twistzz (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3851.webp"}, {"name": "EliGE | Katowice 2019", "rarity": "💫", "image": "assets/stickers/3852.webp"}, {"name": "EliGE (Foil) | Katowice 2019", "rarity": "👑 Rare", "image": "assets/stickers/3853.webp"}, {"name": "EliGE (Gold) | Katowice 2019", "rarity": "👑 Legendary", "image": "assets/stickers/3854.webp"}],
    },
    "berlin_2019_legends_autograph": {
        "name": "Berlin 2019 Legends Autograph",
        "emoji": "\ud83d\udd25",
        "price": 2.0,
        "image": "assets/containers/2087.webp",
        "stickers": [{"name": "Magisk | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4144.webp"}, {"name": "Magisk (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4145.webp"}, {"name": "Magisk (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4146.webp"}, {"name": "device | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4147.webp"}, {"name": "device (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4148.webp"}, {"name": "device (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4149.webp"}, {"name": "Xyp9x | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4150.webp"}, {"name": "Xyp9x (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4151.webp"}, {"name": "Xyp9x (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4152.webp"}, {"name": "dupreeh | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4153.webp"}, {"name": "dupreeh (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4154.webp"}, {"name": "dupreeh (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4155.webp"}, {"name": "gla1ve | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4156.webp"}, {"name": "gla1ve (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4157.webp"}, {"name": "gla1ve (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4158.webp"}, {"name": "allu | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4159.webp"}, {"name": "allu (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4160.webp"}, {"name": "allu (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4161.webp"}, {"name": "Aerial | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4162.webp"}, {"name": "Aerial (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4163.webp"}, {"name": "Aerial (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4164.webp"}, {"name": "xseveN | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4165.webp"}, {"name": "xseveN (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4166.webp"}, {"name": "xseveN (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4167.webp"}, {"name": "Aleksib | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4168.webp"}, {"name": "Aleksib (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4169.webp"}, {"name": "Aleksib (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4170.webp"}, {"name": "sergej | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4171.webp"}, {"name": "sergej (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4172.webp"}, {"name": "sergej (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4173.webp"}, {"name": "FalleN | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4174.webp"}, {"name": "FalleN (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4175.webp"}, {"name": "FalleN (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4176.webp"}, {"name": "LUCAS1 | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4177.webp"}, {"name": "LUCAS1 (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4178.webp"}, {"name": "LUCAS1 (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4179.webp"}, {"name": "fer | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4180.webp"}, {"name": "fer (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4181.webp"}, {"name": "fer (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4182.webp"}, {"name": "TACO | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4183.webp"}, {"name": "TACO (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4184.webp"}, {"name": "TACO (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4185.webp"}, {"name": "coldzera | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4186.webp"}, {"name": "coldzera (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4187.webp"}, {"name": "coldzera (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4188.webp"}, {"name": "Zeus | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4189.webp"}, {"name": "Zeus (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4190.webp"}, {"name": "Zeus (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4191.webp"}, {"name": "s1mple | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4192.webp"}, {"name": "s1mple (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4193.webp"}, {"name": "s1mple (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4194.webp"}, {"name": "electronic | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4195.webp"}, {"name": "electronic (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4196.webp"}, {"name": "electronic (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4197.webp"}, {"name": "flamie | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4198.webp"}, {"name": "flamie (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4199.webp"}, {"name": "flamie (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4200.webp"}, {"name": "Boombl4 | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4201.webp"}, {"name": "Boombl4 (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4202.webp"}, {"name": "Boombl4 (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4203.webp"}, {"name": "f0rest | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4204.webp"}, {"name": "f0rest (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4205.webp"}, {"name": "f0rest (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4206.webp"}, {"name": "Lekr0 | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4207.webp"}, {"name": "Lekr0 (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4208.webp"}, {"name": "Lekr0 (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4209.webp"}, {"name": "GeT_RiGhT | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4210.webp"}, {"name": "GeT_RiGhT (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4211.webp"}, {"name": "GeT_RiGhT (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4212.webp"}, {"name": "REZ | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4213.webp"}, {"name": "REZ (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4214.webp"}, {"name": "REZ (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4215.webp"}, {"name": "Golden | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4216.webp"}, {"name": "Golden (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4217.webp"}, {"name": "Golden (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4218.webp"}, {"name": "NEO | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4219.webp"}, {"name": "NEO (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4220.webp"}, {"name": "NEO (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4221.webp"}, {"name": "GuardiaN | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4222.webp"}, {"name": "GuardiaN (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4223.webp"}, {"name": "GuardiaN (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4224.webp"}, {"name": "olofmeister | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4225.webp"}, {"name": "olofmeister (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4226.webp"}, {"name": "olofmeister (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4227.webp"}, {"name": "rain | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4228.webp"}, {"name": "rain (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4229.webp"}, {"name": "rain (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4230.webp"}, {"name": "NiKo | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4231.webp"}, {"name": "NiKo (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4232.webp"}, {"name": "NiKo (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4233.webp"}, {"name": "nitr0 | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4234.webp"}, {"name": "nitr0 (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4235.webp"}, {"name": "nitr0 (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4236.webp"}, {"name": "Stewie2K | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4237.webp"}, {"name": "Stewie2K (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4238.webp"}, {"name": "Stewie2K (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4239.webp"}, {"name": "NAF | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4240.webp"}, {"name": "NAF (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4241.webp"}, {"name": "NAF (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4242.webp"}, {"name": "Twistzz | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4243.webp"}, {"name": "Twistzz (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4244.webp"}, {"name": "Twistzz (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4245.webp"}, {"name": "EliGE | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4246.webp"}, {"name": "EliGE (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4247.webp"}, {"name": "EliGE (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4248.webp"}, {"name": "Gratisfaction | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4249.webp"}, {"name": "Gratisfaction (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4250.webp"}, {"name": "Gratisfaction (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4251.webp"}, {"name": "jks | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4252.webp"}, {"name": "jks (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4253.webp"}, {"name": "jks (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4254.webp"}, {"name": "AZR | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4255.webp"}, {"name": "AZR (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4256.webp"}, {"name": "AZR (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4257.webp"}, {"name": "jkaem | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4258.webp"}, {"name": "jkaem (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4259.webp"}, {"name": "jkaem (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4260.webp"}, {"name": "Liazz | Berlin 2019", "rarity": "💫", "image": "assets/stickers/4261.webp"}, {"name": "Liazz (Foil) | Berlin 2019", "rarity": "👑 Rare", "image": "assets/stickers/4262.webp"}, {"name": "Liazz (Gold) | Berlin 2019", "rarity": "👑 Legendary", "image": "assets/stickers/4263.webp"}],
    },
    "krakow_2017_legends_autograph": {
        "name": "Krakow 2017 Legends Autograph",
        "emoji": "\ud83d\udcab",
        "price": 1.5,
        "image": "assets/containers/2129.webp",
        "stickers": [{"name": "device | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2148.webp"}, {"name": "device (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2149.webp"}, {"name": "device (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2150.webp"}, {"name": "dupreeh | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2151.webp"}, {"name": "dupreeh (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2152.webp"}, {"name": "dupreeh (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2153.webp"}, {"name": "gla1ve | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2154.webp"}, {"name": "gla1ve (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2155.webp"}, {"name": "gla1ve (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2156.webp"}, {"name": "Kjaerbye | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2157.webp"}, {"name": "Kjaerbye (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2158.webp"}, {"name": "Kjaerbye (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2159.webp"}, {"name": "Xyp9x | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2160.webp"}, {"name": "Xyp9x (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2161.webp"}, {"name": "Xyp9x (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2162.webp"}, {"name": "byali | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2163.webp"}, {"name": "byali (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2164.webp"}, {"name": "byali (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2165.webp"}, {"name": "NEO | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2166.webp"}, {"name": "NEO (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2167.webp"}, {"name": "NEO (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2168.webp"}, {"name": "pashaBiceps | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2169.webp"}, {"name": "pashaBiceps (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2170.webp"}, {"name": "pashaBiceps (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2171.webp"}, {"name": "Snax | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2172.webp"}, {"name": "Snax (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2173.webp"}, {"name": "Snax (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2174.webp"}, {"name": "TaZ | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2175.webp"}, {"name": "TaZ (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2176.webp"}, {"name": "TaZ (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2177.webp"}, {"name": "dennis | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2178.webp"}, {"name": "dennis (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2179.webp"}, {"name": "dennis (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2180.webp"}, {"name": "flusha | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2181.webp"}, {"name": "flusha (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2182.webp"}, {"name": "flusha (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2183.webp"}, {"name": "JW | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2184.webp"}, {"name": "JW (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2185.webp"}, {"name": "JW (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2186.webp"}, {"name": "KRIMZ | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2187.webp"}, {"name": "KRIMZ (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2188.webp"}, {"name": "KRIMZ (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2189.webp"}, {"name": "olofmeister | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2190.webp"}, {"name": "olofmeister (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2191.webp"}, {"name": "olofmeister (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2192.webp"}, {"name": "coldzera | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2193.webp"}, {"name": "coldzera (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2194.webp"}, {"name": "coldzera (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2195.webp"}, {"name": "FalleN | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2196.webp"}, {"name": "FalleN (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2197.webp"}, {"name": "FalleN (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2198.webp"}, {"name": "felps | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2199.webp"}, {"name": "felps (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2200.webp"}, {"name": "felps (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2201.webp"}, {"name": "fer | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2202.webp"}, {"name": "fer (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2203.webp"}, {"name": "fer (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2204.webp"}, {"name": "TACO | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2205.webp"}, {"name": "TACO (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2206.webp"}, {"name": "TACO (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2207.webp"}, {"name": "Edward | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2208.webp"}, {"name": "Edward (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2209.webp"}, {"name": "Edward (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2210.webp"}, {"name": "flamie | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2211.webp"}, {"name": "flamie (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2212.webp"}, {"name": "flamie (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2213.webp"}, {"name": "GuardiaN | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2214.webp"}, {"name": "GuardiaN (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2215.webp"}, {"name": "GuardiaN (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2216.webp"}, {"name": "s1mple | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2217.webp"}, {"name": "s1mple (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2218.webp"}, {"name": "s1mple (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2219.webp"}, {"name": "seized | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2220.webp"}, {"name": "seized (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2221.webp"}, {"name": "seized (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2222.webp"}, {"name": "AdreN | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2223.webp"}, {"name": "AdreN (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2224.webp"}, {"name": "AdreN (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2225.webp"}, {"name": "Dosia | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2226.webp"}, {"name": "Dosia (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2227.webp"}, {"name": "Dosia (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2228.webp"}, {"name": "Hobbit | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2229.webp"}, {"name": "Hobbit (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2230.webp"}, {"name": "Hobbit (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2231.webp"}, {"name": "mou | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2232.webp"}, {"name": "mou (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2233.webp"}, {"name": "mou (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2234.webp"}, {"name": "Zeus | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2235.webp"}, {"name": "Zeus (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2236.webp"}, {"name": "Zeus (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2237.webp"}, {"name": "aizy | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2238.webp"}, {"name": "aizy (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2239.webp"}, {"name": "aizy (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2240.webp"}, {"name": "cajunb | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2241.webp"}, {"name": "cajunb (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2242.webp"}, {"name": "cajunb (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2243.webp"}, {"name": "k0nfig | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2244.webp"}, {"name": "k0nfig (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2245.webp"}, {"name": "k0nfig (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2246.webp"}, {"name": "Magisk | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2247.webp"}, {"name": "Magisk (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2248.webp"}, {"name": "Magisk (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2249.webp"}, {"name": "MSL | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2250.webp"}, {"name": "MSL (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2251.webp"}, {"name": "MSL (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2252.webp"}, {"name": "allu | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2253.webp"}, {"name": "allu (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2254.webp"}, {"name": "allu (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2255.webp"}, {"name": "karrigan | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2256.webp"}, {"name": "karrigan (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2257.webp"}, {"name": "karrigan (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2258.webp"}, {"name": "kioShiMa | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2259.webp"}, {"name": "kioShiMa (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2260.webp"}, {"name": "kioShiMa (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2261.webp"}, {"name": "NiKo | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2262.webp"}, {"name": "NiKo (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2263.webp"}, {"name": "NiKo (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2264.webp"}, {"name": "rain | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2265.webp"}, {"name": "rain (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2266.webp"}, {"name": "rain (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2267.webp"}],
    },
    "krakow_2017_challengers_autogr": {
        "name": "Krakow 2017 Challengers Autograph",
        "emoji": "\ud83d\udcab",
        "price": 1.0,
        "image": "assets/containers/2128.webp",
        "stickers": [{"name": "chrisJ | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2268.webp"}, {"name": "chrisJ (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2269.webp"}, {"name": "chrisJ (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2270.webp"}, {"name": "denis | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2271.webp"}, {"name": "denis (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2272.webp"}, {"name": "denis (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2273.webp"}, {"name": "loWel | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2274.webp"}, {"name": "loWel (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2275.webp"}, {"name": "loWel (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2276.webp"}, {"name": "oskar | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2277.webp"}, {"name": "oskar (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2278.webp"}, {"name": "oskar (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2279.webp"}, {"name": "ropz | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2280.webp"}, {"name": "ropz (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2281.webp"}, {"name": "ropz (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2282.webp"}, {"name": "apEX | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2283.webp"}, {"name": "apEX (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2284.webp"}, {"name": "apEX (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2285.webp"}, {"name": "bodyy | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2286.webp"}, {"name": "bodyy (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2287.webp"}, {"name": "bodyy (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2288.webp"}, {"name": "kennyS | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2289.webp"}, {"name": "kennyS (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2290.webp"}, {"name": "kennyS (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2291.webp"}, {"name": "NBK- | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2292.webp"}, {"name": "NBK- (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2293.webp"}, {"name": "NBK- (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2294.webp"}, {"name": "shox | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2295.webp"}, {"name": "shox (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2296.webp"}, {"name": "shox (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2297.webp"}, {"name": "gob b | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2298.webp"}, {"name": "gob b (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2299.webp"}, {"name": "gob b (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2300.webp"}, {"name": "keev | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2301.webp"}, {"name": "keev (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2302.webp"}, {"name": "keev (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2303.webp"}, {"name": "LEGIJA | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2304.webp"}, {"name": "LEGIJA (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2305.webp"}, {"name": "LEGIJA (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2306.webp"}, {"name": "nex | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2307.webp"}, {"name": "nex (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2308.webp"}, {"name": "nex (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2309.webp"}, {"name": "tabseN | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2310.webp"}, {"name": "tabseN (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2311.webp"}, {"name": "tabseN (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2312.webp"}, {"name": "autimatic | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2313.webp"}, {"name": "autimatic (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2314.webp"}, {"name": "autimatic (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2315.webp"}, {"name": "n0thing | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2316.webp"}, {"name": "n0thing (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2317.webp"}, {"name": "n0thing (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2318.webp"}, {"name": "shroud | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2319.webp"}, {"name": "shroud (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2320.webp"}, {"name": "shroud (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2321.webp"}, {"name": "Skadoodle | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2322.webp"}, {"name": "Skadoodle (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2323.webp"}, {"name": "Skadoodle (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2324.webp"}, {"name": "Stewie2K | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2325.webp"}, {"name": "Stewie2K (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2326.webp"}, {"name": "Stewie2K (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2327.webp"}, {"name": "HS | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2328.webp"}, {"name": "HS (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2329.webp"}, {"name": "HS (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2330.webp"}, {"name": "innocent | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2331.webp"}, {"name": "innocent (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2332.webp"}, {"name": "innocent (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2333.webp"}, {"name": "kRYSTAL | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2334.webp"}, {"name": "kRYSTAL (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2335.webp"}, {"name": "kRYSTAL (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2336.webp"}, {"name": "suNny | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2337.webp"}, {"name": "suNny (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2338.webp"}, {"name": "suNny (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2339.webp"}, {"name": "zehN | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2340.webp"}, {"name": "zehN (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2341.webp"}, {"name": "zehN (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2342.webp"}, {"name": "B1ad3 | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2343.webp"}, {"name": "B1ad3 (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2344.webp"}, {"name": "B1ad3 (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2345.webp"}, {"name": "electronic | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2346.webp"}, {"name": "electronic (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2347.webp"}, {"name": "electronic (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2348.webp"}, {"name": "markeloff | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2349.webp"}, {"name": "markeloff (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2350.webp"}, {"name": "markeloff (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2351.webp"}, {"name": "wayLander | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2352.webp"}, {"name": "wayLander (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2353.webp"}, {"name": "wayLander (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2354.webp"}, {"name": "WorldEdit | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2355.webp"}, {"name": "WorldEdit (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2356.webp"}, {"name": "WorldEdit (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2357.webp"}, {"name": "boltz | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2358.webp"}, {"name": "boltz (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2359.webp"}, {"name": "boltz (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2360.webp"}, {"name": "HEN1 | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2361.webp"}, {"name": "HEN1 (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2362.webp"}, {"name": "HEN1 (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2363.webp"}, {"name": "kNgV- | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2364.webp"}, {"name": "kNgV- (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2365.webp"}, {"name": "kNgV- (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2366.webp"}, {"name": "LUCAS1 | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2367.webp"}, {"name": "LUCAS1 (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2368.webp"}, {"name": "LUCAS1 (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2369.webp"}, {"name": "steel | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2370.webp"}, {"name": "steel (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2371.webp"}, {"name": "steel (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2372.webp"}, {"name": "chopper | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2373.webp"}, {"name": "chopper (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2374.webp"}, {"name": "chopper (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2375.webp"}, {"name": "hutji | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2376.webp"}, {"name": "hutji (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2377.webp"}, {"name": "hutji (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2378.webp"}, {"name": "jR | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2379.webp"}, {"name": "jR (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2380.webp"}, {"name": "jR (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2381.webp"}, {"name": "keshandr | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2382.webp"}, {"name": "keshandr (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2383.webp"}, {"name": "keshandr (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2384.webp"}, {"name": "mir | Krakow 2017", "rarity": "💫", "image": "assets/stickers/2385.webp"}, {"name": "mir (Foil) | Krakow 2017", "rarity": "👑 Rare", "image": "assets/stickers/2386.webp"}, {"name": "mir (Gold) | Krakow 2017", "rarity": "👑 Legendary", "image": "assets/stickers/2387.webp"}],
    },
    "cologne_2016_legends_holo_foil": {
        "name": "Cologne 2016 Legends (Holo/Foil)",
        "emoji": "\u2728",
        "price": 2.0,
        "image": "assets/containers/2199.webp",
        "stickers": [{"name": "Ninjas in Pyjamas (Holo) | Cologne 2016", "rarity": "👑 Rare", "image": "assets/stickers/1318.webp"}, {"name": "Ninjas in Pyjamas (Foil) | Cologne 2016", "rarity": "🔥", "image": "assets/stickers/1319.webp"}, {"name": "Counter Logic Gaming (Holo) | Cologne 2016", "rarity": "👑 Rare", "image": "assets/stickers/1326.webp"}, {"name": "Counter Logic Gaming (Foil) | Cologne 2016", "rarity": "🔥", "image": "assets/stickers/1327.webp"}, {"name": "Team Liquid (Holo) | Cologne 2016", "rarity": "👑 Rare", "image": "assets/stickers/1338.webp"}, {"name": "Team Liquid (Foil) | Cologne 2016", "rarity": "🔥", "image": "assets/stickers/1339.webp"}, {"name": "Natus Vincere (Holo) | Cologne 2016", "rarity": "👑 Rare", "image": "assets/stickers/1346.webp"}, {"name": "Natus Vincere (Foil) | Cologne 2016", "rarity": "🔥", "image": "assets/stickers/1347.webp"}, {"name": "Virtus.Pro (Holo) | Cologne 2016", "rarity": "👑 Rare", "image": "assets/stickers/1350.webp"}, {"name": "Virtus.Pro (Foil) | Cologne 2016", "rarity": "🔥", "image": "assets/stickers/1351.webp"}, {"name": "SK Gaming (Holo) | Cologne 2016", "rarity": "👑 Rare", "image": "assets/stickers/1354.webp"}, {"name": "SK Gaming (Foil) | Cologne 2016", "rarity": "🔥", "image": "assets/stickers/1355.webp"}, {"name": "Astralis (Holo) | Cologne 2016", "rarity": "👑 Rare", "image": "assets/stickers/1366.webp"}, {"name": "Astralis (Foil) | Cologne 2016", "rarity": "🔥", "image": "assets/stickers/1367.webp"}, {"name": "Fnatic (Holo) | Cologne 2016", "rarity": "👑 Rare", "image": "assets/stickers/1374.webp"}, {"name": "Fnatic (Foil) | Cologne 2016", "rarity": "🔥", "image": "assets/stickers/1375.webp"}, {"name": "ESL (Holo) | Cologne 2016", "rarity": "👑 Rare", "image": "assets/stickers/1382.webp"}, {"name": "ESL (Foil) | Cologne 2016", "rarity": "🔥", "image": "assets/stickers/1383.webp"}],
    },
    "mlg_columbus_2016_legends_holo": {
        "name": "MLG Columbus 2016 Legends (Holo/Foil)",
        "emoji": "\u2728",
        "price": 2.0,
        "image": "assets/containers/2197.webp",
        "stickers": [{"name": "Ninjas in Pyjamas (Holo) | MLG Columbus 2016", "rarity": "👑 Rare", "image": "assets/stickers/1008.webp"}, {"name": "Ninjas in Pyjamas (Foil) | MLG Columbus 2016", "rarity": "🔥", "image": "assets/stickers/1009.webp"}, {"name": "Natus Vincere (Holo) | MLG Columbus 2016", "rarity": "👑 Rare", "image": "assets/stickers/1036.webp"}, {"name": "Natus Vincere (Foil) | MLG Columbus 2016", "rarity": "🔥", "image": "assets/stickers/1037.webp"}, {"name": "Virtus.Pro (Holo) | MLG Columbus 2016", "rarity": "👑 Rare", "image": "assets/stickers/1040.webp"}, {"name": "Virtus.Pro (Foil) | MLG Columbus 2016", "rarity": "🔥", "image": "assets/stickers/1041.webp"}, {"name": "FaZe Clan (Holo) | MLG Columbus 2016", "rarity": "👑 Rare", "image": "assets/stickers/1052.webp"}, {"name": "FaZe Clan (Foil) | MLG Columbus 2016", "rarity": "🔥", "image": "assets/stickers/1053.webp"}, {"name": "Astralis (Holo) | MLG Columbus 2016", "rarity": "👑 Rare", "image": "assets/stickers/1056.webp"}, {"name": "Astralis (Foil) | MLG Columbus 2016", "rarity": "🔥", "image": "assets/stickers/1057.webp"}, {"name": "Team EnVyUs (Holo) | MLG Columbus 2016", "rarity": "👑 Rare", "image": "assets/stickers/1060.webp"}, {"name": "Team EnVyUs (Foil) | MLG Columbus 2016", "rarity": "🔥", "image": "assets/stickers/1061.webp"}, {"name": "Fnatic (Holo) | MLG Columbus 2016", "rarity": "👑 Rare", "image": "assets/stickers/1064.webp"}, {"name": "Fnatic (Foil) | MLG Columbus 2016", "rarity": "🔥", "image": "assets/stickers/1065.webp"}, {"name": "Luminosity Gaming (Holo) | MLG Columbus 2016", "rarity": "👑 Rare", "image": "assets/stickers/1068.webp"}, {"name": "Luminosity Gaming (Foil) | MLG Columbus 2016", "rarity": "🔥", "image": "assets/stickers/1069.webp"}, {"name": "MLG (Holo) | MLG Columbus 2016", "rarity": "👑 Rare", "image": "assets/stickers/1072.webp"}, {"name": "MLG (Foil) | MLG Columbus 2016", "rarity": "🔥", "image": "assets/stickers/1073.webp"}],
    },
    "budapest_2025_challengers_stic": {
        "name": "Budapest 2025 Challengers Sticker",
        "emoji": "\ud83c\udf1f",
        "price": 1.5,
        "image": "assets/containers/2097.webp",
        "stickers": [{"name": "Aurora | Budapest 2025", "rarity": "💫", "image": "assets/stickers/9521.webp"}, {"name": "Aurora (Embroidered) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/9522.webp"}, {"name": "Aurora (Holo) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/9523.webp"}, {"name": "Aurora (Gold) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9524.webp"}, {"name": "Natus Vincere | Budapest 2025", "rarity": "💫", "image": "assets/stickers/9525.webp"}, {"name": "Natus Vincere (Embroidered) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/9526.webp"}, {"name": "Natus Vincere (Holo) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/9527.webp"}, {"name": "Natus Vincere (Gold) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9528.webp"}, {"name": "Team Liquid | Budapest 2025", "rarity": "💫", "image": "assets/stickers/9529.webp"}, {"name": "Team Liquid (Embroidered) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/9530.webp"}, {"name": "Team Liquid (Holo) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/9531.webp"}, {"name": "Team Liquid (Gold) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9532.webp"}, {"name": "3DMAX | Budapest 2025", "rarity": "💫", "image": "assets/stickers/9533.webp"}, {"name": "3DMAX (Embroidered) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/9534.webp"}, {"name": "3DMAX (Holo) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/9535.webp"}, {"name": "3DMAX (Gold) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9536.webp"}, {"name": "Astralis | Budapest 2025", "rarity": "💫", "image": "assets/stickers/9537.webp"}, {"name": "Astralis (Embroidered) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/9538.webp"}, {"name": "Astralis (Holo) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/9539.webp"}, {"name": "Astralis (Gold) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9540.webp"}, {"name": "TYLOO | Budapest 2025", "rarity": "💫", "image": "assets/stickers/9541.webp"}, {"name": "TYLOO (Embroidered) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/9542.webp"}, {"name": "TYLOO (Holo) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/9543.webp"}, {"name": "TYLOO (Gold) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9544.webp"}, {"name": "MIBR | Budapest 2025", "rarity": "💫", "image": "assets/stickers/9545.webp"}, {"name": "MIBR (Embroidered) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/9546.webp"}, {"name": "MIBR (Holo) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/9547.webp"}, {"name": "MIBR (Gold) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9548.webp"}, {"name": "Passion UA | Budapest 2025", "rarity": "💫", "image": "assets/stickers/9549.webp"}, {"name": "Passion UA (Embroidered) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/9550.webp"}, {"name": "Passion UA (Holo) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/9551.webp"}, {"name": "Passion UA (Gold) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9552.webp"}, {"name": "StarLadder | Budapest 2025", "rarity": "💫", "image": "assets/stickers/9617.webp"}, {"name": "StarLadder (Embroidered) | Budapest 2025", "rarity": "👑 Rare", "image": "assets/stickers/9618.webp"}, {"name": "StarLadder (Holo) | Budapest 2025", "rarity": "🔥", "image": "assets/stickers/9619.webp"}, {"name": "StarLadder (Gold) | Budapest 2025", "rarity": "👑 Legendary", "image": "assets/stickers/9620.webp"}],
    },
    "copenhagen_2024_challengers_st": {
        "name": "Copenhagen 2024 Challengers Sticker",
        "emoji": "\ud83c\udf1f",
        "price": 1.5,
        "image": "assets/containers/2111.webp",
        "stickers": [{"name": "Cloud9 | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7286.webp"}, {"name": "Cloud9 (Glitter) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7287.webp"}, {"name": "Cloud9 (Holo) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7288.webp"}, {"name": "Cloud9 (Gold) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7289.webp"}, {"name": "ENCE | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7290.webp"}, {"name": "ENCE (Glitter) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7291.webp"}, {"name": "ENCE (Holo) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7292.webp"}, {"name": "ENCE (Gold) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7293.webp"}, {"name": "FURIA | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7294.webp"}, {"name": "FURIA (Glitter) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7295.webp"}, {"name": "FURIA (Holo) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7296.webp"}, {"name": "FURIA (Gold) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7297.webp"}, {"name": "Heroic | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7298.webp"}, {"name": "Heroic (Glitter) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7299.webp"}, {"name": "Heroic (Holo) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7300.webp"}, {"name": "Heroic (Gold) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7301.webp"}, {"name": "Eternal Fire | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7302.webp"}, {"name": "Eternal Fire (Glitter) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7303.webp"}, {"name": "Eternal Fire (Holo) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7304.webp"}, {"name": "Eternal Fire (Gold) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7305.webp"}, {"name": "Apeks | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7306.webp"}, {"name": "Apeks (Glitter) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7307.webp"}, {"name": "Apeks (Holo) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7308.webp"}, {"name": "Apeks (Gold) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7309.webp"}, {"name": "GamerLegion | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7310.webp"}, {"name": "GamerLegion (Glitter) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7311.webp"}, {"name": "GamerLegion (Holo) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7312.webp"}, {"name": "GamerLegion (Gold) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7313.webp"}, {"name": "SAW | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7314.webp"}, {"name": "SAW (Glitter) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7315.webp"}, {"name": "SAW (Holo) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7316.webp"}, {"name": "SAW (Gold) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7317.webp"}, {"name": "PGL | Copenhagen 2024", "rarity": "💫", "image": "assets/stickers/7350.webp"}, {"name": "PGL (Glitter) | Copenhagen 2024", "rarity": "👑 Rare", "image": "assets/stickers/7351.webp"}, {"name": "PGL (Holo) | Copenhagen 2024", "rarity": "🔥", "image": "assets/stickers/7352.webp"}, {"name": "PGL (Gold) | Copenhagen 2024", "rarity": "👑 Legendary", "image": "assets/stickers/7353.webp"}],
    },
}

STICKER_VALUES = {
    "⭐": 0.10, "✨": 0.50, "💫": 2.00, "🔥": 10.00,
    "👑 Common": 30, "👑 Rare": 75, "👑 Epic": 150, "👑 Legendary": 300
}

# ============================================================
# EXTRA STICKER CAPSULES — auto-generated from containers.json
# ============================================================
# The 20 capsules above were hand-curated, but containers.json actually has
# 228 real STICKER_CAPSULE-type entries with a matching sticker pool in
# sticker_contents.json -- meaning ~208 real capsules (and the ~1,800+
# sticker images only they use) were sitting completely unreachable in the
# shop. Rather than hand-writing hundreds more dict entries, build them
# programmatically at startup. containers.json has no price field, so price
# is derived from the pool's average per-sticker value (STICKER_VALUES,
# keyed by the same rarity-tier emoji the 20 hand-curated capsules use)
# at ~2.5% of that average -- calibrated to land close to what those 20
# already charge for a similar pool, then clamped/rounded for clean pricing.
STICKER_CONTENTS_JSON_PATH = os.path.join(os.path.dirname(__file__), "sticker_contents.json")
STICKERS_JSON_PATH = os.path.join(os.path.dirname(__file__), "stickers.json")

# stickers.json's own `rarity` field (HIGH_GRADE/REMARKABLE/EXOTIC/
# EXTRAORDINARY/CONTRABAND) maps 1:1 onto the emoji tiers already used by
# the 20 hand-curated capsules -- confirmed by cross-referencing specific
# sticker IDs (e.g. id 9431-9434, the Austin 2025 apEX autograph set).
STICKER_RARITY_TO_EMOJI = {
    "HIGH_GRADE":    "💫",
    "REMARKABLE":    "👑 Rare",
    "EXOTIC":        "🔥",
    "EXTRAORDINARY": "👑 Legendary",
    "CONTRABAND":    "👑 Legendary",   # only 1 sticker total has this rarity
    "DEFAULT":       "💫",             # legacy 2013-era stickers, no tier
}

def _capsule_category(name: str) -> str:
    """Buckets a capsule by its own containers.json name -- no separate
    taxonomy needed, the naming conventions already say what it is."""
    if "Autograph Capsule |" in name:
        return "team_autograph"
    if any(k in name for k in ("Legends", "Challengers", "Champions", "Contenders", "Finalists")):
        return "major_autograph"
    return "retail_community"

def _build_extra_sticker_capsules() -> Dict[str, dict]:
    if not (os.path.exists(CONTAINERS_JSON_PATH) and os.path.exists(STICKER_CONTENTS_JSON_PATH)
            and os.path.exists(STICKERS_JSON_PATH)):
        logger.warning("⚠️ containers.json/sticker_contents.json/stickers.json missing "
                        "— only the 20 hand-curated sticker capsules will be available")
        return {}

    with open(CONTAINERS_JSON_PATH, "r", encoding="utf-8") as f:
        _containers = json.load(f)
    with open(STICKER_CONTENTS_JSON_PATH, "r", encoding="utf-8") as f:
        _contents = json.load(f)
    with open(STICKERS_JSON_PATH, "r", encoding="utf-8") as f:
        _stickers = json.load(f)

    _stickers_by_id = {s["id"]: s for s in _stickers}
    _pool_by_container_id = {c["containerId"]: c.get("stickerIds", []) for c in _contents}
    _existing_images = {c["image"] for c in STICKER_CAPSULES.values()}

    result: Dict[str, dict] = {}
    for c in _containers:
        if c.get("type") != "STICKER_CAPSULE":
            continue
        container_id = c["id"]
        pool_ids = _pool_by_container_id.get(container_id)
        if not pool_ids:
            continue
        image = c.get("containerImage")
        if image in _existing_images:
            continue  # already one of the 20 hand-curated capsules above

        stickers = []
        for sid in pool_ids:
            s = _stickers_by_id.get(sid)
            if not s:
                continue
            rarity_emoji = STICKER_RARITY_TO_EMOJI.get(s.get("rarity", "DEFAULT"), "💫")
            stickers.append({"name": s["name"], "rarity": rarity_emoji, "image": s["stickerImage"]})
        if not stickers:
            continue

        avg_val = sum(STICKER_VALUES.get(s["rarity"], 0.25) for s in stickers) / len(stickers)
        raw_price = min(max(avg_val * 0.025, 0.50), 6.00)
        price = round(raw_price * 4) / 4  # nearest $0.25

        name = c["name"]
        result[f"capsule_{container_id}"] = {
            "name": name,
            "emoji": "🎟️",
            "price": price,
            "image": image,
            "category": _capsule_category(name),
            "stickers": stickers,
        }
    return result

# Tag the 20 hand-curated capsules with a category too, same inference rule,
# so the frontend can treat all sticker capsules uniformly.
for _cid, _cdata in STICKER_CAPSULES.items():
    _cdata.setdefault("category", _capsule_category(_cdata["name"]))

_EXTRA_STICKER_CAPSULES = _build_extra_sticker_capsules()
STICKER_CAPSULES.update(_EXTRA_STICKER_CAPSULES)
logger.info(
    f"✅ Built {len(_EXTRA_STICKER_CAPSULES)} additional sticker capsules from containers.json "
    f"({len(STICKER_CAPSULES)} total sticker capsules available)"
)

# ============================================================
# BOT-SPECIFIC STICKER CAPSULES (simplified for Discord commands)
# ============================================================

CAPSULE_EMOJIS = {
    "recoil": "⭐", "dreams": "🌙⭐", "cs20": "🎂⭐",
    "championship": "🏆", "legends": "👑"
}

BOT_STICKER_CAPSULES = {
    "recoil": {
        "name": "Recoil Sticker Capsule", "emoji": CAPSULE_EMOJIS["recoil"], "price": 0.50,
        "stickers": [
            {"name": "CS2 Logo", "rarity": "⭐"}, {"name": "AWP Sniper", "rarity": "✨"},
            {"name": "Headshot", "rarity": "💫"}, {"name": "Clutch King", "rarity": "🔥"}
        ]
    },
    "dreams": {
        "name": "Dreams Sticker Capsule", "emoji": CAPSULE_EMOJIS["dreams"], "price": 1.00,
        "stickers": [
            {"name": "Phoenix Rising", "rarity": "⭐"}, {"name": "Dragon Lore", "rarity": "✨"},
            {"name": "Royal Crown", "rarity": "👑 Common"}, {"name": "Knight's Oath", "rarity": "👑 Rare"}
        ]
    },
    "cs20": {
        "name": "CS20 Sticker Capsule", "emoji": CAPSULE_EMOJIS["cs20"], "price": 1.00,
        "stickers": [
            {"name": "Counter-Terrorist Elite", "rarity": "⭐"}, {"name": "Terrorist Elite", "rarity": "✨"},
            {"name": "20 Years", "rarity": "💫"}, {"name": "Legends", "rarity": "👑 Epic"}
        ]
    },
    "championship": {
        "name": "Championship Sticker Capsule", "emoji": CAPSULE_EMOJIS["championship"], "price": 2.00,
        "stickers": [
            {"name": "Victory", "rarity": "✨"}, {"name": "Champion", "rarity": "💫"},
            {"name": "Golden Trophy", "rarity": "👑 Epic"}, {"name": "Hall of Fame", "rarity": "👑 Legendary"}
        ]
    },
    "legends": {
        "name": "Legends Sticker Capsule", "emoji": CAPSULE_EMOJIS["legends"], "price": 3.00,
        "stickers": [
            {"name": "s1mple", "rarity": "🔥"}, {"name": "ZyWoo", "rarity": "🔥"},
            {"name": "NiKo", "rarity": "👑 Rare"}, {"name": "KennyS", "rarity": "👑 Epic"}
        ]
    }
}

# ============================================================
# SLOTS DATA
# ============================================================

SLOT_SYMBOLS = [
    {'emoji': '🍒', 'value': 1,  'name': 'Cherry'},
    {'emoji': '🍋', 'value': 2,  'name': 'Lemon'},
    {'emoji': '🍊', 'value': 3,  'name': 'Orange'},
    {'emoji': '🍇', 'value': 4,  'name': 'Grape'},
    {'emoji': '💎', 'value': 10, 'name': 'Diamond'},
    {'emoji': '7️⃣', 'value': 20, 'name': 'Seven'},
    {'emoji': '🎰', 'value': 50, 'name': 'Jackpot'},
]
SLOT_PAYOUTS = {
    '🍒🍒🍒': 3, '🍋🍋🍋': 5, '🍊🍊🍊': 8,
    '🍇🍇🍇': 12, '💎💎💎': 30, '7️⃣7️⃣7️⃣': 60, '🎰🎰🎰': 200,
}

# ============================================================
# QUEST TYPES
# ============================================================

QUEST_TYPES = {
    "open_cases":   {"name": "🔑 Case Opener",   "base_reward": 500,  "base_required": 5},
    "get_golds":    {"name": "✨ Gold Hunter",    "base_reward": 1000, "base_required": 1},
    "earn_money":   {"name": "💰 Money Maker",   "base_reward": 750,  "base_required": 5000},
    "trade_up":     {"name": "🔄 Trade Master",  "base_reward": 800,  "base_required": 3},
    "sell_items":   {"name": "💸 Salesman",       "base_reward": 600,  "base_required": 5},
    "jackpot_win":  {"name": "🎲 Gambler",        "base_reward": 2000, "base_required": 1},
    "daily_streak": {"name": "📅 Streak Keeper", "base_reward": 1000, "base_required": 5},
}

# ============================================================
# GAME CATALOG  (used by games.html hub)
# ============================================================

GAME_CATALOG = {
    "arcade": [
        {"id": "reaction-time",   "name": "Reaction Time",   "emoji": "⚡", "desc": "Click the moment the screen flashes — sub-150ms wins tickets", "url": "/?arcade=reaction", "multiplayer": False},
        {"id": "aim-trainer",     "name": "Aim Trainer",     "emoji": "🎯", "desc": "20 targets, 10 seconds — hit them all for tickets",             "url": "/?arcade=aim",      "multiplayer": False},
        {"id": "bomb-defuse",     "name": "Bomb Defuse",     "emoji": "💣", "desc": "5 wires, one is safe — cut the right one for tickets",         "url": "/?arcade=bomb",     "multiplayer": False},
        {"id": "float-guesser",   "name": "Float Guesser",   "emoji": "🔫", "desc": "Guess a CS2 skin's float value within 0.01 for tickets",       "url": "/?arcade=float",    "multiplayer": False},
        {"id": "memory-sequence", "name": "Memory Sequence", "emoji": "🧠", "desc": "Memorise a number sequence and repeat it perfectly",           "url": "/?arcade=memory",   "multiplayer": False},
    ],
    "easy": [
        {"id": "slots",        "name": "Slots",        "emoji": "🎰", "desc": "Spin the reels, match symbols",         "url": "/games/slots.html",        "multiplayer": False},
        {"id": "slots-cs2",     "name": "CS2 Slots",     "emoji": "🔫", "desc": "Rarity reels, Blue to Gold",           "url": "/games/slots-cs2.html",     "multiplayer": False},
        {"id": "slots-jackpot", "name": "Jackpot Slots", "emoji": "🏆", "desc": "5-reel progressive, every spin feeds the pot", "url": "/games/slots-jackpot.html", "multiplayer": False},
        {"id": "slots-bomb",    "name": "Bomb Slots",    "emoji": "💣", "desc": "CT vs T reels, triple bomb busts your bet",    "url": "/games/slots-bomb.html",    "multiplayer": False},
        {"id": "skin-spin",     "name": "Skin Spin",     "emoji": "💎", "desc": "5 reels, real skins only — always a win, never a whiff", "url": "/games/skin-spin.html", "multiplayer": False},
        {"id": "coinflip",     "name": "Coinflip",     "emoji": "🪙", "desc": "50/50 heads or tails",                  "url": "/games/coinflip.html",     "multiplayer": False},
        {"id": "dice",         "name": "Dice",         "emoji": "🎲", "desc": "Roll the dice, over or under",           "url": "/games/dice.html",         "multiplayer": False},
        {"id": "limbo",        "name": "Limbo",        "emoji": "📉", "desc": "Set a target, beat the multiplier",      "url": "/games/limbo.html",        "multiplayer": False},
        {"id": "hilo",         "name": "Hi-Lo",        "emoji": "🃏", "desc": "Guess higher or lower, chain wins",      "url": "/games/hilo.html",         "multiplayer": False},
        {"id": "dragon-tiger", "name": "Dragon Tiger", "emoji": "🐉", "desc": "Dragon vs Tiger, pick your side",        "url": "/games/dragon-tiger.html", "multiplayer": False},
        {"id": "keno",         "name": "Keno",         "emoji": "🔢", "desc": "Pick your numbers, watch them drop",     "url": "/games/keno.html",         "multiplayer": False},
        {"id": "crash",        "name": "Crash",        "emoji": "🚀", "desc": "Cash out before it crashes — 4 players", "url": "/games/crash.html",        "multiplayer": True},
    ],
    "medium": [
        {"id": "mines",        "name": "Mines",        "emoji": "💣", "desc": "Reveal tiles, avoid the bombs",          "url": "/games/mines.html",        "multiplayer": False},
        {"id": "plinko",       "name": "Plinko",       "emoji": "⚽", "desc": "Drop the ball through the pegs",         "url": "/games/plinko.html",       "multiplayer": False},
        {"id": "tower",        "name": "Tower",        "emoji": "🏗️", "desc": "Climb floors, pick the safe box",        "url": "/games/tower.html",        "multiplayer": False},
        {"id": "shotgun",      "name": "Shotgun",      "emoji": "🔫", "desc": "CS2-themed chamber gamble",              "url": "/games/shotgun.html",      "multiplayer": False},
        {"id": "ladder-climb", "name": "Ladder Climb", "emoji": "🪜", "desc": "Climb higher for bigger rewards",        "url": "/games/ladder-climb.html", "multiplayer": False},
        {"id": "roulette",     "name": "Roulette",     "emoji": "🎡", "desc": "Spin the wheel, place your bets",        "url": "/games/roulette.html",     "multiplayer": False},
    ],
    "hard": [
        {"id": "slide",            "name": "Slide",            "emoji": "🎯", "desc": "Slide into the multiplier zone",         "url": "/games/slide.html",            "multiplayer": False},
        {"id": "mystery-box",      "name": "Mystery Box",      "emoji": "📦", "desc": "CS2 boxes: multipliers or bombs",        "url": "/games/mystery-box.html",      "multiplayer": False},
        {"id": "russian-roulette", "name": "Russian Roulette", "emoji": "🔴", "desc": "You vs an AI with attitude",             "url": "/games/russian-roulette.html", "multiplayer": False},
        {"id": "baccarat",         "name": "Baccarat",         "emoji": "🃏", "desc": "Player vs Banker, closest to 9 wins",    "url": "/games/baccarat.html",         "multiplayer": False},
        {"id": "blackjack",        "name": "Blackjack",        "emoji": "🂡", "desc": "Beat the dealer, hit 21",                "url": "/games/blackjack.html",        "multiplayer": False},
    ],
    "heavy": [
        {"id": "live-race", "name": "Live Race", "emoji": "🏃", "desc": "CS2 agents race — 4 players + bots",   "url": "/games/live-race.html", "multiplayer": True},
        {"id": "battles",   "name": "Case Battles","emoji": "⚔️","desc": "Open cases PvP — 4 players + bots",   "url": "/battle-setup",    "multiplayer": True},
    ],
    "featured": [
        {"id": "poker", "name": "Poker", "emoji": "♠️", "desc": "Texas Hold'em or Video Poker — 4 players", "url": "/games/poker.html", "multiplayer": True},
    ],
    "duels": [
        {"id": "dice-duel", "name": "Dice Duel", "emoji": "🎲", "desc": "1v1 cash roll-off, higher roll wins", "url": "/games/dice-duel.html", "multiplayer": True},
        {"id": "weapon-duel", "name": "CS2 Weapon Duel", "emoji": "🔫", "desc": "1v1 cash roll-off with weapon flavor", "url": "/games/weapon-duel.html", "multiplayer": True},
        {"id": "reaction-duel", "name": "Reaction Duel", "emoji": "⚡", "desc": "1v1 cash duel — fastest click wins", "url": "/games/reaction-duel.html", "multiplayer": True},
        {"id": "case-draft-duel", "name": "Case Draft Duel", "emoji": "🃏", "desc": "Alternate drafting cases, highest total wins the pot", "url": "/games/case-draft-duel.html", "multiplayer": True},
        {"id": "elimination-coinflip", "name": "Elimination Coinflip", "emoji": "👑", "desc": "Self-service 4/8/16-player coinflip bracket", "url": "/tournament", "multiplayer": True},
    ],
    "item_wager": [
        {"id": "item-jackpot", "name": "Item Jackpot", "emoji": "🎁", "desc": "Stake real skins, winner takes the pot", "url": "/games/item-jackpot.html", "multiplayer": True},
        {"id": "item-house-jackpot", "name": "Item vs House", "emoji": "🎲", "desc": "Double or nothing against the house", "url": "/games/item-house-jackpot.html", "multiplayer": False},
        {"id": "item-wager-duel", "name": "Item Wager Duel", "emoji": "⚔️", "desc": "1v1 skin duel, winner takes both items", "url": "/games/item-wager-duel.html", "multiplayer": True},
        {"id": "item-trade-up-duel", "name": "Trade-Up Duel", "emoji": "🔺", "desc": "1v1 same-tier duel, winner gets a next-tier item", "url": "/games/item-trade-up-duel.html", "multiplayer": True},
    ],
    "live_table": [
        {"id": "live-roulette", "name": "Live Roulette", "emoji": "🎡", "desc": "One shared wheel spin decides the whole table", "url": "/games/live-roulette.html", "multiplayer": True},
        {"id": "live-keno", "name": "Live Keno Draw", "emoji": "🎱", "desc": "Private picks, one shared draw for everyone", "url": "/games/live-keno.html", "multiplayer": True},
        {"id": "sync-spin-slots", "name": "Sync-Spin Slots", "emoji": "🎰", "desc": "Everyone's reels spin together, independent results", "url": "/games/sync-spin-slots.html", "multiplayer": True},
        {"id": "live-blackjack", "name": "Live Blackjack", "emoji": "♠️", "desc": "Real multi-seat table — up to 7 players, seat-order turns", "url": "/games/live-blackjack.html", "multiplayer": True},
    ],
    "elimination_race": [
        {"id": "koth-ladder", "name": "King of the Hill Ladder", "emoji": "🪜", "desc": "Solo-style ladder cashout, highest survivor wins a bonus pot", "url": "/games/koth-ladder.html", "multiplayer": True},
        {"id": "ladder-race", "name": "PvP Ladder Race", "emoji": "🏁", "desc": "First to the top rung takes the entire pot, no rake", "url": "/games/ladder-race.html", "multiplayer": True},
        {"id": "battle-royale-mines", "name": "Battle Royale Minefield", "emoji": "💣", "desc": "Solo-style Mines cashout, busted stakes fund a survivor bonus pot", "url": "/games/battle-royale-mines.html", "multiplayer": True},
        {"id": "mines-race", "name": "PvP Mines Race", "emoji": "⛏️", "desc": "First to 5 safe reveals takes the entire pot, no rake", "url": "/games/mines-race.html", "multiplayer": True},
        {"id": "speed-case-race", "name": "Speed Case Race", "emoji": "📦", "desc": "Open cases against the clock, highest total value wins the pot", "url": "/games/speed-case-race.html", "multiplayer": True},
    ],
    "novel": [
        {"id": "live-case-auction", "name": "Live Case Auction", "emoji": "🔨", "desc": "Bid blind on a mystery case — winner pays their bid, keeps whatever it rolls", "url": "/games/live-case-auction.html", "multiplayer": True},
        {"id": "skin-bingo", "name": "Skin Bingo", "emoji": "🎫", "desc": "Shared number draw, first completed line wins the pot", "url": "/games/skin-bingo.html", "multiplayer": True},
    ],
}

# ============================================================
# FLOAT / CONDITION HELPERS
# ============================================================

def generate_skin_float() -> float:
    return round(secure_random(), 4)

def get_skin_condition(float_value: float) -> str:
    if float_value <= 0.07:   return "Factory New"
    elif float_value <= 0.15: return "Minimal Wear"
    elif float_value <= 0.38: return "Field-Tested"
    elif float_value <= 0.45: return "Well-Worn"
    else:                     return "Battle-Scarred"

# ============================================================
# VALUE CALCULATION
# ============================================================

def calculate_item_value(
    rarity: str,
    condition: Optional[str] = None,
    tier: Optional[str] = None,
    is_stattrak: bool = False,
) -> float:
    try:
        if rarity == "Gold":
            base_value = float(GOLD_VALUES.get(tier, 250)) if tier else 250.0
        elif rarity in WEAPON_BASE_VALUES:
            base_value = float(WEAPON_BASE_VALUES[rarity])
        else:
            base_value = 0.25
        multiplier = float(CONDITION_MULTIPLIERS.get(condition or "Field-Tested", 1.0))
        value = base_value * multiplier
        if is_stattrak:
            value *= 2.0
        return round(value, 2)
    except Exception:
        return 0.25


# ============================================================
# ITEM GENERATION
# ============================================================
def get_random_item(case_id: str) -> Optional[Dict]:
    """Roll a random item from a case using the dynamic collection system."""
    case = CASES.get(case_id)
    if not case:
        return None

    collection = case.get('collection')
    if collection and collection in COLLECTION_ITEMS:
        pool = COLLECTION_ITEMS[collection]
        # Group by rarity
        by_rarity = {}
        for item in pool:
            by_rarity.setdefault(item['rarity'], []).append(item)

        # Roll rarity using shared DROP_RATES
        r = secure_random() * 100
        cumulative = 0.0
        chosen_rarity = 'Blue'
        for rarity, chance in DROP_RATES.items():
            cumulative += chance
            if r <= cumulative:
                chosen_rarity = rarity
                break

        # Fallback if chosen rarity empty
        if chosen_rarity not in by_rarity or not by_rarity[chosen_rarity]:
            # Gold (gloves/knives) never belong to a collection — use this
            # case's own real knife/glove set (from container_contents.json)
            # if we have one, else the shared global pool as a last resort.
            case_gold = CASE_GOLD_ITEMS.get(case_id) or GOLD_ITEMS_POOL
            if chosen_rarity == 'Gold' and case_gold:
                by_rarity['Gold'] = case_gold
            else:
                for fallback in ['Blue', 'Purple', 'Pink', 'Red']:
                    if fallback in by_rarity and by_rarity[fallback]:
                        chosen_rarity = fallback
                        break
                else:
                    return None

        skin_template = secure_choice(by_rarity[chosen_rarity])

        # Generate float within skin's allowed range
        float_min = skin_template['float_min']
        float_max = skin_template['float_max']
        if float_max < float_min:
            float_min, float_max = float_max, float_min
        float_value = float_min + secure_random() * (float_max - float_min)

        condition = get_skin_condition(float_value)
        is_stattrak = secure_random() < 0.1
        price = calculate_item_value(chosen_rarity, condition, None, is_stattrak)

        # Full name includes weapon type so image lookup and card display are correct
        # e.g. "XM1014 | Red Python" or "StatTrak™ XM1014 | Red Python"
        full_name = f"{skin_template['weapon_type']} | {skin_template['skin_name']}"
        if is_stattrak:
            name = f"StatTrak™ {full_name}"
        else:
            name = full_name
        display_name = f"{RARITY_EMOJIS.get(chosen_rarity, '')} {name}"

        # Image filename from skin_image
        skin_image = skin_template.get('skin_image')
        image_filename = os.path.basename(skin_image) if skin_image else None

        return {
            'name': name,
            'display_name': display_name,
            'rarity': chosen_rarity,
            'rarity_emoji': RARITY_EMOJIS.get(chosen_rarity, ''),
            'condition': condition,
            'float': float_value,
            'price': price,
            'is_stattrak': is_stattrak,
            'tier': None,
            'weapon_type': skin_template['weapon_type'],
            'skin_name': skin_template['skin_name'],
            'item_id': skin_template['item_id'],
            'image_filename': image_filename,
        }


def get_random_item_by_rarity(case_id: str, target_rarity: str) -> Optional[Dict]:
    """Like get_random_item(), but skips the DROP_RATES rarity roll and rolls
    directly within target_rarity -- used by the Item Trade-Up Duel payout,
    which needs a specific next-tier rarity rather than a natural drop.
    Reuses the same collection/by_rarity lookup and Gold-tier fallback as
    get_random_item() so item generation stays consistent everywhere."""
    case = CASES.get(case_id)
    if not case:
        return None

    collection = case.get('collection')
    if not (collection and collection in COLLECTION_ITEMS):
        return None

    pool = COLLECTION_ITEMS[collection]
    by_rarity = {}
    for item in pool:
        by_rarity.setdefault(item['rarity'], []).append(item)

    chosen_rarity = target_rarity
    if chosen_rarity not in by_rarity or not by_rarity[chosen_rarity]:
        case_gold = CASE_GOLD_ITEMS.get(case_id) or GOLD_ITEMS_POOL
        if chosen_rarity == 'Gold' and case_gold:
            by_rarity['Gold'] = case_gold
        else:
            return None

    skin_template = secure_choice(by_rarity[chosen_rarity])

    float_min = skin_template['float_min']
    float_max = skin_template['float_max']
    if float_max < float_min:
        float_min, float_max = float_max, float_min
    float_value = float_min + secure_random() * (float_max - float_min)

    condition = get_skin_condition(float_value)
    is_stattrak = secure_random() < 0.1
    price = calculate_item_value(chosen_rarity, condition, None, is_stattrak)

    full_name = f"{skin_template['weapon_type']} | {skin_template['skin_name']}"
    name = f"StatTrak™ {full_name}" if is_stattrak else full_name
    display_name = f"{RARITY_EMOJIS.get(chosen_rarity, '')} {name}"

    skin_image = skin_template.get('skin_image')
    image_filename = os.path.basename(skin_image) if skin_image else None

    return {
        'name': name,
        'display_name': display_name,
        'rarity': chosen_rarity,
        'rarity_emoji': RARITY_EMOJIS.get(chosen_rarity, ''),
        'condition': condition,
        'float': float_value,
        'price': price,
        'is_stattrak': is_stattrak,
        'tier': None,
        'weapon_type': skin_template['weapon_type'],
        'skin_name': skin_template['skin_name'],
        'item_id': skin_template['item_id'],
        'image_filename': image_filename,
    }


def get_random_sticker(capsule_id: str) -> Optional[Dict]:
    # Stickers don't have StatTrak variants in real CS2 (only weapons, knives,
    # gloves, and music kits do), so no StatTrak roll here.
    capsule = STICKER_CAPSULES.get(capsule_id)
    if not capsule or not capsule.get('stickers'):
        return None
    sticker = secure_choice(capsule['stickers'])
    value = STICKER_VALUES.get(sticker['rarity'], 0.25)
    name = sticker['name']
    image = sticker.get('image', '')
    return {
        'name':         name,
        'display_name': name,
        'rarity':       sticker['rarity'],
        'price':        round(value, 2),
        'is_stattrak':  False,
        'image':        image,
    }

def get_skin_image_filename(skin_name: str) -> Optional[str]:
    """
    Return the image filename for a skin name string in any stored format:
      - "Redline"                        bare skin name
      - "AK-47 | Redline"               correct CS2 name (new items)
      - "RIFLE | Redline"                old generic category name (legacy DB rows)
      - "StatTrak™ AK-47 | Redline"     StatTrak™ variant
      - "StatTrak™ RIFLE | Redline"      legacy StatTrak™ variant
    """
    if not skin_name:
        return None
    # Strategy 1: direct lookup — covers all pre-indexed key formats
    result = SKIN_NAME_TO_IMAGE.get(skin_name)
    if result:
        return result
    # Strategy 2: strip StatTrak™ prefix and retry
    clean = skin_name.replace("StatTrak™ ", "").strip()
    result = SKIN_NAME_TO_IMAGE.get(clean)
    if result:
        return result
    # Strategy 3: strip leading ★ / ⭐ prefix (gold items) and retry
    import re as _re
    without_star = _re.sub(r'^[★⭐✨💫]\s*', '', clean).strip()
    if without_star != clean:
        result = SKIN_NAME_TO_IMAGE.get(without_star)
        if result:
            return result
    # Strategy 4: extract just the skin name after " | " and look that up
    if " | " in (without_star or clean):
        skin_part = (without_star or clean).split(" | ", 1)[1].strip()
        result = SKIN_NAME_TO_IMAGE.get(skin_part)
        if result:
            return result
    return None

# Build a name → filename lookup for stickers so the inventory API can
# reconstruct image paths for items that predate the image_url column.
def _build_sticker_name_to_image() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for capsule in STICKER_CAPSULES.values():
        for s in capsule.get("stickers", []):
            name = s.get("name", "")
            img  = s.get("image", "")
            if name and img:
                mapping[name]                           = os.path.basename(img)
                # Also index the StatTrak™ variant name
                mapping[f"StatTrak™ {name}"]            = os.path.basename(img)
    return mapping

STICKER_NAME_TO_IMAGE: Dict[str, str] = _build_sticker_name_to_image()

def get_sticker_image(sticker_name: str) -> Optional[str]:
    """
    Return the sticker image filename (e.g. '4513.webp') for a given sticker
    name, or None if not found.  Strips the StatTrak™ prefix before lookup
    so both plain and ST names resolve correctly.
    """
    # Direct lookup first (covers plain names and pre-mapped ST names)
    result = STICKER_NAME_TO_IMAGE.get(sticker_name)
    if result:
        return result
    # Strip StatTrak™ prefix and retry
    clean = sticker_name.replace("StatTrak™ ", "").strip()
    return STICKER_NAME_TO_IMAGE.get(clean)
# ============================================================
# WEAPON IMAGE PATH
# ============================================================

def get_weapon_image_path(item_name: str, weapon_dir: Optional[str] = None) -> str:
    if weapon_dir is None:
        weapon_dir = "static/images/Organized_Weapons_with_Skins"
    fallback = os.path.join("static/images/Default CS2 Weapons", "weapon_ak47.png")
    clean = re.sub(r'StatTrak™\s*|★\s*', '', item_name).strip()
    # Future: walk weapon_dir to find actual file; for now return fallback
    return fallback

# ============================================================
# JSON SERIALISATION HELPER
# ============================================================

from decimal import Decimal
from fastapi.responses import JSONResponse

# ── Standard response helpers (Fix 18) ──────────────────────
def error_response(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "error": message}
    )

def ok_response(data: dict) -> JSONResponse:
    return JSONResponse(content={"success": True, **data})

def convert_decimals(obj: Any) -> Any:
    """Recursively convert Decimal → float for JSON serialisation.
    Also stringifies BIGINT user_id fields to prevent JS integer precision loss.
    Discord IDs are 18-19 digits — beyond JS Number safe range (2^53-1 = 16 digits).
    """
    _BIGINT_KEYS = {
        'user_id', 'admin_id', 'target_id', 'winner_id',
        'player_id', 'discord_id',
    }
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k in _BIGINT_KEYS and isinstance(v, int):
                result[k] = str(v)   # stringify so JS never truncates
            else:
                result[k] = convert_decimals(v)
        return result
    if isinstance(obj, list):
        return [convert_decimals(v) for v in obj]
    return obj

# ============================================================
# HOUSE EDGE CONSTANTS (Fix 20)
# ============================================================

HOUSE_EDGE         = 0.04   # 4% – default for all games
HOUSE_EDGE_POKER   = 0.03   # 3% – slightly lower for poker rake
HOUSE_EDGE_JACKPOT = 0.05   # 5% – jackpot cut

def apply_house_edge(multiplier: float, edge: float = HOUSE_EDGE) -> float:
    """Scale a win multiplier down by the house edge."""
    return round(multiplier * (1.0 - edge), 6)

# ============================================================
# SHARED GAME-ROUTE HELPERS (Fix: de-duplicate clamp_bet /
# apply_house / log_game, previously copy-pasted with subtle
# differences into every games_*.py file)
# ============================================================

def clamp_bet(amount: float, min_bet: float, max_bet: float) -> float:
    return max(min_bet, min(max_bet, float(amount)))

def apply_house(raw: float, edge: float = HOUSE_EDGE) -> float:
    return round(apply_house_edge(raw, edge), 2)

# ── Per-game admin kill switch (game_settings.enabled) ────────
_game_enabled_cache: dict = {}
_game_enabled_cache_at = 0.0
_GAME_ENABLED_TTL = 15   # seconds

async def _refresh_game_enabled_cache():
    global _game_enabled_cache, _game_enabled_cache_at
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT game_name, settings FROM game_settings ORDER BY updated_at ASC NULLS FIRST"
        )
    cache = {}
    for r in rows:
        try:
            cache[r["game_name"]] = json.loads(r["settings"])
        except Exception:
            cache[r["game_name"]] = {}
    _game_enabled_cache = cache
    _game_enabled_cache_at = time.monotonic()

def invalidate_game_enabled_cache():
    global _game_enabled_cache_at
    _game_enabled_cache_at = 0.0

async def require_game_enabled(game_name: str):
    """Raise 503 if an admin has disabled this game via the admin panel."""
    global _game_enabled_cache_at
    if time.monotonic() - _game_enabled_cache_at > _GAME_ENABLED_TTL:
        try:
            await _refresh_game_enabled_cache()
        except Exception as e:
            logger.warning(f"Game-enabled cache refresh failed: {e}")
    settings = _game_enabled_cache.get(game_name, {})
    if settings.get("enabled") == "false":
        raise HTTPException(503, f"{game_name} is currently disabled by an admin")

# ── Case price/featured overrides + fire sale discounts ────────
# admin_update_case_price / admin_toggle_featured write to case_prices,
# and admin_fire_sale writes to fire_sales — this merges both onto the
# static CASES data so admin edits actually take effect at checkout.
_case_override_cache: dict = {}
_case_override_cache_at = 0.0
_CASE_OVERRIDE_TTL = 15   # seconds

async def _refresh_case_override_cache():
    global _case_override_cache, _case_override_cache_at
    pool = await get_db()
    async with pool.acquire() as conn:
        price_rows = await conn.fetch("SELECT id, price, featured FROM case_prices")
        sale_rows  = await conn.fetch(
            "SELECT case_type, discount_percent FROM fire_sales "
            "WHERE expires_at > NOW() ORDER BY created_at DESC"
        )
    overrides = {
        r["id"]: {
            "price":    float(r["price"]) if r["price"] is not None else None,
            "featured": bool(r["featured"]),
        }
        for r in price_rows
    }
    global_discount    = 0.0
    per_case_discount  = {}
    for r in sale_rows:
        pct = float(r["discount_percent"]) / 100.0
        if r["case_type"]:
            per_case_discount.setdefault(r["case_type"], pct)   # rows ordered newest-first
        else:
            global_discount = max(global_discount, pct)
    _case_override_cache = {
        "overrides":          overrides,
        "global_discount":    global_discount,
        "per_case_discount":  per_case_discount,
    }
    _case_override_cache_at = time.monotonic()

def invalidate_case_override_cache():
    global _case_override_cache_at
    _case_override_cache_at = 0.0

async def get_effective_case(case_id: str, base_price: float, base_featured: bool) -> dict:
    """Merge admin price/featured overrides + any active fire sale onto a case.
    Returns {price, featured, original_price, on_sale}."""
    global _case_override_cache_at
    if time.monotonic() - _case_override_cache_at > _CASE_OVERRIDE_TTL:
        try:
            await _refresh_case_override_cache()
        except Exception as e:
            logger.warning(f"Case override cache refresh failed: {e}")
    data      = _case_override_cache or {}
    overrides = data.get("overrides", {})
    ov        = overrides.get(case_id)

    price    = ov["price"] if (ov and ov["price"] is not None) else base_price
    featured = ov["featured"] if ov is not None else base_featured
    original_price = price

    discount = data.get("per_case_discount", {}).get(case_id, data.get("global_discount", 0.0))
    if discount:
        price = round(price * (1 - discount), 2)

    return {
        "price":          price,
        "featured":       featured,
        "original_price": original_price,
        "on_sale":        bool(discount),
    }

# ── Sticker capsule price/featured overrides (mirrors case_prices) ─
_capsule_override_cache: dict = {}
_capsule_override_cache_at = 0.0

async def _refresh_capsule_override_cache():
    global _capsule_override_cache, _capsule_override_cache_at
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, price, featured FROM capsule_prices")
    _capsule_override_cache = {
        r["id"]: {
            "price":    float(r["price"]) if r["price"] is not None else None,
            "featured": bool(r["featured"]),
        }
        for r in rows
    }
    _capsule_override_cache_at = time.monotonic()

def invalidate_capsule_override_cache():
    global _capsule_override_cache_at
    _capsule_override_cache_at = 0.0

async def get_effective_capsule(capsule_id: str, base_price: float, base_featured: bool = False) -> dict:
    """Merge admin price/featured overrides onto a sticker capsule."""
    global _capsule_override_cache_at
    if time.monotonic() - _capsule_override_cache_at > _CASE_OVERRIDE_TTL:
        try:
            await _refresh_capsule_override_cache()
        except Exception as e:
            logger.warning(f"Capsule override cache refresh failed: {e}")
    ov = (_capsule_override_cache or {}).get(capsule_id)
    return {
        "price":    ov["price"] if (ov and ov["price"] is not None) else base_price,
        "featured": ov["featured"] if ov is not None else base_featured,
    }

def fix_surrogate_emoji(s: str) -> str:
    """Some STICKER_CAPSULES emoji were transcribed as two lone UTF-16 surrogate
    code points instead of one combined astral character, which can't be
    UTF-8 encoded and crashes JSON responses. Re-pairing them through a UTF-16
    round trip repairs valid pairs; anything still broken falls back safely."""
    try:
        return s.encode('utf-16', 'surrogatepass').decode('utf-16')
    except Exception:
        return '🧷'

async def log_game(conn, user_id: int, game_type: str, bet: float, win: float,
                    meta: dict = None, win_inclusive: bool = True,
                    update_earn_quest: bool = True):
    """Insert a game_logs row and (optionally) bump the earn_money quest.

    win_inclusive: True means a push (win == bet) counts as 'win' (matches the
    original games_medium/hard/poker behavior); False means push counts as
    'loss' (matches the original games_easy/heavy behavior). Preserved as a
    parameter rather than unified, since changing it would silently alter
    payout/result classification for existing games.
    """
    is_win = (win >= bet) if win_inclusive else (win > bet)
    await conn.execute("""
        INSERT INTO game_logs (user_id, game_type, bet_amount, win_amount,
                               multiplier, result, meta)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
    """, user_id, game_type, bet, win,
        round(win / bet, 4) if bet else 0,
        'win' if is_win else 'loss',
        json.dumps(meta or {}))
    await conn.execute("""
        UPDATE users
        SET total_games_played = total_games_played + 1,
            win_streak = CASE WHEN $2 THEN win_streak + 1 ELSE 0 END
        WHERE user_id = $1
    """, user_id, is_win)
    if update_earn_quest and win > 0:
        await conn.execute("""
            UPDATE quests SET progress = progress + $1
            WHERE user_id=$2 AND quest_type='earn_money' AND completed=FALSE
        """, int(win), user_id)
        await conn.execute("""
            UPDATE quests SET completed=TRUE
            WHERE user_id=$1 AND quest_type='earn_money' AND progress >= required AND completed=FALSE
        """, user_id)

# ============================================================
# CRASH MULTIPLIER MATH
# ============================================================

def generate_crash_point(house_edge: float = 0.04) -> float:
    """
    Generate a provably-fair crash point.
    house_edge of 0.04 means 4% house edge.
    Returns a float >= 1.00.
    """
    r = secure_random()
    if r < house_edge:
        return 1.00  # instant crash
    crash = (1.0 - house_edge) / (1.0 - r)
    return round(max(1.00, crash), 2)

def crash_multiplier_at_second(elapsed: float, speed: float = 0.06) -> float:
    """
    Exponential multiplier growth: starts at 1.00, grows over time.
    elapsed = seconds since round start
    """
    return round(math.e ** (speed * elapsed), 2)

# ============================================================
# USER HELPERS
# ============================================================

async def ensure_user_exists(user_id: int, conn=None) -> None:
    """Create user row if it doesn't exist yet."""
    pool = await get_db()
    if conn:
        await _ensure_user(user_id, conn)
    else:
        async with pool.acquire() as c:
            await _ensure_user(user_id, c)

async def _ensure_user(user_id: int, conn) -> None:
    await conn.execute("""
        INSERT INTO users (user_id, balance, created_at, updated_at)
        VALUES ($1, 1000, NOW(), NOW())
        ON CONFLICT (user_id) DO NOTHING
    """, user_id)

async def get_user_balance(user_id: int) -> float:
    pool = await get_db()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT balance FROM users WHERE user_id = $1", user_id
        )
    return float(val or 0)

async def deduct_balance(user_id: int, amount: float, conn=None) -> bool:
    """Deduct amount from user balance. Returns False if insufficient funds."""
    pool = await get_db()
    async def _do(c):
        result = await c.execute("""
            UPDATE users SET balance = balance - $1, updated_at = NOW()
            WHERE user_id = $2 AND balance >= $1
        """, amount, user_id)
        return result == "UPDATE 1"
    if conn:
        return await _do(conn)
    async with pool.acquire() as c:
        return await _do(c)

async def add_balance(user_id: int, amount: float, conn=None) -> None:
    pool = await get_db()
    async def _do(c):
        await c.execute("""
            UPDATE users SET balance = balance + $1, updated_at = NOW()
            WHERE user_id = $2
        """, amount, user_id)
    if conn:
        await _do(conn)
    else:
        async with pool.acquire() as c:
            await _do(c)

# ============================================================
# VIP HELPERS  (used by all game endpoints for win boost)
# ============================================================

_vip_cache: dict = {}          # user_id -> (expires_at_monotonic, result_dict)
_VIP_CACHE_TTL = 60.0          # seconds — VIP status changes infrequently

def _invalidate_vip_cache(user_id: int) -> None:
    _vip_cache.pop(user_id, None)

async def get_vip_status(user_id: int) -> dict:
    """
    Fast VIP lookup — returns tier info for win boost calculations.
    Returns {'tier': str, 'boost': float, 'active': bool, 'tickets': int}
    Cached per-user for 60 s to avoid opening a second DB connection inside
    an already-active transaction (which would double pool usage under load).
    """
    now = time.monotonic()
    cached = _vip_cache.get(user_id)
    if cached and cached[0] > now:
        return cached[1]

    from datetime import datetime
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT vip_tier, vip_expires_at, tickets FROM users WHERE user_id=$1",
            user_id
        )
    if not row:
        result = {'tier': 'none', 'boost': 1.0, 'active': False, 'tickets': 0}
        _vip_cache[user_id] = (now + _VIP_CACHE_TTL, result)
        return result
    tier    = row['vip_tier'] or 'none'
    expires = row['vip_expires_at']
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    active  = (tier != 'none' and expires is not None and expires > datetime.now(timezone.utc))
    if not active:
        result = {'tier': 'none', 'boost': 1.0, 'active': False,
                  'tickets': int(row['tickets'] or 0)}
        _vip_cache[user_id] = (now + _VIP_CACHE_TTL, result)
        return result
    # Boost values match VIP_TIERS in premium.py
    boosts = {'silver': 1.05, 'gold': 1.10, 'platinum': 1.20}
    result = {
        'tier':    tier,
        'boost':   boosts.get(tier, 1.0),
        'active':  True,
        'tickets': int(row['tickets'] or 0),
    }
    _vip_cache[user_id] = (now + _VIP_CACHE_TTL, result)
    return result

def apply_vip_boost(win: float, vip: dict) -> float:
    """Multiply a win by the user's VIP boost if active. Returns rounded result."""
    if not vip.get('active') or win <= 0:
        return win
    return round(win * vip['boost'], 2)

async def credit_win(user_id: int, win: float, conn) -> float:
    """Apply VIP boost then credit balance. Returns the final (boosted) win."""
    if win <= 0:
        return win
    vip = await get_vip_status(user_id)
    win = apply_vip_boost(win, vip)
    await add_balance(user_id, win, conn)
    return win

# ============================================================
# WEBSOCKET BROADCAST HELPER
# ============================================================

async def broadcast_to_set(ws_set: Set, message: dict) -> Set:
    """
    Broadcast a JSON message to all WebSockets in ws_set.
    Returns a set of dead connections that should be removed.
    Always use try/except — never rely on ws.closed.
    """
    dead = set()
    for ws in ws_set:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    return dead

# ============================================================
# INVENTORY FK MIGRATION HELPER
# ============================================================

async def relax_inventory_fk_to_set_null(conn, table: str, column: str = 'inventory_id') -> None:
    """One-time migration: loosen a NOT NULL + ON DELETE RESTRICT FK on an
    inventory_id column to nullable + ON DELETE SET NULL.

    item_house_wagers, item_trade_up_duel_entries, item_duel_entries, and
    item_jackpot_entries all snapshot the staked item's full display data
    (name/rarity/condition/is_stattrak/float_value/image_url/value) at write
    time -- the FK to inventory(id) is pure traceability, not a live
    dependency. But skin upgrade and trade-up both delete-then-reinsert the
    inventory row, so ON DELETE RESTRICT permanently blocks upgrading/
    trading any item that has ever been staked in a resolved wager/duel/
    jackpot, with a raw DB 500. Safe to relax since nothing reads
    inventory_id back out for display. Idempotent -- cheap no-op once
    already relaxed.
    """
    constraint = f"{table}_{column}_fkey"
    already_nullable = await conn.fetchval("""
        SELECT is_nullable = 'YES' FROM information_schema.columns
        WHERE table_name = $1 AND column_name = $2
    """, table, column)
    delete_rule = await conn.fetchval("""
        SELECT rc.delete_rule FROM information_schema.referential_constraints rc
        WHERE rc.constraint_name = $1
    """, constraint)
    if already_nullable and delete_rule == 'SET NULL':
        return
    await conn.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL")
    await conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}")
    await conn.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT {constraint} "
        f"FOREIGN KEY ({column}) REFERENCES inventory(id) ON DELETE SET NULL"
    )
    logger.info(f"🔧 Relaxed {constraint} to ON DELETE SET NULL")
