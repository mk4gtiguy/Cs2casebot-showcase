# ============================================================
# SERVER.PY — FastAPI Web Server Entrypoint
# CS2CaseBot | Mounts all routers, serves static files
# Run with: uvicorn server:app --host 0.0.0.0 --port 8000
# ============================================================

import os
import json
import asyncio
import html
import secrets
import mimetypes
import asyncpg
from contextlib import asynccontextmanager

# Python's mimetypes module resolves extensions like .webp through the OS's
# own registry/config, which is inconsistent across machines (notably some
# Windows installs have no .webp association at all). When that lookup comes
# back empty, Starlette's FileResponse falls back to serving the file as
# text/plain -- which the X-Content-Type-Options: nosniff header below then
# makes browsers refuse to render as an image. Registering it explicitly
# guarantees correct behavior regardless of the host OS's own MIME config.
# Every skin/sticker/case image in this project is served as .webp, so this
# single line is what keeps all of those actually rendering as images.
mimetypes.add_type("image/webp", ".webp")
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, List

from fastapi import (
    FastAPI, Request, Response, HTTPException,
    Depends, WebSocket, WebSocketDisconnect
)
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx

# ─── Load env ────────────────────────────────────────────────
if os.path.exists('.env'):
    load_dotenv()

DATABASE_URL          = os.getenv('DATABASE_URL', '')
DISCORD_CLIENT_ID     = os.getenv('DISCORD_CLIENT_ID', '')
DISCORD_CLIENT_SECRET = os.getenv('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI  = os.getenv('DISCORD_REDIRECT_URI', 'https://cs2casebot.xyz/auth/discord/callback')
GOOGLE_CLIENT_ID      = os.getenv('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET  = os.getenv('GOOGLE_CLIENT_SECRET', '')
GOOGLE_REDIRECT_URI   = os.getenv('GOOGLE_REDIRECT_URI', 'https://cs2casebot.xyz/auth/google/callback')

# ─── Load admin / moderator IDs from env ─────────────────────
_admin_env = os.getenv('ADMIN_USER_IDS', '')
_mod_env   = os.getenv('MODERATOR_USER_IDS', '')
shared_import_done = False   # populated after shared import below
STRIPE_SECRET_KEY     = os.getenv('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET', '')
KO_FI_URL             = "https://ko-fi.com/mk4gtiguy"
DASHBOARD_URL         = "https://cs2casebot.xyz/"

# ─── Shared imports ──────────────────────────────────────────
import shared
from shared import (
    logger, get_user_id_from_session, require_auth,
    require_admin, require_admin_or_moderator,
    CASES, FEATURED_CASES, STICKER_CAPSULES, RARITY_EMOJIS,
    QUEST_TYPES, GAME_CATALOG,
    get_random_item, get_random_sticker, calculate_item_value,
    generate_skin_float, get_skin_condition,
    ensure_user_exists, get_user_balance, deduct_balance, add_balance,
    convert_decimals, TRADE_UP_PROGRESSION, GOLD_TIER_PROGRESSION,
    STICKER_TRADE_PROGRESSION, GOLD_VALUES, CONDITION_MULTIPLIERS,
    WEAPON_BASE_VALUES, STICKER_VALUES, get_db, init_db, ensure_bot_users,
    SLOT_SYMBOLS, SLOT_PAYOUTS,
    ADMIN_USER_IDS, MODERATOR_USER_IDS, ALL_ITEMS_BY_RARITY,
    check_rate_limit, RATE_CASE, RATE_MARKET, RATE_WRITE,
    get_effective_case, get_effective_capsule, fix_surrogate_emoji,
)

# ─── Populate admin / moderator sets from env ────────────────
if _admin_env:
    for _x in _admin_env.split(','):
        _x = _x.strip()
        if _x:
            try:
                ADMIN_USER_IDS.add(int(_x))
            except ValueError:
                logger.warning(f"Invalid admin user ID in env: {_x}")
if _mod_env:
    for _x in _mod_env.split(','):
        _x = _x.strip()
        if _x:
            try:
                MODERATOR_USER_IDS.add(int(_x))
            except ValueError:
                logger.warning(f"Invalid moderator user ID in env: {_x}")
logger.info(f"👑 Admin IDs loaded: {ADMIN_USER_IDS}")

# ─── Stripe (optional) ───────────────────────────────────────
try:
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    STRIPE_ENABLED = bool(STRIPE_SECRET_KEY)
except ImportError:
    STRIPE_ENABLED = False

# ============================================================
# LIFESPAN  (startup / shutdown)
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──
    logger.info("🚀 CS2CaseBot web server starting...")

    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL not set — DB features will fail")
    else:
        pool = await init_db(DATABASE_URL)
        await _init_all_tables(pool)
        await ensure_bot_users(pool)

        # ── Admin tables ──
        try:
            from routes.admin import init_admin_tables
            await init_admin_tables()
            logger.info("✅ Admin tables ready")
        except Exception as e:
            logger.warning(f"Admin table init skipped: {e}")

        # ── Premium / VIP tables ──
        try:
            from routes.premium import init_premium_tables, daily_ticket_award_loop
            await init_premium_tables()
            logger.info("✅ Premium tables ready")
            asyncio.create_task(daily_ticket_award_loop())
            logger.info("🎟️  Daily ticket award task started")
        except Exception as e:
            logger.warning(f"Premium table init skipped: {e}")

        # ── Tournament tables ──
        try:
            from routes.tournament import init_tournament_tables, recover_stale_tournament_polls
            await init_tournament_tables()
            await recover_stale_tournament_polls()
            logger.info("🏆 Tournament tables ready")
        except Exception as e:
            logger.warning(f"Tournament table init skipped: {e}")

        # ── Ranked mode tables ──
        try:
            from routes.ranked import init_ranked_tables, recover_stale_ranked_matches, recover_stale_ranked_polls
            await init_ranked_tables()
            await recover_stale_ranked_matches()
            await recover_stale_ranked_polls()
            logger.info("🏆 Ranked mode tables ready")
        except Exception as e:
            logger.warning(f"Ranked mode table init skipped: {e}")

    # ── Race tables/index ──
    try:
        from routes.games_heavy import init_race_tables
        await init_race_tables()
        logger.info("🏁 Race tables ready")
    except Exception as e:
        logger.warning(f"Race table init skipped: {e}")

    # ── Battle tables ──
    try:
        from routes.case_battles import init_battle_tables
        await init_battle_tables()
        logger.info("⚔️  Battle tables ready")
    except Exception as e:
        logger.warning(f"Battle table init skipped: {e}")

    # Mount battle matchmaking loop
    try:
        from routes.case_battles import battle_manager, start_matchmaking, recover_stale_case_battles, expire_stale_case_battles_loop
        start_matchmaking()
        await recover_stale_case_battles()
        asyncio.create_task(expire_stale_case_battles_loop())
        logger.info("⚔️  Battle matchmaking started")
    except Exception as e:
        logger.warning(f"Battle module not loaded: {e}")

    # ── Item Jackpot tables ──
    try:
        from routes.item_jackpot import init_jackpot_tables, recover_stale_jackpots, expire_stale_item_jackpots_loop
        await init_jackpot_tables()
        await recover_stale_jackpots()
        asyncio.create_task(expire_stale_item_jackpots_loop())
        logger.info("🎁 Item Jackpot tables ready")
    except Exception as e:
        logger.warning(f"Item Jackpot table init skipped: {e}")

    # ── Item vs House Jackpot table ──
    try:
        from routes.item_house_jackpot import init_house_wager_table
        await init_house_wager_table()
    except Exception as e:
        logger.warning(f"Item House Jackpot table init skipped: {e}")

    # ── Item Wager Duel tables ──
    try:
        from routes.item_wager_duel import init_duel_tables, recover_stale_duels, expire_stale_item_duels_loop
        await init_duel_tables()
        await recover_stale_duels()
        asyncio.create_task(expire_stale_item_duels_loop())
        logger.info("⚔️ Item Wager Duel tables ready")
    except Exception as e:
        logger.warning(f"Item Wager Duel table init skipped: {e}")

    # ── Item Trade-Up Duel tables ──
    try:
        from routes.item_trade_up_duel import init_trade_up_tables, recover_stale_trade_up_duels, expire_stale_trade_up_duels_loop
        await init_trade_up_tables()
        await recover_stale_trade_up_duels()
        asyncio.create_task(expire_stale_trade_up_duels_loop())
        logger.info("🔺 Item Trade-Up Duel tables ready")
    except Exception as e:
        logger.warning(f"Item Trade-Up Duel table init skipped: {e}")

    # ── Dice Duel table ──
    try:
        from routes.dice_duel import init_dice_duel_tables, recover_stale_dice_duels, expire_stale_dice_duels_loop
        await init_dice_duel_tables()
        await recover_stale_dice_duels()
        asyncio.create_task(expire_stale_dice_duels_loop())
    except Exception as e:
        logger.warning(f"Dice Duel table init skipped: {e}")

    # ── Weapon Duel table ──
    try:
        from routes.weapon_duel import init_weapon_duel_tables, recover_stale_weapon_duels, expire_stale_weapon_duels_loop
        await init_weapon_duel_tables()
        await recover_stale_weapon_duels()
        asyncio.create_task(expire_stale_weapon_duels_loop())
    except Exception as e:
        logger.warning(f"Weapon Duel table init skipped: {e}")

    # ── Reaction Duel table ──
    try:
        from routes.reaction_duel import init_reaction_duel_tables, recover_stale_reaction_duels, expire_stale_reaction_duels_loop
        await init_reaction_duel_tables()
        await recover_stale_reaction_duels()
        asyncio.create_task(expire_stale_reaction_duels_loop())
    except Exception as e:
        logger.warning(f"Reaction Duel table init skipped: {e}")

    # ── Case Draft Duel tables ──
    try:
        from routes.case_draft_duel import init_case_draft_duel_tables, recover_stale_case_draft_duels, expire_stale_case_draft_duels_loop
        await init_case_draft_duel_tables()
        await recover_stale_case_draft_duels()
        asyncio.create_task(expire_stale_case_draft_duels_loop())
    except Exception as e:
        logger.warning(f"Case Draft Duel table init skipped: {e}")

    # Keep-alive ping
    asyncio.create_task(_db_keepalive())

    # Fix 9: Start game-session TTL cleanup task
    asyncio.create_task(_cleanup_game_sessions())

    # Refund + expire stale PvP ticket challenges (were previously lost forever)
    try:
        from routes.friends import expire_pvp_challenges_loop, init_friend_challenges_tables
        await init_friend_challenges_tables()
        asyncio.create_task(expire_pvp_challenges_loop())
        logger.info("⚔️  PvP challenge expiry/refund task started")
    except Exception as e:
        logger.warning(f"PvP challenge expiry task not started: {e}")

    # ── Sync-Spin Slots tables ──
    try:
        from routes.sync_slots import init_sync_slots_tables, recover_stale_sync_slots_rounds
        await init_sync_slots_tables()
        await recover_stale_sync_slots_rounds()
    except Exception as e:
        logger.warning(f"Sync-Spin Slots table init skipped: {e}")

    # ── Live Roulette tables ──
    try:
        from routes.live_roulette import init_live_roulette_tables, recover_stale_live_roulette_rounds
        await init_live_roulette_tables()
        await recover_stale_live_roulette_rounds()
    except Exception as e:
        logger.warning(f"Live Roulette table init skipped: {e}")

    # ── Live Keno Draw tables ──
    try:
        from routes.live_keno import init_live_keno_tables, recover_stale_live_keno_rounds
        await init_live_keno_tables()
        await recover_stale_live_keno_rounds()
    except Exception as e:
        logger.warning(f"Live Keno Draw table init skipped: {e}")

    # ── Live Blackjack tables ──
    try:
        from routes.live_blackjack import init_live_blackjack_tables, recover_stale_live_blackjack_tables
        await init_live_blackjack_tables()
        await recover_stale_live_blackjack_tables()
    except Exception as e:
        logger.warning(f"Live Blackjack table init skipped: {e}")

    # ── King of the Hill Ladder tables ──
    try:
        from routes.koth_ladder import init_koth_ladder_tables, recover_stale_koth_ladder_rounds
        await init_koth_ladder_tables()
        await recover_stale_koth_ladder_rounds()
    except Exception as e:
        logger.warning(f"KOTH Ladder table init skipped: {e}")

    # ── PvP Ladder Race tables ──
    try:
        from routes.ladder_race import init_ladder_race_tables, recover_stale_ladder_race_rounds
        await init_ladder_race_tables()
        await recover_stale_ladder_race_rounds()
    except Exception as e:
        logger.warning(f"Ladder Race table init skipped: {e}")

    # ── Battle Royale Minefield tables ──
    try:
        from routes.battle_royale_mines import init_battle_royale_mines_tables, recover_stale_battle_royale_mines_rounds
        await init_battle_royale_mines_tables()
        await recover_stale_battle_royale_mines_rounds()
    except Exception as e:
        logger.warning(f"Battle Royale Minefield table init skipped: {e}")

    # ── PvP Mines Race tables ──
    try:
        from routes.mines_race import init_mines_race_tables, recover_stale_mines_race_rounds
        await init_mines_race_tables()
        await recover_stale_mines_race_rounds()
    except Exception as e:
        logger.warning(f"Mines Race table init skipped: {e}")

    # ── Speed Case Race tables ──
    try:
        from routes.speed_case_race import init_speed_case_race_tables, recover_stale_speed_case_race_rounds
        await init_speed_case_race_tables()
        await recover_stale_speed_case_race_rounds()
    except Exception as e:
        logger.warning(f"Speed Case Race table init skipped: {e}")

    # ── Skin Bingo tables ──
    try:
        from routes.skin_bingo import init_skin_bingo_tables, recover_stale_skin_bingo_rounds
        await init_skin_bingo_tables()
        await recover_stale_skin_bingo_rounds()
    except Exception as e:
        logger.warning(f"Skin Bingo table init skipped: {e}")

    # ── Live Case Auction tables ──
    try:
        from routes.live_case_auction import init_live_case_auction_tables, recover_stale_case_auction_rounds
        await init_live_case_auction_tables()
        await recover_stale_case_auction_rounds()
    except Exception as e:
        logger.warning(f"Live Case Auction table init skipped: {e}")

    logger.info("✅ Server ready!")
    yield

    # ── Shutdown ──
    logger.info("🛑 Server shutting down...")
    try:
        from routes.case_battles import shutdown_matchmaking
        shutdown_matchmaking()
    except Exception:
        pass
    if shared.db_pool:
        await shared.db_pool.close()

# ─── Fix 17: Per-user rate limiting middleware ───
from starlette.middleware.base import BaseHTTPMiddleware
import time as _time

class PerUserRateLimitMiddleware(BaseHTTPMiddleware):
    _EVICT_INTERVAL = 300  # evict idle entries every 5 minutes

    def __init__(self, app, requests_per_second: int = 10):
        super().__init__(app)
        self._rps = requests_per_second
        self._user_windows: dict = {}
        self._last_evict = _time.monotonic()
        self._lock = asyncio.Lock()

    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/api/games/"):
            session_token = request.cookies.get("session_token")
            session = shared.get_session(session_token or "") or {}
            uid = session.get("user_id")
            rate_key = str(uid) if uid else (request.client.host if request.client else "anon")
            now = _time.monotonic()
            async with self._lock:
                window_start, count = self._user_windows.get(rate_key, (now, 0))
                if now - window_start > 1.0:
                    self._user_windows[rate_key] = (now, 1)
                elif count >= self._rps:
                    from fastapi.responses import JSONResponse
                    return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
                else:
                    self._user_windows[rate_key] = (window_start, count + 1)
                if now - self._last_evict > self._EVICT_INTERVAL:
                    self._last_evict = now
                cutoff = now - 60.0
                self._user_windows = {
                    k: v for k, v in self._user_windows.items()
                    if v[0] >= cutoff
                }
        return await call_next(request)
# ─── CSRF protection middleware ──────────────────────────────

CSRF_EXEMPT_PATHS = {
    "/webhook/stripe",
    "/auth/discord/callback", "/auth/callback",
    "/auth/google/callback",
    "/api/csrf-token", "/health", "/api/ping",
}

class CsrfProtectMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF protection for POST/PUT/DELETE."""

    async def dispatch(self, request, call_next):
        if request.method in ("POST", "PUT", "DELETE"):
            path = request.url.path.rstrip("/")
            if path not in CSRF_EXEMPT_PATHS and not path.startswith("/webhook/"):
                csrf_cookie = request.cookies.get("csrf_token", "")
                csrf_header = request.headers.get("X-CSRF-Token", "")
                if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
                    return JSONResponse(
                        {"error": "CSRF validation failed", "detail": "Missing or invalid CSRF token"},
                        status_code=403,
                    )
        return await call_next(request)

# ─── Maintenance mode gate ────────────────────────────────────

MAINTENANCE_EXEMPT_PREFIXES = (
    "/api/admin", "/static/", "/auth/",
    "/favicon.ico", "/manifest.json", "/sw.js",
    "/api/csrf-token", "/health", "/api/ping",
)

def _maintenance_page_html(message: str) -> str:
    safe_message = html.escape(message or "We'll be back soon!")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Under Maintenance — CS2CaseBot</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#0d0f14; color:#e8e8e8; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         text-align:center; padding:20px; box-sizing:border-box; }}
  .box {{ max-width:420px; }}
  .icon {{ font-size:56px; margin-bottom:16px; }}
  h1 {{ font-size:22px; margin:0 0 12px; color:#f0b90b; }}
  p {{ font-size:15px; line-height:1.5; color:#b7b7b7; margin:0; }}
</style></head>
<body><div class="box">
  <div class="icon">🛠️</div>
  <h1>Under Maintenance</h1>
  <p>{safe_message}</p>
</div></body></html>"""

class MaintenanceModeMiddleware(BaseHTTPMiddleware):
    """Blocks all non-exempt requests while maintenance mode is on.
    Admins (by session) always pass through so they can keep working
    on the live site without needing terminal/server access."""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.rstrip("/") in MAINTENANCE_EXEMPT_PREFIXES or \
           any(path.startswith(p) for p in MAINTENANCE_EXEMPT_PREFIXES):
            return await call_next(request)

        uid = await get_user_id_from_session(request)
        if uid in ADMIN_USER_IDS:
            return await call_next(request)

        try:
            from routes.admin import get_settings
            pool = await get_db()
            settings = await get_settings(pool)
        except Exception:
            settings = {}

        if settings.get("maintenance_mode") == "true":
            message = settings.get("maintenance_message", "We'll be back soon!")
            if path.startswith("/api/"):
                return JSONResponse(
                    {"error": "maintenance", "message": message}, status_code=503
                )
            return HTMLResponse(_maintenance_page_html(message), status_code=503)

        return await call_next(request)

# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="CS2CaseBot API",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail, "status": exc.status_code})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": "Validation error", "detail": exc.errors()})

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.url}: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://cs2casebot.xyz"],
    allow_origin_regex=r"https://[a-z0-9]+\.discordsays\.com",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Fix 17: Per-user rate limiting on all /api/games/* endpoints
app.add_middleware(PerUserRateLimitMiddleware, requests_per_second=10)

# CSRF protection for all state-changing endpoints
app.add_middleware(CsrfProtectMiddleware)

# Maintenance mode gate — blocks the whole site except for admins/exempt paths
app.add_middleware(MaintenanceModeMiddleware)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # JS/CSS under /static/ are actively edited during development -- don't
    # let a CDN in front of the tunnel (or the browser) cache them, or code
    # changes silently stop showing up for anyone behind that layer.
    if request.url.path.startswith("/static/") and request.url.path.endswith((".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://js.stripe.com https://static.cloudflareinsights.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https://cdn.discordapp.com https://community.fastly.steamstatic.com https://*.steamstatic.com; "
        "connect-src 'self' https://js.stripe.com https://static.cloudflareinsights.com https://cloudflareinsights.com https://discord.com wss://discord.com https://cdn.discordapp.com https://fonts.googleapis.com https://fonts.gstatic.com; "
        "frame-src https://js.stripe.com; "
        "frame-ancestors 'self' https://*.discord.com https://*.discordsays.com; "
        "object-src 'none';"
    )
    return response

# ============================================================
# MOUNT GAME ROUTERS
# ============================================================

def _safe_include(module_path: str, attr: str = "router"):
    """Include a router, warn but don't crash if the module isn't written yet."""
    try:
        import importlib
        mod = importlib.import_module(module_path)
        app.include_router(getattr(mod, attr))
        logger.info(f"✅ Router mounted: {module_path}")
    except ModuleNotFoundError:
        logger.warning(f"⏳ Router not yet written, skipping: {module_path}")
    except Exception as e:
        logger.error(f"❌ Failed to mount {module_path}: {e}")

_safe_include("routes.case_battles")
_safe_include("routes.item_jackpot")
_safe_include("routes.item_house_jackpot")
_safe_include("routes.item_wager_duel")
_safe_include("routes.item_trade_up_duel")
_safe_include("routes.dice_duel")
_safe_include("routes.weapon_duel")
_safe_include("routes.reaction_duel")
_safe_include("routes.case_draft_duel")
_safe_include("routes.sync_slots")
_safe_include("routes.live_roulette")
_safe_include("routes.live_keno")
_safe_include("routes.live_blackjack")
_safe_include("routes.koth_ladder")
_safe_include("routes.ladder_race")
_safe_include("routes.battle_royale_mines")
_safe_include("routes.mines_race")
_safe_include("routes.speed_case_race")
_safe_include("routes.skin_bingo")
_safe_include("routes.live_case_auction")
_safe_include("routes.games_easy")
_safe_include("routes.games_medium")
_safe_include("routes.games_hard")
_safe_include("routes.games_heavy")
_safe_include("routes.games_poker")
_safe_include("routes.admin")
_safe_include("routes.premium")
_safe_include("routes.referral")
_safe_include("routes.friends")
_safe_include("routes.loadouts")
_safe_include("routes.ticket_games")
_safe_include("routes.market")
_safe_include("routes.tournament")
_safe_include("routes.ranked")
_safe_include("routes.chat")
_safe_include("routes.site")

@app.get("/admin", include_in_schema=False)
async def page_admin(request: Request):
    user_id = await get_user_id_from_session(request)
    if not user_id or user_id not in ADMIN_USER_IDS:
        return RedirectResponse("/")
    return _html("static/admin.html")

# ============================================================
# DISCORD ACTIVITY ENDPOINTS
# ============================================================

@app.get("/api/discord-activity/config")
async def discord_activity_config():
    """Return the Discord client ID needed by the Embedded App SDK."""
    return {"client_id": DISCORD_CLIENT_ID}


@app.post("/api/discord-activity/token")
async def discord_activity_token(request: Request):
    """Exchange a Discord OAuth2 code (from the SDK) for an activity session cookie."""
    body = await request.json()
    code = body.get("code")
    if not code:
        raise HTTPException(400, "Missing code")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id":     DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                # No redirect_uri — the Embedded App SDK handles auth internally
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_resp.status_code != 200:
            logger.error(f"Discord Activity token exchange failed: {token_resp.status_code} {token_resp.text}")
            raise HTTPException(400, "Failed to exchange Discord code")
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(400, "No access token from Discord")

        user_resp = await client.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            raise HTTPException(400, "Failed to fetch Discord user")
        discord_user = user_resp.json()

    discord_id  = int(discord_user["id"])
    username    = discord_user.get("global_name") or discord_user.get("username", f"User{discord_id}")
    avatar_hash = discord_user.get("avatar")
    avatar_url  = (
        f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png"
        if avatar_hash else
        f"https://cdn.discordapp.com/embed/avatars/{discord_id % 5}.png"
    )

    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, balance, avatar_url, primary_provider, created_at, updated_at)
            VALUES ($1, $2, 1000, $3, 'discord', NOW(), NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET username=$2, avatar_url=COALESCE($3, users.avatar_url), updated_at=NOW()
        """, discord_id, username, avatar_url)

    session_token = secrets.token_urlsafe(32)
    shared.create_session(session_token, {
        "user_id":          discord_id,
        "username":         username,
        "avatar":           avatar_hash,
        "avatar_url":       avatar_url,
        "primary_provider": "discord",
        "created_at":       datetime.now(timezone.utc).isoformat(),
    })

    # Return token in body — Discord's proxy may strip Set-Cookie headers,
    # so the frontend uses X-Activity-Token header auth instead of cookies.
    resp = JSONResponse({"success": True, "username": username, "session_token": session_token})
    resp.set_cookie(
        "activity_session",
        session_token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=86400 * 7,
    )
    return resp

# ============================================================
# STATIC FILES
# ============================================================

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# ============================================================
# PAGE ROUTES  (serve HTML files)
# ============================================================

def _html(path: str) -> HTMLResponse:
    # These pages are actively edited during development -- never let a CDN
    # (e.g. Cloudflare in front of the tunnel) or the browser cache them, or
    # code changes silently stop showing up for anyone behind that layer.
    no_cache_headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return HTMLResponse(f.read(), media_type="text/html; charset=utf-8", headers=no_cache_headers)
        except (UnicodeDecodeError, UnicodeError):
            return HTMLResponse("<h1>Encoding error</h1>", status_code=500, headers=no_cache_headers)
    return HTMLResponse("<h1>Page not found</h1>", status_code=404, headers=no_cache_headers)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("static/icons/icon-192.png", media_type="image/png")

@app.get("/manifest.json", include_in_schema=False)
async def pwa_manifest():
    return FileResponse("static/manifest.json", media_type="application/manifest+json")

@app.get("/sw.js", include_in_schema=False)
async def pwa_service_worker():
    resp = FileResponse("static/sw.js", media_type="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    # The service worker script itself must never be served from HTTP cache --
    # otherwise browsers can keep running a stale worker indefinitely even
    # after the file changes on disk, since they never notice the update.
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/",              include_in_schema=False)
async def page_index():        return _html("static/index.html")

@app.get("/tickets/success", include_in_schema=False)
async def page_tickets_success():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/?payment=tickets", status_code=302)

@app.get("/vip/success", include_in_schema=False)
async def page_vip_success():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/?payment=vip", status_code=302)

@app.get("/battle",        include_in_schema=False)
async def page_battle():       return _html("static/battle.html")

@app.get("/battle-setup",  include_in_schema=False)
async def page_battle_setup(): return _html("static/battle-setup.html")

@app.get("/games",         include_in_schema=False)
async def page_games():        return _html("static/games.html")

@app.get("/market",        include_in_schema=False)
async def page_market():       return _html("static/market.html")

@app.get("/tournament",    include_in_schema=False)
async def page_tournament():   return _html("static/tournament.html")

@app.get("/leaderboard",   include_in_schema=False)
async def page_leaderboard():  return _html("static/leaderboard.html")

@app.get("/ranked",        include_in_schema=False)
async def page_ranked():       return _html("static/ranked.html")

@app.get("/terms",         include_in_schema=False)
async def page_terms():        return _html("static/terms.html")

@app.get("/privacy",       include_in_schema=False)
async def page_privacy():      return _html("static/privacy.html")

# Individual game pages
_GAME_PAGES = [
    "slots", "slots-cs2", "slots-jackpot", "slots-bomb", "skin-spin",
    "coinflip", "dice", "mines", "crash", "limbo", "hilo",
    "dragon-tiger", "keno", "plinko", "tower", "shotgun", "ladder-climb",
    "roulette", "slide", "mystery-box", "russian-roulette",
    "baccarat", "blackjack", "live-race", "poker",
]

for _game in _GAME_PAGES:
    # capture in closure
    def _make_handler(g):
        async def handler():
            return _html(f"static/games/{g}.html")
        handler.__name__ = f"page_game_{g.replace('-','_')}"
        return handler
    app.get(f"/games/{_game}", include_in_schema=False)(_make_handler(_game))

# ─── Also serve game pages with .html extension ──────────────
@app.get("/games/{game_name}.html", include_in_schema=False)
async def serve_game_with_ext(game_name: str):
    # Bug 181 fix: strip any directory components from game_name to prevent path
    # traversal attacks (e.g. "../admin" → "admin.html" serving admin panel to anyone).
    safe_name = os.path.basename(game_name)
    return _html(f"static/games/{safe_name}.html")

def _safe_static_path(base_dir: str, filename: str) -> str | None:
    """Return resolved path only if it stays within base_dir; else None."""
    base = os.path.realpath(base_dir)
    candidate = os.path.realpath(os.path.join(base_dir, filename))
    return candidate if candidate.startswith(base + os.sep) or candidate == base else None

@app.get("/static/images/containers/{filename}.png")
async def serve_container_image(filename: str):
    # Try .png first (if any), then .webp
    for ext in ('.png', '.webp'):
        path = _safe_static_path("static/images/containers", filename + ext)
        if path and os.path.exists(path):
            return FileResponse(path)
    # Fallback to a default image
    default = "static/images/containers/default.png"
    if os.path.exists(default):
        return FileResponse(default)
    # If nothing, return a tiny placeholder
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="#ccc"/><text x="12" y="40" font-size="20" fill="#333">?</text></svg>'''
    from fastapi.responses import Response
    return Response(content=svg, media_type="image/svg+xml")

@app.get("/static/images/stickers/{filename}")
async def serve_sticker_image(filename: str):
    path = _safe_static_path("static/images/stickers", filename)
    if path and os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(404, "Sticker image not found")
# ============================================================
# DATABASE TABLE INIT
# ============================================================

async def _init_all_tables(pool):
    async with pool.acquire() as conn:
        # Users
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id          BIGINT PRIMARY KEY,
                username         TEXT,
                balance          DECIMAL(15,2) DEFAULT 1000,
                credits          INTEGER DEFAULT 0,
                tickets          INTEGER DEFAULT 0,
                total_opens      INTEGER DEFAULT 0,
                total_premium_opens INTEGER DEFAULT 0,
                total_golds      INTEGER DEFAULT 0,
                total_trades     INTEGER DEFAULT 0,
                total_games_played INTEGER DEFAULT 0,
                win_streak       INTEGER DEFAULT 0,
                coinflip_wins    INTEGER DEFAULT 0,
                dice_wins        INTEGER DEFAULT 0,
                mines_wins       INTEGER DEFAULT 0,
                slots_wins       INTEGER DEFAULT 0,
                daily_streak     INTEGER DEFAULT 0,
                last_daily       TIMESTAMP,
                last_hourly      TIMESTAMP,
                last_weekly      TIMESTAMP,
                total_hourly_claimed INTEGER DEFAULT 0,
                total_weekly_claimed INTEGER DEFAULT 0,
                xp               INTEGER DEFAULT 0,
                level            INTEGER DEFAULT 1,
                prestige         INTEGER DEFAULT 0,
                created_at       TIMESTAMP DEFAULT NOW(),
                updated_at       TIMESTAMP DEFAULT NOW(),
                is_banned        BOOLEAN DEFAULT FALSE,
                ban_reason       TEXT,
                ban_expires      TIMESTAMP,
                avatar_url       TEXT
            )
        """)
        # Ensure ban columns exist on older DBs (ALTER TABLE IF NOT EXISTS col)
        for col_sql in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_reason TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_expires TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT",
            # settings and tickets columns
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS settings JSONB DEFAULT '{}'::jsonb",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS tickets INTEGER DEFAULT 0",
            # achievement/stat tracking columns
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_spent DECIMAL(15,2) DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS jackpot_wins INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_stickers INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_inventory_items INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_quests_completed INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_premium_opens INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_games_played INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS win_streak INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS coinflip_wins INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS dice_wins INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS mines_wins INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS slots_wins INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_hourly_claimed INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_weekly_claimed INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS xp INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS level INTEGER DEFAULT 1",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS prestige INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_email TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_avatar_url TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS primary_provider TEXT DEFAULT 'discord'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_agent_egg_claim TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS agent_phrases_seen INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS login_count INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                await conn.execute(col_sql)
            except asyncpg.exceptions.DuplicateColumnError:
                pass
            except Exception as e:
                shared.logger.error(f"Migration (users) failed: {e}")
                raise
        for idx_sql in [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code ON users(referral_code) WHERE referral_code IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id) WHERE google_id IS NOT NULL",
        ]:
            try:
                await conn.execute(idx_sql)
            except asyncpg.exceptions.DuplicateTableError:
                pass
            except Exception as e:
                shared.logger.error(f"Migration (index) failed: {e}")
                raise
        # Sequence used to generate user_ids for Google-first accounts
        try:
            await conn.execute("CREATE SEQUENCE IF NOT EXISTS google_user_id_seq START WITH 1000000 INCREMENT BY 1")
        except asyncpg.exceptions.DuplicateTableError:
            pass
        except Exception as e:
            shared.logger.error(f"Migration (sequence) failed: {e}")
            raise
        # Inventory
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                item_name   TEXT NOT NULL,
                item_type   TEXT DEFAULT 'weapon',
                rarity      TEXT,
                price       DECIMAL(15,2),
                condition   TEXT,
                is_stattrak BOOLEAN DEFAULT FALSE,
                status      TEXT DEFAULT 'kept',
                case_id     TEXT,
                float_value DECIMAL(10,4),
                image_url   TEXT,
                tier        TEXT,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        # Migrate: add columns to existing inventory tables
        for col_def in [
            "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS image_url TEXT",
            "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS tier TEXT",
            "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS source TEXT",
            "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS acquired_at TIMESTAMPTZ DEFAULT NOW()",
            "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS applied_stickers JSONB DEFAULT '[]'::jsonb",
            "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS in_loadout BOOLEAN DEFAULT FALSE",
            "ALTER TABLE inventory ADD COLUMN IF NOT EXISTS protected BOOLEAN NOT NULL DEFAULT FALSE",
        ]:
            try:
                await conn.execute(col_def)
            except asyncpg.exceptions.DuplicateColumnError:
                pass
            except Exception as e:
                shared.logger.error(f"Migration (inventory) failed: {e}")
                raise

        # Loadouts -- multiple named showcases, replacing the single
        # inventory.in_loadout boolean. That column is kept, but its meaning
        # narrows to "is this item in the user's currently ACTIVE loadout" --
        # a cache flag kept in sync by routes/loadouts.py so every existing
        # reader (GET /api/loadout, friends' loadout view, inventory list,
        # item-picker exclusions) keeps working unmodified.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS loadouts (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                name       TEXT NOT NULL,
                is_active  BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS loadout_items (
                loadout_id   INTEGER NOT NULL REFERENCES loadouts(id) ON DELETE CASCADE,
                inventory_id INTEGER NOT NULL REFERENCES inventory(id) ON DELETE CASCADE,
                added_at     TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (loadout_id, inventory_id)
            )
        """)
        try:
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_loadouts_user_active ON loadouts(user_id) WHERE is_active = TRUE"
            )
        except asyncpg.exceptions.DuplicateTableError:
            pass
        except Exception as e:
            shared.logger.warning(f"Migration (loadouts active index) skipped: {e}")
        try:
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_loadout_items_inventory ON loadout_items(inventory_id)"
            )
        except asyncpg.exceptions.DuplicateTableError:
            pass
        except Exception as e:
            shared.logger.warning(f"Migration (loadout_items index) skipped: {e}")

        # One-time backfill: give every user who already had in_loadout=TRUE
        # items a real "My Loadout" row (marked active) containing them, so
        # nobody's existing showcase silently disappears when this ships.
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS migration_flags (
                    name TEXT PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT NOW()
                )
            """)
            flag = "loadouts_backfill_from_in_loadout"
            already_ran = await conn.fetchval(
                "SELECT 1 FROM migration_flags WHERE name = $1", flag
            )
            if not already_ran:
                user_rows = await conn.fetch(
                    "SELECT DISTINCT user_id FROM inventory WHERE in_loadout = TRUE"
                )
                for row in user_rows:
                    uid = row["user_id"]
                    loadout_id = await conn.fetchval(
                        "INSERT INTO loadouts (user_id, name, is_active) VALUES ($1, 'My Loadout', TRUE) RETURNING id",
                        uid
                    )
                    await conn.execute("""
                        INSERT INTO loadout_items (loadout_id, inventory_id)
                        SELECT $1, id FROM inventory WHERE user_id=$2 AND in_loadout=TRUE
                        ON CONFLICT DO NOTHING
                    """, loadout_id, uid)
                await conn.execute(
                    "INSERT INTO migration_flags (name) VALUES ($1) ON CONFLICT DO NOTHING", flag
                )
                shared.logger.info(f"Loadouts backfill: created default loadout for {len(user_rows)} users")
        except Exception as e:
            shared.logger.warning(f"Migration (loadouts backfill) skipped: {e}")

        # Friends & PvP challenges
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS friendships (
                id           SERIAL PRIMARY KEY,
                requester_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                addressee_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                status       TEXT DEFAULT 'pending',
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                updated_at   TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(requester_id, addressee_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pvp_challenges (
                id             SERIAL PRIMARY KEY,
                challenger_id  BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                challenged_id  BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                bet_tickets    INT DEFAULT 1,
                status         TEXT DEFAULT 'pending',
                winner_id      BIGINT,
                created_at     TIMESTAMPTZ DEFAULT NOW(),
                completed_at   TIMESTAMPTZ,
                expires_at     TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '15 minutes')
            )
        """)
        # Ticket arcade game sessions
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ticket_games (
                id            SERIAL PRIMARY KEY,
                user_id       BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                game_type     TEXT NOT NULL,
                session_token TEXT UNIQUE NOT NULL,
                game_data     JSONB DEFAULT '{}',
                started_at    TIMESTAMPTZ DEFAULT NOW(),
                completed_at  TIMESTAMPTZ,
                score         FLOAT,
                tickets_won   INT DEFAULT 0,
                status        TEXT DEFAULT 'active'
            )
        """)
        for idx in [
            "CREATE INDEX IF NOT EXISTS idx_friendships_req ON friendships(requester_id)",
            "CREATE INDEX IF NOT EXISTS idx_friendships_addr ON friendships(addressee_id)",
            "CREATE INDEX IF NOT EXISTS idx_pvp_challenged ON pvp_challenges(challenged_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_tgames_user ON ticket_games(user_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_tgames_token ON ticket_games(session_token)",
            "CREATE INDEX IF NOT EXISTS idx_ticket_games_leaderboard ON ticket_games(game_type, status, score)",
        ]:
            try:
                await conn.execute(idx)
            except asyncpg.exceptions.DuplicateTableError:
                pass
            except Exception as e:
                shared.logger.error(f"Migration (index) failed: {e}")
                raise
        # Prevent mirrored duplicate friendships (A->B and B->A both existing at once).
        # Best-effort: skip (don't crash startup) if legacy duplicate rows already violate it.
        try:
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_friendships_unordered_pair
                ON friendships (LEAST(requester_id, addressee_id), GREATEST(requester_id, addressee_id))
            """)
        except asyncpg.exceptions.DuplicateTableError:
            pass
        except Exception as e:
            shared.logger.warning(f"Migration (friendships unique pair index) skipped: {e}")

        # One-time backfill: before this fix, adding someone who'd already sent you a
        # request errored out instead of accepting it, so plenty of mutual "we both hit
        # Add" pairs got stuck on status='pending' forever. Accept everything that's
        # pending as of the first startup after this code ships. Guarded by a flag (not
        # a hardcoded date cutoff) so it fires exactly once, whenever that first restart
        # happens, and never touches requests created afterward under the fixed logic.
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS migration_flags (
                    name TEXT PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT NOW()
                )
            """)
            flag = "friendship_stuck_pending_backfill"
            already_ran = await conn.fetchval(
                "SELECT 1 FROM migration_flags WHERE name = $1", flag
            )
            if not already_ran:
                backfilled = await conn.execute(
                    "UPDATE friendships SET status='accepted', updated_at=NOW() WHERE status='pending'"
                )
                await conn.execute(
                    "INSERT INTO migration_flags (name) VALUES ($1) ON CONFLICT DO NOTHING", flag
                )
                shared.logger.info(f"Friendship pending->accepted one-time backfill: {backfilled}")
        except Exception as e:
            shared.logger.warning(f"Migration (friendship pending backfill) skipped: {e}")

        # Referral tracking
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id          SERIAL PRIMARY KEY,
                referrer_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                referred_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE UNIQUE,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
        except Exception:
            pass

        # Guild settings
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id      BIGINT PRIMARY KEY,
                name          TEXT,
                bot_channel_id BIGINT,
                updated_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        # Quests
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS quests (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                quest_type  TEXT,
                progress    INTEGER DEFAULT 0,
                required    INTEGER,
                reward      INTEGER,
                completed   BOOLEAN DEFAULT FALSE,
                claimed     BOOLEAN DEFAULT FALSE,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        # Giveaways
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                id           SERIAL PRIMARY KEY,
                creator_id   BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                message_id   BIGINT,
                channel_id   BIGINT,
                prize        TEXT,
                prize_amount DECIMAL(10,2),
                winner_count INTEGER DEFAULT 1,
                end_time     TIMESTAMP,
                ends_at      TIMESTAMP,
                status       TEXT DEFAULT 'active',
                ended        BOOLEAN DEFAULT FALSE,
                created_at   TIMESTAMP DEFAULT NOW()
            )
        """)
        # Migration: add admin-required columns that may be missing on older installs.
        # The admin module's CREATE TABLE IF NOT EXISTS never runs because server.py
        # creates the table first; ALTER TABLE ADD COLUMN IF NOT EXISTS is idempotent.
        for _col, _def in [
            ("required_level", "INT DEFAULT 0"),
            ("required_opens",  "INT DEFAULT 0"),
            ("created_by",      "BIGINT"),
            ("drawn_at",        "TIMESTAMP"),
        ]:
            await conn.execute(f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='giveaways' AND column_name='{_col}'
                    ) THEN
                        ALTER TABLE giveaways ADD COLUMN {_col} {_def};
                    END IF;
                END $$;
            """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS giveaway_entries (
                id           SERIAL PRIMARY KEY,
                giveaway_id  INTEGER REFERENCES giveaways(id) ON DELETE CASCADE,
                user_id      BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                created_at   TIMESTAMP DEFAULT NOW(),
                UNIQUE (giveaway_id, user_id)
            )
        """)
        # Migration: add unique constraint to existing tables that lack it
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'giveaway_entries_giveaway_id_user_id_key'
                ) THEN
                    ALTER TABLE giveaway_entries
                        ADD CONSTRAINT giveaway_entries_giveaway_id_user_id_key
                        UNIQUE (giveaway_id, user_id);
                END IF;
            END $$;
        """)
        # Game tables
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS coinflip_games (
                id           SERIAL PRIMARY KEY,
                creator_id   BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                opponent_id  BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                amount       DECIMAL(15,2),
                winner_id    BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                status       TEXT DEFAULT 'waiting',
                created_at   TIMESTAMP DEFAULT NOW(),
                completed_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dice_games (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                amount      DECIMAL(15,2),
                bet_type    TEXT,
                bet_number  INTEGER,
                roll_number INTEGER,
                result      TEXT,
                multiplier  DECIMAL(10,2),
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mines_games (
                id              SERIAL PRIMARY KEY,
                user_id         BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                bet_amount      DECIMAL(15,2),
                grid_size       INTEGER DEFAULT 5,
                mine_count      INTEGER DEFAULT 3,
                status          TEXT DEFAULT 'active',
                mine_positions  INTEGER[],
                revealed_tiles  INTEGER[] DEFAULT '{}',
                multiplier      DECIMAL(10,2) DEFAULT 1.0,
                exploded        BOOLEAN DEFAULT FALSE,
                created_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS slots_games (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                bet_amount  DECIMAL(15,2),
                spin_result TEXT[],
                multiplier  DECIMAL(10,2),
                win_amount  DECIMAL(15,2),
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        # Generic game log (for new games)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS game_logs (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                game_type   TEXT NOT NULL,
                bet_amount  DECIMAL(15,2),
                win_amount  DECIMAL(15,2),
                multiplier  DECIMAL(10,4),
                result      TEXT,
                meta        JSONB,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        # Crash rounds
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS crash_rounds (
                id          SERIAL PRIMARY KEY,
                room_id     TEXT NOT NULL,
                crash_at    DECIMAL(10,2),
                started_at  TIMESTAMP DEFAULT NOW(),
                ended_at    TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS crash_bets (
                id          SERIAL PRIMARY KEY,
                round_id    INTEGER REFERENCES crash_rounds(id) ON DELETE CASCADE,
                user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                bet_amount  DECIMAL(15,2),
                cashout_at  DECIMAL(10,2),
                win_amount  DECIMAL(15,2),
                status      TEXT DEFAULT 'active',
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        # Live race
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS race_rooms (
                id          SERIAL PRIMARY KEY,
                room_code   TEXT UNIQUE NOT NULL,
                status      TEXT DEFAULT 'waiting',
                bet_amount  DECIMAL(15,2),
                winner_id   BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                created_at  TIMESTAMP DEFAULT NOW(),
                started_at  TIMESTAMP,
                ended_at    TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS race_participants (
                id          SERIAL PRIMARY KEY,
                room_id     INTEGER REFERENCES race_rooms(id) ON DELETE CASCADE,
                user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                agent_id    TEXT,
                is_bot      BOOLEAN DEFAULT FALSE,
                position    DECIMAL(10,4) DEFAULT 0,
                finished    BOOLEAN DEFAULT FALSE,
                finish_time TIMESTAMP,
                payout      DECIMAL(15,2) DEFAULT 0
            )
        """)
        # Poker
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS poker_tables (
                id          SERIAL PRIMARY KEY,
                room_code   TEXT UNIQUE NOT NULL,
                status      TEXT DEFAULT 'waiting',
                buy_in      DECIMAL(15,2),
                pot         DECIMAL(15,2) DEFAULT 0,
                community   TEXT[],
                phase       TEXT DEFAULT 'waiting',
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS poker_players (
                id          SERIAL PRIMARY KEY,
                table_id    INTEGER REFERENCES poker_tables(id) ON DELETE CASCADE,
                user_id     BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                is_bot      BOOLEAN DEFAULT FALSE,
                cards       TEXT[],
                chips       DECIMAL(15,2),
                bet         DECIMAL(15,2) DEFAULT 0,
                status      TEXT DEFAULT 'active',
                seat        INTEGER
            )
        """)
        # Misc
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_achievements (
                id             SERIAL PRIMARY KEY,
                user_id        BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                achievement_id TEXT,
                unlocked_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_streaks (
                user_id              BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                current_streak       INTEGER DEFAULT 0,
                best_streak          INTEGER DEFAULT 0,
                golds_in_streak      INTEGER DEFAULT 0,
                total_session_opens  INTEGER DEFAULT 0,
                current_case_id      TEXT,
                updated_at           TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id         BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                theme           TEXT DEFAULT 'casino',
                spin_speed      TEXT DEFAULT 'normal',
                sound_enabled   BOOLEAN DEFAULT TRUE,
                feed_enabled    BOOLEAN DEFAULT TRUE,
                confetti_mode   TEXT DEFAULT 'always',
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS live_feed (
                id           SERIAL PRIMARY KEY,
                user_id      BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                username     TEXT,
                item_name    TEXT,
                rarity       TEXT,
                rarity_emoji TEXT,
                case_type    TEXT,
                float_value  DECIMAL(10,4),
                created_at   TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS donations (
                id                 SERIAL PRIMARY KEY,
                user_id            BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                amount             DECIMAL(15,2),
                donor_name         TEXT,
                donor_email        TEXT,
                payment_provider   TEXT DEFAULT 'stripe',
                stripe_payment_id  TEXT,
                status             TEXT DEFAULT 'pending',
                created_at         TIMESTAMP DEFAULT NOW(),
                updated_at         TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ticket_purchases (
                id                SERIAL PRIMARY KEY,
                user_id           BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                amount            INTEGER,
                cost_usd          DECIMAL(10,2),
                stripe_session_id TEXT,
                created_at        TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS skin_upgrades (
                id            SERIAL PRIMARY KEY,
                user_id       BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                item_id       INTEGER,
                input_rarity  TEXT,
                output_rarity TEXT,
                success       BOOLEAN,
                created_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        # Inventory value history snapshots
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS inventory_value_snapshots (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                value      DECIMAL(15,2) NOT NULL,
                item_count INTEGER NOT NULL,
                snapped_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        try:
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inv_snapshots_user "
                "ON inventory_value_snapshots(user_id, snapped_at DESC)"
            )
        except Exception:
            pass
        # Market listings table
        try:
            from routes.market import init_market_tables
            await init_market_tables()
            logger.info("✅ Market tables ready")
        except Exception as e:
            logger.warning(f"Market table init skipped: {e}")
    logger.info("✅ All database tables ready")

# ============================================================
# DB KEEP-ALIVE
# ============================================================

async def _db_keepalive():
    while True:
        await asyncio.sleep(300)
        try:
            pool = await get_db()
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
        except Exception as e:
            logger.warning(f"DB keep-alive failed: {e}")

# Fix 9: Background task — purge abandoned in-memory game sessions every 60s
SESSION_TTL_SECONDS = 300   # 5 minutes

async def _cleanup_game_sessions():
    """Purge abandoned game sessions to prevent memory leaks."""
    while True:
        await asyncio.sleep(60)
        try:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=SESSION_TTL_SECONDS)

            # Collect session+lock dict pairs from each game module
            session_lock_pairs = []
            try:
                from routes.games_easy import _hilo_sessions, _hilo_locks
                session_lock_pairs.append((_hilo_sessions, _hilo_locks))
            except Exception:
                pass
            try:
                from routes.games_medium import (
                    _mine_sessions, _mine_locks,
                    _tower_sessions, _tower_locks,
                    _shotgun_sessions, _shotgun_locks,
                    _ladder_sessions, _ladder_locks,
                )
                session_lock_pairs.extend([
                    (_mine_sessions, _mine_locks),
                    (_tower_sessions, _tower_locks),
                    (_shotgun_sessions, _shotgun_locks),
                    (_ladder_sessions, _ladder_locks),
                ])
            except Exception:
                pass
            try:
                from routes.games_hard import (
                    _mystery_sessions, _mystery_locks,
                    _rr_sessions, _rr_locks,
                    _bj_sessions, _bj_locks,
                )
                session_lock_pairs.extend([
                    (_mystery_sessions, _mystery_locks),
                    (_rr_sessions, _rr_locks),
                    (_bj_sessions, _bj_locks),
                ])
            except Exception:
                pass
            try:
                from routes.games_poker import _vp_sessions, _vp_locks
                session_lock_pairs.append((_vp_sessions, _vp_locks))
            except Exception:
                pass

            total_cleaned = 0
            for sessions_dict, locks_dict in session_lock_pairs:
                expired = [
                    uid for uid, s in sessions_dict.items()
                    if s.get('created_at', datetime.min.replace(tzinfo=timezone.utc)) < cutoff
                ]
                for uid in expired:
                    sess = sessions_dict.pop(uid, None)
                    locks_dict.pop(uid, None)
                    total_cleaned += 1

                    # Refund the in-flight bet so the player doesn't lose money
                    # just because their session timed out before they could finish.
                    bet = (sess or {}).get('bet', 0)
                    if bet and bet > 0:
                        try:
                            _pool = await get_db()
                            async with _pool.acquire() as _conn:
                                await add_balance(uid, bet, _conn)
                            logger.info(
                                f"🧹 Refunded ${bet} to user {uid} (session TTL expired)"
                            )
                        except Exception as _e:
                            logger.warning(
                                f"Session cleanup refund failed for user {uid}: {_e}"
                            )

            if total_cleaned:
                logger.info(f"🧹 Cleaned {total_cleaned} expired game sessions")
        except Exception as e:
            logger.warning(f"Session cleanup error: {e}")


# ============================================================
# AUTH ROUTES
# ============================================================

@app.get("/auth/discord")
async def auth_discord(request: Request, response: Response):
    rl_key = request.client.host if request.client else "anon"
    if not shared.RATE_AUTH.is_allowed(f"auth_discord_start:{rl_key}"):
        raise HTTPException(429, "Too many auth attempts — please wait")
    scope = "identify email guilds"
    state = secrets.token_urlsafe(16)
    url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code&scope={scope}"
        f"&state={state}"
    )
    resp = RedirectResponse(url)
    resp.set_cookie(
        "oauth_state", state,
        max_age=300,          # 5-minute window to complete login
        httponly=True,
        samesite="lax",
        secure=os.getenv("SECURE_COOKIES", "false").lower() == "true",
    )
    return resp

@app.get("/auth/discord/callback")
@app.get("/auth/callback")   # keep old path as fallback
async def auth_callback(code: str, request: Request, response: Response, state: str = ""):
    # Rate-limit brute-force attempts on auth callback
    rl_key = request.client.host if request.client else "anon"
    if not shared.RATE_AUTH.is_allowed(f"auth_discord:{rl_key}"):
        raise HTTPException(429, "Too many auth attempts — please wait")
    # Bug 162 fix: verify CSRF state matches cookie to prevent Login CSRF
    stored_state = request.cookies.get("oauth_state", "")
    if not stored_state or not state or stored_state != state:
        raise HTTPException(400, "OAuth state mismatch — possible CSRF attempt")

    async with httpx.AsyncClient() as client:
        # Exchange code for token
        token_resp = await client.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id":     DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_resp.status_code != 200:
            raise HTTPException(400, "OAuth token exchange failed")
        token_data = token_resp.json()

        # Get user info
        user_resp = await client.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        if user_resp.status_code != 200:
            raise HTTPException(400, "Failed to fetch Discord user")
        discord_user = user_resp.json()

    discord_id = int(discord_user["id"])
    username   = discord_user.get("global_name") or discord_user.get("username", "Unknown")
    avatar     = discord_user.get("avatar")
    avatar_url = f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar}.png" if avatar else None

    pool = await get_db()

    # Link-mode: Google-primary user is adding Discord as a secondary provider
    link_mode = request.cookies.get("discord_link_mode") == "1"
    current_uid = await get_user_id_from_session(request) if link_mode else None

    async with pool.acquire() as conn:
        if link_mode and current_uid:
            # Check Discord ID not already used by another account
            existing = await conn.fetchval("SELECT user_id FROM users WHERE user_id=$1", discord_id)
            if existing and existing != current_uid:
                resp = RedirectResponse("/?error=discord_already_linked")
                resp.delete_cookie("discord_link_mode")
                return resp
            # Migrate primary account: copy Discord ID in as the account's user_id alias
            # We store discord_id on the google-primary user for display purposes
            await conn.execute("""
                UPDATE users SET avatar_url=COALESCE($1, avatar_url), username=$2, updated_at=NOW()
                WHERE user_id=$3
            """, avatar_url, username, current_uid)
            resp = RedirectResponse("/?linked=discord")
            resp.delete_cookie("discord_link_mode")
            return resp

        # Normal Discord login — upsert by Discord ID (primary provider)
        await conn.execute("""
            INSERT INTO users (user_id, username, balance, avatar_url, primary_provider, created_at, updated_at)
            VALUES ($1, $2, 1000, $3, 'discord', NOW(), NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET username=$2, avatar_url=COALESCE($3, users.avatar_url), updated_at=NOW()
        """, discord_id, username, avatar_url)
        # Bump login_count once per actual login (not per page load) so the
        # frontend welcome modal knows whether this is a first/second login.
        await conn.execute(
            "UPDATE users SET login_count = login_count + 1 WHERE user_id=$1", discord_id
        )

    token = secrets.token_urlsafe(32)
    shared.create_session(token, {
        "user_id":          int(discord_id),
        "username":         username,
        "avatar":           avatar,
        "avatar_url":       avatar_url,
        "primary_provider": "discord",
        "created_at":       datetime.now(timezone.utc).isoformat(),
    })

    resp = RedirectResponse(url="/")
    resp.set_cookie(
        "session_token", token,
        max_age=7 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=os.getenv("SECURE_COOKIES", "false").lower() == "true",
    )
    resp.delete_cookie("oauth_state")
    resp.delete_cookie("discord_link_mode")
    return resp


# ============================================================
# GOOGLE OAUTH
# ============================================================

@app.get("/auth/google")
async def google_login(request: Request):
    rl_key = request.client.host if request.client else "anon"
    if not shared.RATE_AUTH.is_allowed(f"auth_google_start:{rl_key}"):
        raise HTTPException(429, "Too many auth attempts — please wait")
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google login not configured")
    from urllib.parse import urlencode
    state = secrets.token_urlsafe(16)
    is_logged_in = bool(await get_user_id_from_session(request))
    params = urlencode({
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "access_type":   "online",
    })
    resp = RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")
    resp.set_cookie("google_oauth_state", state, max_age=300, httponly=True, samesite="lax",
                    secure=os.getenv("SECURE_COOKIES", "false").lower() == "true")
    resp.set_cookie("google_link_mode", "1" if is_logged_in else "0",
                    max_age=300, httponly=True, samesite="lax",
                    secure=os.getenv("SECURE_COOKIES", "false").lower() == "true")
    return resp

@app.get("/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    # Rate-limit brute-force attempts on auth callback
    rl_key = request.client.host if request.client else "anon"
    if not shared.RATE_AUTH.is_allowed(f"auth_google:{rl_key}"):
        raise HTTPException(429, "Too many auth attempts — please wait")
    if error:
        return RedirectResponse("/?error=google_denied")
    stored_state = request.cookies.get("google_oauth_state", "")
    if not stored_state or stored_state != state:
        raise HTTPException(400, "OAuth state mismatch")

    link_mode = request.cookies.get("google_link_mode", "0") == "1"

    async with httpx.AsyncClient() as client:
        tok = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code":          code,
            "grant_type":    "authorization_code",
            "redirect_uri":  GOOGLE_REDIRECT_URI,
        })
        if tok.status_code != 200:
            raise HTTPException(400, "Google token exchange failed")
        ginfo = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
        )
        if ginfo.status_code != 200:
            raise HTTPException(400, "Failed to fetch Google user info")
        gu = ginfo.json()

    google_id     = gu["id"]
    google_email  = gu.get("email", "")
    google_name   = gu.get("name") or google_email.split("@")[0] or "User"
    google_avatar = gu.get("picture", "")

    pool = await get_db()
    _secure = os.getenv("SECURE_COOKIES", "false").lower() == "true"

    def _clear_google_cookies(r):
        r.delete_cookie("google_oauth_state")
        r.delete_cookie("google_link_mode")
        return r

    if link_mode:
        current_uid = await get_user_id_from_session(request)
        if not current_uid:
            return _clear_google_cookies(RedirectResponse("/?error=session_expired"))
        async with pool.acquire() as conn:
            clash = await conn.fetchval(
                "SELECT user_id FROM users WHERE google_id=$1 AND user_id!=$2", google_id, current_uid
            )
            if clash:
                return _clear_google_cookies(RedirectResponse("/?error=google_already_linked"))
            await conn.execute("""
                UPDATE users SET google_id=$1, google_email=$2, google_avatar_url=$3, updated_at=NOW()
                WHERE user_id=$4
            """, google_id, google_email, google_avatar, current_uid)
        return _clear_google_cookies(RedirectResponse("/?linked=google"))

    # Standalone Google login / signup
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM users WHERE google_id=$1", google_id)
        if row:
            user_id = row["user_id"]
            await conn.execute("""
                UPDATE users SET google_avatar_url=$1, google_email=$2, updated_at=NOW()
                WHERE user_id=$3
            """, google_avatar, google_email, user_id)
        else:
            user_id = await conn.fetchval("SELECT nextval('google_user_id_seq')")
            await conn.execute("""
                INSERT INTO users (user_id, username, balance, google_id, google_email,
                                   google_avatar_url, primary_provider, created_at, updated_at)
                VALUES ($1, $2, 1000, $3, $4, $5, 'google', NOW(), NOW())
            """, user_id, google_name, google_id, google_email, google_avatar)
        # Bump login_count once per actual login (not per page load) so the
        # frontend welcome modal knows whether this is a first/second login.
        await conn.execute(
            "UPDATE users SET login_count = login_count + 1 WHERE user_id=$1", user_id
        )

    token = secrets.token_urlsafe(32)
    shared.create_session(token, {
        "user_id":          int(user_id),
        "username":         google_name,
        "avatar":           None,
        "avatar_url":       google_avatar,
        "primary_provider": "google",
        "created_at":       datetime.now(timezone.utc).isoformat(),
    })
    resp = RedirectResponse("/")
    resp.set_cookie("session_token", token, max_age=7*24*3600, httponly=True,
                    samesite="lax", secure=_secure)
    return _clear_google_cookies(resp)


@app.get("/auth/discord/link")
async def discord_link(request: Request):
    """Start Discord OAuth in link mode for Google-primary users."""
    if not await get_user_id_from_session(request):
        return RedirectResponse("/")
    from urllib.parse import urlencode
    state = secrets.token_urlsafe(16)
    params = urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "redirect_uri":  DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope":         "identify",
        "state":         state,
    })
    resp = RedirectResponse(f"https://discord.com/api/oauth2/authorize?{params}")
    resp.set_cookie("oauth_state",        state, max_age=300, httponly=True, samesite="lax",
                    secure=os.getenv("SECURE_COOKIES", "false").lower() == "true")
    resp.set_cookie("discord_link_mode", "1",   max_age=300, httponly=True, samesite="lax",
                    secure=os.getenv("SECURE_COOKIES", "false").lower() == "true")
    return resp

@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("session_token")
    if token:
        shared.delete_session(token)
    response.delete_cookie("session_token")
    return {"success": True}

@app.get("/auth/logout")
async def auth_logout_get(request: Request, response: Response):
    """GET logout — used by frontend window.location redirects."""
    token = request.cookies.get("session_token")
    if token:
        shared.delete_session(token)
    resp = RedirectResponse(url="/")
    resp.delete_cookie("session_token")
    return resp

# ============================================================
# USER / ME
# ============================================================

@app.get("/api/user/me")
async def get_me(request: Request):
    user_id = await get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")

    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_seen=NOW() WHERE user_id=$1", user_id
        )
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE user_id = $1", user_id
        )

    if not user:
        raise HTTPException(404, "User not found")

    # Fire-and-forget daily inventory value snapshot (non-blocking)
    asyncio.create_task(_snapshot_inventory_value(user_id, pool))

    session = shared.get_session(request.cookies.get("session_token") or "") or {}
    primary = (user.get("primary_provider") or session.get("primary_provider") or "discord")

    # Build avatar URL: prefer Google avatar for Google-primary users
    avatar_url = None
    if primary == "google":
        avatar_url = (user.get("google_avatar_url") or session.get("avatar_url")
                      or user.get("avatar_url"))
    else:
        avatar = session.get("avatar")
        if avatar:
            avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png"
        avatar_url = avatar_url or session.get("avatar_url") or user.get("avatar_url")

    return {
        "user_id":          str(user["user_id"]),
        "username":         user["username"],
        "balance":          float(user["balance"] or 0),
        "tickets":          int(user["tickets"] or 0),
        "xp":               int(user["xp"] or 0),
        "level":            int(user["level"] or 1),
        "prestige":         int(user["prestige"] or 0),
        "total_opens":      int(user["total_opens"] or 0),
        "total_golds":      int(user["total_golds"] or 0),
        "avatar_url":       avatar_url,
        "avatar":           session.get("avatar"),
        "primary_provider": primary,
        "google_linked":    bool(user.get("google_id")),
        "discord_linked":   primary == "discord" or bool(user.get("avatar_url")),
        "login_count":      int(user.get("login_count") or 0),
        "google_email":     user.get("google_email") or "",
        "is_google":        primary == "google",
        "is_admin":         user_id in ADMIN_USER_IDS,
    }

# Alias for frontend compatibility
@app.get("/api/me")
async def get_me_alias(request: Request):
    return await get_me(request)

@app.get("/api/user/me/inventory")
async def get_inventory(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    rarity: Optional[str] = None,
    item_type: Optional[str] = None,
):
    # Bug 161 fix: cap limit/offset to prevent table-scan DoS
    limit  = max(1, min(200, limit))
    offset = max(0, offset)
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        where = ["user_id = $1", "status = 'kept'"]
        params: list = [user_id]
        if rarity:
            params.append(rarity)
            where.append(f"rarity = ${len(params)}")
        if item_type:
            params.append(item_type)
            where.append(f"item_type = ${len(params)}")
        where_sql = " AND ".join(where)
        params += [limit, offset]
        # Worst condition (highest float = closest to Battle-Scarred) first, best
        # (lowest float = closest to Factory New) last — so on the inventory page
        # the best items land on the final page, and on the trade-up screen the
        # worst items are offered first, guarding against accidentally trading
        # away a good-condition item. Items with no float (stickers) sort last.
        rows = await conn.fetch(f"""
            SELECT * FROM inventory WHERE {where_sql}
            ORDER BY float_value DESC NULLS LAST, created_at DESC
            LIMIT ${len(params)-1} OFFSET ${len(params)}
        """, *params)
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM inventory WHERE {where_sql}",
            *params[:-2]
        )
    def _enrich(r):
        d = convert_decimals(dict(r))
        raw_name = d.get("item_name", "")
        # Strip leading rarity emoji from stored names so frontend gets clean names
        import re as _re
        clean = _re.sub(r'^[\U0001F300-\U0001FFFF\U00002600-\U000027BF\U0000FE00-\U0000FEFF\s🟦🟪🟥🟨🟩⬛⬜🟫🔥⭐💫👑✨]+', '', raw_name).strip()
        d["display_name"] = clean or raw_name
        d["name"]         = clean or raw_name
        # Parse applied_stickers from JSONB (comes back as string or list)
        import json as _json
        raw_stickers = d.get("applied_stickers")
        if isinstance(raw_stickers, str):
            try:
                d["applied_stickers"] = _json.loads(raw_stickers)
            except Exception:
                d["applied_stickers"] = []
        elif raw_stickers is None:
            d["applied_stickers"] = []
        # Populate image_url for items that predate the image_url column
        if not d.get("image_url"):
            item_type = (d.get("item_type") or "").lower()
            item_name = clean or raw_name
            if item_type == "sticker":
                # Reconstruct sticker image path from shared sticker data
                from shared import get_sticker_image
                sticker_file = get_sticker_image(item_name)
                if sticker_file:
                    d["image_url"] = f"/static/images/stickers/{sticker_file}"
            else:
                # Weapon skin: use name-based lookup (works for "AK-47 | Redline" format)
                from shared import get_skin_image_filename
                filename = get_skin_image_filename(item_name)
                if filename:
                    d["image_url"] = f"/static/images/skins/{filename}"
        return d
    return {
        "items": [_enrich(r) for r in rows],
        "total": int(total or 0),
        "count": int(total or 0),
    }

# ─── Sticker application ─────────────────────────────────────

def _clamp(value, lo, hi, default):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


@app.post("/api/inventory/{weapon_id}/sticker")
async def apply_sticker(weapon_id: int, request: Request):
    import json as _json
    user_id = await require_auth(request)
    body = await request.json()
    sticker_id = int(body.get("sticker_id", 0))
    slot = int(body.get("slot", 0))
    if slot not in (0, 1, 2, 3):
        raise HTTPException(400, "Slot must be 0–3")
    # Position/rotation/scale for the sticker sandbox (all optional -- default
    # to centered/upright/normal size so a client that doesn't send them yet
    # still gets a sane placement).
    x        = _clamp(body.get("x", 0.5), 0.0, 1.0, 0.5)
    y        = _clamp(body.get("y", 0.5), 0.0, 1.0, 0.5)
    rotation = _clamp(body.get("rotation", 0), 0.0, 360.0, 0.0)
    scale    = _clamp(body.get("scale", 1.0), 0.3, 2.5, 1.0)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            weapon = await conn.fetchrow(
                "SELECT id, item_type, applied_stickers FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept' FOR UPDATE",
                weapon_id, user_id
            )
            if not weapon:
                raise HTTPException(404, "Weapon not found")
            if (weapon["item_type"] or "weapon") not in ("weapon", "gold"):
                raise HTTPException(400, "Can only apply stickers to weapons")
            sticker = await conn.fetchrow(
                "SELECT id, item_name, rarity, image_url FROM inventory WHERE id=$1 AND user_id=$2 AND item_type='sticker' AND status='kept' FOR UPDATE",
                sticker_id, user_id
            )
            if not sticker:
                raise HTTPException(404, "Sticker not found")

            raw = weapon["applied_stickers"]
            current = _json.loads(raw) if isinstance(raw, str) else (raw or [])
            # Check slot not already occupied
            if any(s.get("slot") == slot for s in current):
                raise HTTPException(400, f"Slot {slot} already has a sticker")
            if len(current) >= 4:
                raise HTTPException(400, "All 4 sticker slots are full")

            current.append({
                "slot":          slot,
                "sticker_id":    sticker_id,
                "sticker_name":  sticker["item_name"],
                "sticker_image": sticker["image_url"] or "",
                "rarity":        sticker["rarity"] or "",
                "x":             x,
                "y":             y,
                "rotation":      rotation,
                "scale":         scale,
            })
            await conn.execute(
                "UPDATE inventory SET applied_stickers=$1 WHERE id=$2",
                current, weapon_id
            )
            # Consume the sticker
            await conn.execute("UPDATE inventory SET status='sold' WHERE id=$1", sticker_id)
    return {"success": True, "applied_stickers": current}


@app.patch("/api/inventory/{weapon_id}/sticker/{slot}")
async def reposition_sticker(weapon_id: int, slot: int, request: Request):
    """Move/rotate/resize an ALREADY-applied sticker -- unlike apply_sticker,
    this never consumes anything, it just rewrites that slot's placement
    fields in place. Used by the sticker sandbox to save a repositioning."""
    import json as _json
    user_id = await require_auth(request)
    if slot not in (0, 1, 2, 3):
        raise HTTPException(400, "Slot must be 0–3")
    body = await request.json()
    x        = _clamp(body.get("x", 0.5), 0.0, 1.0, 0.5)
    y        = _clamp(body.get("y", 0.5), 0.0, 1.0, 0.5)
    rotation = _clamp(body.get("rotation", 0), 0.0, 360.0, 0.0)
    scale    = _clamp(body.get("scale", 1.0), 0.3, 2.5, 1.0)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            weapon = await conn.fetchrow(
                "SELECT id, applied_stickers FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept' FOR UPDATE",
                weapon_id, user_id
            )
            if not weapon:
                raise HTTPException(404, "Weapon not found")
            raw = weapon["applied_stickers"]
            current = _json.loads(raw) if isinstance(raw, str) else (raw or [])
            found = False
            for s in current:
                if s.get("slot") == slot:
                    s["x"], s["y"], s["rotation"], s["scale"] = x, y, rotation, scale
                    found = True
                    break
            if not found:
                raise HTTPException(404, f"No sticker in slot {slot}")
            await conn.execute(
                "UPDATE inventory SET applied_stickers=$1 WHERE id=$2",
                current, weapon_id
            )
    return {"success": True, "applied_stickers": current}


@app.delete("/api/inventory/{weapon_id}/sticker/{slot}")
async def remove_sticker(weapon_id: int, slot: int, request: Request):
    import json as _json
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            weapon = await conn.fetchrow(
                "SELECT id, applied_stickers FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept' FOR UPDATE",
                weapon_id, user_id
            )
            if not weapon:
                raise HTTPException(404, "Weapon not found")
            raw = weapon["applied_stickers"]
            current = _json.loads(raw) if isinstance(raw, str) else (raw or [])
            updated = [s for s in current if s.get("slot") != slot]
            await conn.execute(
                "UPDATE inventory SET applied_stickers=$1 WHERE id=$2",
                updated, weapon_id
            )
    return {"success": True, "applied_stickers": updated}


# ─── Loadout ──────────────────────────────────────────────────

@app.post("/api/inventory/{item_id}/loadout")
async def toggle_loadout(item_id: int, request: Request):
    """Toggles an item's membership in the user's currently ACTIVE loadout
    (auto-creating a default 'My Loadout' the first time this is used) --
    keeps the single-star-button UX from before named loadouts existed.
    Managing membership in a non-active loadout goes through routes/loadouts.py."""
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, in_loadout FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept' FOR UPDATE",
                item_id, user_id
            )
            if not row:
                raise HTTPException(404, "Item not found")
            active = await conn.fetchrow(
                "SELECT id FROM loadouts WHERE user_id=$1 AND is_active=TRUE FOR UPDATE",
                user_id
            )
            if active:
                active_id = active["id"]
            else:
                active_id = await conn.fetchval(
                    "INSERT INTO loadouts (user_id, name, is_active) VALUES ($1, 'My Loadout', TRUE) RETURNING id",
                    user_id
                )
            new_val = not bool(row["in_loadout"])
            if new_val:
                await conn.execute(
                    "INSERT INTO loadout_items (loadout_id, inventory_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    active_id, item_id
                )
            else:
                await conn.execute(
                    "DELETE FROM loadout_items WHERE loadout_id=$1 AND inventory_id=$2",
                    active_id, item_id
                )
            await conn.execute("UPDATE inventory SET in_loadout=$1 WHERE id=$2", new_val, item_id)
    return {"success": True, "in_loadout": new_val}


# ─── Item protection (blocks sell/trade-up/stake, not sticker cosmetics) ──

@app.patch("/api/inventory/{item_id}/protect")
async def toggle_protect(item_id: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, protected FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept' FOR UPDATE",
                item_id, user_id
            )
            if not row:
                raise HTTPException(404, "Item not found")
            new_val = not bool(row["protected"])
            await conn.execute("UPDATE inventory SET protected=$1 WHERE id=$2", new_val, item_id)
    return {"success": True, "protected": new_val}


@app.get("/api/loadout")
async def get_loadout(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM inventory WHERE user_id=$1 AND in_loadout=TRUE AND status='kept' ORDER BY created_at DESC",
            user_id
        )
    import json as _json, re as _re
    def _enrich_loadout(r):
        d = convert_decimals(dict(r))
        raw = d.get("item_name", "")
        clean = _re.sub(r'^[\U0001F300-\U0001FFFF\U00002600-\U000027BF\U0000FE00-\U0000FEFF\s🟦🟪🟥🟨🟩⬛⬜🟫🔥⭐💫👑✨]+', '', raw).strip()
        d["display_name"] = clean or raw
        if not d.get("image_url"):
            from shared import get_skin_image_filename
            filename = get_skin_image_filename(clean or raw)
            if filename:
                d["image_url"] = f"/static/images/skins/{filename}"
        raw_st = d.get("applied_stickers")
        d["applied_stickers"] = _json.loads(raw_st) if isinstance(raw_st, str) else (raw_st or [])
        return d
    return {"items": [_enrich_loadout(r) for r in rows]}


# ─── New endpoints for frontend ──────────────────────────────

@app.get("/api/user/me/stats")
async def get_user_stats(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT total_opens, total_golds, total_trades, daily_streak
            FROM users WHERE user_id = $1
        """, user_id)
        inv = await conn.fetchrow("""
            SELECT COUNT(*) as count, COALESCE(SUM(price), 0) as value
            FROM inventory WHERE user_id = $1 AND status = 'kept'
        """, user_id)
    if not row:
        raise HTTPException(404, "User not found")
    return {
        "total_opens": row["total_opens"] or 0,
        "total_golds": row["total_golds"] or 0,
        "total_trades": row["total_trades"] or 0,
        "daily_streak": row["daily_streak"] or 0,
        "inventory_count": inv["count"] if inv else 0,
        "inventory_value": float(inv["value"]) if inv else 0.0,
    }

@app.get("/api/user/me/tickets")
async def get_tickets(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        tickets = await conn.fetchval("SELECT tickets FROM users WHERE user_id = $1", user_id)
    return {"tickets": tickets or 0}

@app.get("/api/user/me/profile")
async def get_profile(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT level, prestige, xp FROM users WHERE user_id = $1
        """, user_id)
        if not row:
            raise HTTPException(404, "User not found")
        level = row["level"] or 1
        xp = row["xp"] or 0
        xp_needed = level * 50 + 100
        xp_progress = min(100, (xp / xp_needed) * 100)
    return {
        "level": level,
        "prestige": row["prestige"] or 0,
        "xp": xp,
        "xp_needed": xp_needed,
        "xp_progress": round(xp_progress, 1),
    }

# ── XP / levelling helper ────────────────────────────────────
async def grant_xp(user_id: int, amount: int, conn=None) -> dict:
    """
    Award XP to a user and handle level-ups.
    Returns {"xp": new_xp, "level": new_level, "leveled_up": bool}
    Works inside an existing conn (no transaction) or opens its own.
    """
    async def _do(c):
        # FOR UPDATE inside a (possibly savepoint) transaction prevents a
        # TOCTOU race where two concurrent grant_xp calls both read the same
        # xp value and each overwrites the other's increment.
        async with c.transaction():
            row = await c.fetchrow(
                "SELECT xp, level, prestige FROM users WHERE user_id=$1 FOR UPDATE",
                user_id,
            )
            if not row:
                return {"xp": 0, "level": 1, "leveled_up": False}
            xp      = (row["xp"] or 0) + amount
            level   = row["level"] or 1
            prestige = row["prestige"] or 0
            leveled_up = False
            # Level-up loop — grants balance reward and prestige bonus per level
            while True:
                xp_needed = level * 50 + 100
                if xp >= xp_needed:
                    xp -= xp_needed
                    level += 1
                    leveled_up = True
                    reward = level * 50
                    await c.execute(
                        "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                        reward, user_id
                    )
                    if level % 50 == 0:
                        prestige += 1
                        prestige_bonus = prestige * 5000
                        await c.execute(
                            "UPDATE users SET balance = balance + $1, prestige = $2 WHERE user_id = $3",
                            prestige_bonus, prestige, user_id
                        )
                else:
                    break
            await c.execute(
                "UPDATE users SET xp=$1, level=$2 WHERE user_id=$3",
                xp, level, user_id
            )
            return {"xp": xp, "level": level, "leveled_up": leveled_up, "prestige": prestige}

    pool = await get_db()
    if conn:
        return await _do(conn)
    async with pool.acquire() as c:
        return await _do(c)

@app.get("/api/user/me/balance")
async def get_balance_alias(request: Request):
    return await get_balance_endpoint(request)

@app.get("/api/user/settings")
async def get_user_settings(request: Request):
    import json as _j
    user_id = await require_auth(request)
    D = {"theme": "casino", "spin_speed": "normal", "sound_enabled": True, "confetti_mode": "always"}
    pool = await get_db()
    async with pool.acquire() as conn:
        raw = await conn.fetchval("SELECT settings FROM users WHERE user_id=$1", user_id)
        if raw is None:
            row = await conn.fetchrow("SELECT * FROM user_settings WHERE user_id=$1", user_id)
            if row:
                return {"theme": row["theme"] or "casino", "spin_speed": row["spin_speed"] or "normal",
                        "sound_enabled": bool(row["sound_enabled"]), "confetti_mode": row["confetti_mode"] or "always"}
            return D
    if isinstance(raw, str):
        try: return {**D, **_j.loads(raw)}
        except Exception: return D
    if isinstance(raw, dict): return {**D, **raw}
    return D

@app.post("/api/user/settings")
async def save_user_settings(request: Request):
    import json as _j
    user_id = await require_auth(request)
    body = await request.json()
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            raw = await conn.fetchval("SELECT settings FROM users WHERE user_id=$1", user_id)
            try:
                existing = _j.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:
                existing = {}
            existing.update(body)
            await conn.execute("UPDATE users SET settings=$1 WHERE user_id=$2", _j.dumps(existing), user_id)
            try:
                await conn.execute("""
                    INSERT INTO user_settings (user_id,theme,spin_speed,sound_enabled,confetti_mode)
                    VALUES ($1,$2,$3,$4,$5) ON CONFLICT (user_id) DO UPDATE
                    SET theme=$2,spin_speed=$3,sound_enabled=$4,confetti_mode=$5,updated_at=NOW()
                """, user_id, existing.get("theme","casino"), existing.get("spin_speed","normal"),
                    bool(existing.get("sound_enabled",True)), existing.get("confetti_mode","always"))
            except Exception as e:
                logger.warning(f"Failed to persist user_settings for user {user_id}: {e}")
    return {"success": True}

@app.get("/api/user/streak")
async def get_user_streak(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM user_streaks WHERE user_id = $1", user_id)
    if not row:
        return {"current_streak": 0, "best_streak": 0, "golds_in_streak": 0, "total_opens": 0}
    return {
        "current_streak": row["current_streak"] or 0,
        "best_streak": row["best_streak"] or 0,
        "golds_in_streak": row["golds_in_streak"] or 0,
        "total_opens": row["total_session_opens"] or 0,
    }

@app.get("/api/user/favorites")
async def get_user_favorites(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        try:
            settings_raw = await conn.fetchval("SELECT settings FROM users WHERE user_id = $1", user_id)
            if settings_raw is None:
                return {"favorite_ids": [], "count": 0, "favorites": []}
            # Parse JSON string
            import json
            settings = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
            favs = settings.get("favorites", [])
            # Build case list for frontend
            case_list = []
            for cid in favs:
                case = CASES.get(cid)
                if case:
                    eff = await get_effective_case(cid, case["price"], cid in FEATURED_CASES)
                    case_list.append({"id": cid, "name": case["name"], "emoji": case.get("emoji", "📦"), "price": eff["price"]})
            return {"favorite_ids": favs, "count": len(favs), "favorites": case_list}
        except Exception as e:
            logger.exception("Get favorites error")
            raise HTTPException(500, f"Error loading favorites: {str(e)}")

@app.post("/api/user/favorites/add")
async def add_favorite(request: Request):
    body = await request.json()
    user_id = await require_auth(request)
    case_id = body.get("case_id")
    if not case_id or case_id not in CASES:
        raise HTTPException(400, "Invalid case id")

    pool = await get_db()
    async with pool.acquire() as conn:
        try:
            import json
            async with conn.transaction():
                # FOR UPDATE prevents concurrent add_favorite calls from both
                # passing the len < 5 check and storing more than 5 favorites.
                settings_raw = await conn.fetchval(
                    "SELECT settings FROM users WHERE user_id = $1 FOR UPDATE", user_id
                )
                if settings_raw is None:
                    settings = {}
                elif isinstance(settings_raw, str):
                    settings = json.loads(settings_raw)
                else:
                    settings = settings_raw

                favs = settings.get("favorites", [])
                if case_id not in favs:
                    if len(favs) >= 5:
                        raise HTTPException(400, "Maximum 5 favorites allowed")
                    favs.append(case_id)
                settings["favorites"] = favs

                # Store as JSON string
                await conn.execute("UPDATE users SET settings = $1 WHERE user_id = $2", json.dumps(settings), user_id)
            return {"success": True, "favorites": favs}
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Favorite add error")
            raise HTTPException(500, f"Database error: {str(e)}")

@app.post("/api/user/favorites/remove")
async def remove_favorite(request: Request):
    body = await request.json()
    user_id = await require_auth(request)
    case_id = body.get("case_id")
    if not case_id:
        raise HTTPException(400, "Missing case_id")

    pool = await get_db()
    async with pool.acquire() as conn:
        try:
            import json
            # Bug 163 fix: use a transaction with FOR UPDATE so concurrent add/remove calls
            # cannot interleave their read-modify-write cycles and corrupt the settings JSON.
            async with conn.transaction():
                settings_raw = await conn.fetchval(
                    "SELECT settings FROM users WHERE user_id = $1 FOR UPDATE", user_id
                )
                if settings_raw is None:
                    settings = {}
                else:
                    settings = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw

                favs = settings.get("favorites", [])
                if case_id in favs:
                    favs.remove(case_id)
                    settings["favorites"] = favs
                    await conn.execute("UPDATE users SET settings = $1 WHERE user_id = $2", json.dumps(settings), user_id)
            return {"success": True, "favorites": favs}
        except Exception as e:
            logger.exception("Remove favorite error")
            raise HTTPException(500, f"Error removing favorite: {str(e)}")

@app.get("/api/user/achievements")
async def get_achievements(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT total_opens, total_golds, total_trades, daily_streak, balance,
                   total_spent, total_hourly_claimed, total_weekly_claimed,
                   coinflip_wins, dice_wins, slots_wins, mines_wins, jackpot_wins,
                   total_premium_opens, total_inventory_items, total_stickers,
                   total_games_played, win_streak, total_quests_completed,
                   total_premium_opens, last_agent_egg_claim, agent_phrases_seen
            FROM users WHERE user_id = $1
        """, user_id)

    def u(col, default=0):
        if not user: return default
        v = user[col]
        return float(v) if v is not None else default

    achievement_defs = [
        # ── Case opening ──
        {"id": "first_open",        "name": "First Case",         "icon": "🎯", "description": "Open your first case",             "unlocked": u('total_opens') >= 1},
        {"id": "case_master",       "name": "Case Master",        "icon": "🎰", "description": "Open 100 cases",                   "unlocked": u('total_opens') >= 100},
        {"id": "case_collector",    "name": "Case Collector",     "icon": "📦", "description": "Open 500 cases",                   "unlocked": u('total_opens') >= 500},
        {"id": "case_connoisseur",  "name": "Case Connoisseur",   "icon": "🎁", "description": "Open 1,000 cases",                 "unlocked": u('total_opens') >= 1000},
        # ── Gold hunting ──
        {"id": "first_gold",        "name": "First Gold",         "icon": "⭐", "description": "Find your first Gold item",         "unlocked": u('total_golds') >= 1},
        {"id": "gold_hunter",       "name": "Gold Hunter",        "icon": "🏆", "description": "Find 10 Gold items",               "unlocked": u('total_golds') >= 10},
        {"id": "gold_digger",       "name": "Gold Digger",        "icon": "⛏️", "description": "Find 50 Gold items",               "unlocked": u('total_golds') >= 50},
        {"id": "gold_legend",       "name": "Gold Legend",        "icon": "👑", "description": "Find 100 Gold items",              "unlocked": u('total_golds') >= 100},
        # ── Daily streak ──
        {"id": "streak_5",          "name": "Streak 5",           "icon": "🔥", "description": "Maintain a 5-day daily streak",    "unlocked": u('daily_streak') >= 5},
        {"id": "streak_10",         "name": "Streak 10",          "icon": "💎", "description": "Maintain a 10-day daily streak",   "unlocked": u('daily_streak') >= 10},
        {"id": "daily_dedication",  "name": "Daily Dedication",   "icon": "📆", "description": "Maintain a 30-day daily streak",   "unlocked": u('daily_streak') >= 30},
        {"id": "streak_25",         "name": "Lucky Streak",       "icon": "🍀", "description": "Maintain a 25-day daily streak",   "unlocked": u('daily_streak') >= 25},
        {"id": "streak_50",         "name": "Streak Legend",      "icon": "👑", "description": "Maintain a 50-day daily streak",   "unlocked": u('daily_streak') >= 50},
        # ── Balance milestones ──
        {"id": "high_roller",       "name": "High Roller",        "icon": "💎", "description": "Reach $100,000 balance",           "unlocked": u('balance') >= 100000},
        {"id": "millionaire",       "name": "Millionaire",        "icon": "💰", "description": "Reach $1,000,000 balance",         "unlocked": u('balance') >= 1000000},
        # ── Spending ──
        {"id": "spender",           "name": "Spender",            "icon": "💳", "description": "Spend $10,000 on cases",           "unlocked": u('total_spent') >= 10000},
        {"id": "big_spender",       "name": "Big Spender",        "icon": "💎", "description": "Spend $100,000 on cases",          "unlocked": u('total_spent') >= 100000},
        # ── Trading ──
        {"id": "first_trade",       "name": "First Trade",        "icon": "🤝", "description": "Complete your first trade-up",     "unlocked": u('total_trades') >= 1},
        {"id": "market_trader",     "name": "Market Trader",      "icon": "📊", "description": "Complete 10 trade-ups",            "unlocked": u('total_trades') >= 10},
        {"id": "trade_master",      "name": "Trade Master",       "icon": "🔄", "description": "Complete 50 trade-ups",            "unlocked": u('total_trades') >= 50},
        {"id": "trade_legend",      "name": "Trade Legend",       "icon": "🏅", "description": "Complete 500 trade-ups",           "unlocked": u('total_trades') >= 500},
        # ── Claims ──
        {"id": "hourly_grinder",    "name": "Hourly Grinder",     "icon": "🕐", "description": "Claim 100 hourly rewards",         "unlocked": u('total_hourly_claimed') >= 100},
        {"id": "weekly_warrior",    "name": "Weekly Warrior",     "icon": "📅", "description": "Claim 10 weekly rewards",          "unlocked": u('total_weekly_claimed') >= 10},
        # ── Games ──
        {"id": "coinflip_champion", "name": "Coinflip Champion",  "icon": "🪙", "description": "Win 50 coinflips",                 "unlocked": u('coinflip_wins') >= 50},
        {"id": "dice_master",       "name": "Dice Master",        "icon": "🎲", "description": "Win 100 dice rolls",               "unlocked": u('dice_wins') >= 100},
        {"id": "slots_king",        "name": "Slots King",         "icon": "🎰", "description": "Win 50 slots spins",               "unlocked": u('slots_wins') >= 50},
        {"id": "miner",             "name": "Miner",              "icon": "⛏️", "description": "Win 10 mines games",               "unlocked": u('mines_wins') >= 10},
        {"id": "jackpot_winner",    "name": "Jackpot Winner",     "icon": "🎰", "description": "Win a jackpot",                    "unlocked": u('jackpot_wins') >= 1},
        {"id": "gambler",           "name": "Gambler",            "icon": "🎯", "description": "Play 50 casino games",             "unlocked": u('total_games_played') >= 50},
        {"id": "casino_regular",    "name": "Casino Regular",     "icon": "🎰", "description": "Play 500 casino games",            "unlocked": u('total_games_played') >= 500},
        {"id": "steamroller",       "name": "Steamroller",        "icon": "🔥", "description": "Win 10 games in a row",            "unlocked": u('win_streak') >= 10},
        {"id": "unstoppable",       "name": "Unstoppable",        "icon": "⚡", "description": "Win 25 games in a row",            "unlocked": u('win_streak') >= 25},
        # ── Premium ──
        {"id": "premium_user",      "name": "Premium User",       "icon": "🎟️", "description": "Open your first premium case",    "unlocked": u('total_premium_opens') >= 1},
        {"id": "whale",             "name": "Whale",              "icon": "🐋", "description": "Open 100 premium cases",           "unlocked": u('total_premium_opens') >= 100},
        # ── Inventory / stickers ──
        {"id": "inventory_collector","name": "Inventory Collector","icon": "📦", "description": "Have 100 items in your inventory", "unlocked": u('total_inventory_items') >= 100},
        {"id": "hoarder",           "name": "Hoarder",            "icon": "🏚️", "description": "Have 500 items in your inventory", "unlocked": u('total_inventory_items') >= 500},
        {"id": "sticker_collector", "name": "Sticker Collector",  "icon": "⭐", "description": "Collect 50 stickers",              "unlocked": u('total_stickers') >= 50},
        {"id": "sticker_master",    "name": "Sticker Master",     "icon": "✨", "description": "Collect 200 stickers",             "unlocked": u('total_stickers') >= 200},
        # ── Quests ──
        {"id": "quest_master",      "name": "Quest Master",       "icon": "📋", "description": "Complete 50 quests",               "unlocked": u('total_quests_completed') >= 50},
        # ── General ──
        {"id": "newbie",            "name": "Newbie",             "icon": "👋", "description": "Log in for the first time",        "unlocked": user is not None},
        {"id": "secret_agent",      "name": "Secret Agent",       "icon": "🕵️", "description": "Find and claim the hidden agent Easter egg", "unlocked": bool(user and user["last_agent_egg_claim"] is not None)},
        {"id": "greedy_bastard",    "name": "Greedy Bastard",     "icon": "🤑", "description": "Hear all of the agent's catchphrases for coming back too soon", "unlocked": bool(user and (user["agent_phrases_seen"] or 0) == (2 ** len(AGENT_EGG_PHRASES)) - 1)},
    ]

    unlocked_count = sum(1 for a in achievement_defs if a["unlocked"])
    return {
        "achievements": achievement_defs,
        "unlocked_count": unlocked_count,
        "total_count": len(achievement_defs),
    }

# ─── Daily, Cases, Featured, etc. ──────────────────────────

@app.post("/api/daily")
async def claim_daily(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                "SELECT balance, daily_streak, last_daily FROM users WHERE user_id = $1 FOR UPDATE",
                user_id
            )
            if not user:
                raise HTTPException(404, "User not found")
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            last = user["last_daily"]
            if last and last.tzinfo is not None:
                last = last.replace(tzinfo=None)
            streak = user["daily_streak"] or 0
            if last and last.date() == now.date():
                raise HTTPException(400, "Already claimed today")
            if last and last.date() == (now - timedelta(days=1)).date():
                streak += 1
            else:
                streak = 1
            reward = 500 + (streak * 100)
            jackpot = shared.secure_randint(1, 1000000) == 1
            if jackpot:
                reward += 50000
            await conn.execute(
                "UPDATE users SET balance = balance + $1, daily_streak = $2, last_daily = $3 WHERE user_id = $4",
                reward, streak, now, user_id
            )
            # daily_streak quest tracks the user's actual login streak (only
            # advances once per day), not an incremental counter — it can
            # never reach base_required within a single day's quest window.
            await conn.execute("""
                UPDATE quests SET progress = LEAST($1, required)
                WHERE user_id=$2 AND quest_type='daily_streak' AND completed=FALSE
            """, streak, user_id)
            await conn.execute("""
                UPDATE quests SET completed=TRUE
                WHERE user_id=$1 AND quest_type='daily_streak' AND progress >= required AND completed=FALSE
            """, user_id)
    return {"success": True, "reward": reward, "streak": streak, "jackpot": jackpot}

# ── Agent Easter Egg — a fixed agent shown as a corner decoration on the
# main hub pages; clicking it grants a small reward once per 24h. Clicking it
# again after that gives nothing but one of a few sarcastic catchphrases. ──
AGENT_EGG_REWARD = 100
AGENT_EGG_TICKETS = 5
AGENT_EGG_PHRASES = [
    "Whoa there, tiger — I'm not an ATM. Come back tomorrow.",
    "Greedy, much? One visit a day. Read a book or something.",
    "Nice try, but I don't do repeat customers. Scram.",
    "You again? Some people just can't take a hint.",
    "Patience isn't your strong suit, huh? Tomorrow. Not now.",
]

@app.get("/api/agent-egg/status")
async def agent_egg_status(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        last = await conn.fetchval(
            "SELECT last_agent_egg_claim FROM users WHERE user_id = $1", user_id
        )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if last and last.tzinfo is not None:
        last = last.replace(tzinfo=None)
    claimed_today = bool(last and (now - last) < timedelta(hours=24))
    return {"claimed_today": claimed_today}

@app.post("/api/agent-egg/claim")
async def claim_agent_egg(request: Request):
    user_id = await require_auth(request)
    await check_rate_limit(request, RATE_WRITE)
    pool = await get_db()
    already_claimed_phrase = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT last_agent_egg_claim, agent_phrases_seen FROM users WHERE user_id = $1 FOR UPDATE",
                user_id
            )
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            last = row["last_agent_egg_claim"]
            if last and last.tzinfo is not None:
                last = last.replace(tzinfo=None)
            if last and (now - last) < timedelta(hours=24):
                idx = shared.secure_randint(0, len(AGENT_EGG_PHRASES) - 1)
                mask = (row["agent_phrases_seen"] or 0) | (1 << idx)
                await conn.execute(
                    "UPDATE users SET agent_phrases_seen = $1 WHERE user_id = $2",
                    mask, user_id
                )
                already_claimed_phrase = AGENT_EGG_PHRASES[idx]
            else:
                await conn.execute(
                    "UPDATE users SET balance = balance + $1, tickets = tickets + $2, last_agent_egg_claim = $3 WHERE user_id = $4",
                    AGENT_EGG_REWARD, AGENT_EGG_TICKETS, now, user_id
                )
        if already_claimed_phrase:
            raise HTTPException(400, already_claimed_phrase)
    return {"success": True, "reward": AGENT_EGG_REWARD, "tickets": AGENT_EGG_TICKETS}

@app.get("/api/cases/featured")
async def get_featured_cases():
    featured = []
    for cid, case in CASES.items():
        eff = await get_effective_case(cid, case["price"], cid in FEATURED_CASES)
        if eff["featured"]:
            featured.append({
                "id": cid, "name": case["name"], "emoji": case.get("emoji", "📦"),
                "price": eff["price"], "original_price": eff["original_price"],
                "on_sale": eff["on_sale"],
            })
    return {"featured": featured}

from shared import CONTAINER_IMAGE_MAP

@app.get("/api/case-image/{case_id}")
async def get_case_image(case_id: str):
    filename = CONTAINER_IMAGE_MAP.get(case_id)
    if not filename:
        return FileResponse("static/images/containers/default.png")
    safe = _safe_static_path("static/images/containers", filename)
    if safe and os.path.exists(safe):
        return FileResponse(safe)
    return FileResponse("static/images/containers/default.png")

@app.get("/api/premium-cases")
async def get_premium_cases():
    """Deprecated — premium cases replaced by VIP ticket system."""
    return {"enabled": False, "message": "Premium cases replaced by VIP subscription. See /api/vip/tiers"}

# ============================================================
# CASES API (existing)
# ============================================================

@app.get("/api/cases")
async def list_cases():
    cases = []
    featured = []
    for k, v in CASES.items():
        eff = await get_effective_case(k, v["price"], k in FEATURED_CASES)
        cases.append({
            "id": k, "name": v["name"], "emoji": v["emoji"],
            "price": eff["price"], "original_price": eff["original_price"],
            "on_sale": eff["on_sale"],
        })
        if eff["featured"]:
            featured.append(k)
    return {"cases": cases, "featured": featured}

@app.get("/api/cases/{case_id}")
async def get_case(case_id: str):
    case = CASES.get(case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    eff = await get_effective_case(case_id, case["price"], case_id in FEATURED_CASES)
    return {
        "id": case_id, **case,
        "price": eff["price"], "original_price": eff["original_price"],
        "on_sale": eff["on_sale"], "featured": eff["featured"],
    }

class OpenCaseRequest(BaseModel):
    case_id: str
    quantity: int = 1
    use_guarantee: bool = False
    use_insurance: bool = False

@app.post("/api/open-case")
async def open_case(req: OpenCaseRequest, request: Request):
    await check_rate_limit(request, RATE_CASE)
    user_id = await require_auth(request)
    case = CASES.get(req.case_id)
    if not case:
        raise HTTPException(400, "Invalid case")

    eff = await get_effective_case(req.case_id, case["price"], req.case_id in FEATURED_CASES)
    unit_price = eff["price"]

    qty = max(1, min(req.quantity, 25))
    discount = {1: 1.0, 5: 0.95, 10: 0.90, 15: 0.85, 20: 0.80, 25: 0.75}.get(qty, 1.0)
    total_cost = round(unit_price * qty * discount, 2)

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            ok = await deduct_balance(user_id, total_cost, conn)
            if not ok:
                raise HTTPException(400, "Insufficient balance")

            # Power-up: Rarity Guarantee — costs 2 tickets, rerolls any Blue result
            if req.use_guarantee:
                from routes.premium import deduct_ticket as _dt
                ok1 = await _dt(user_id, 'spend_case', {'action': 'rarity_guarantee'}, conn)
                ok2 = await _dt(user_id, 'spend_case', {'action': 'rarity_guarantee'}, conn) if ok1 else False
                if not (ok1 and ok2):
                    raise HTTPException(400, "Rarity Guarantee requires 2 tickets")

            # Power-up: Case Insurance — costs 1 ticket, refunds cost of any Blue roll
            if req.use_insurance:
                from routes.premium import deduct_ticket as _dt2
                if not await _dt2(user_id, 'spend_case', {'action': 'case_insurance'}, conn):
                    raise HTTPException(400, "Case Insurance requires 1 ticket")

            items = []
            cost_per_item = round(unit_price * discount, 2)
            for _ in range(qty):
                item = get_random_item(req.case_id)
                if not item:
                    continue
                # Rarity Guarantee: reroll Blues up to 20 times
                if req.use_guarantee and item['rarity'] == 'Blue':
                    for _retry in range(20):
                        rerolled = get_random_item(req.case_id)
                        if rerolled and rerolled['rarity'] != 'Blue':
                            item = rerolled
                            break
                # Insurance: refund if still Blue
                if req.use_insurance and item['rarity'] == 'Blue':
                    await add_balance(user_id, cost_per_item, conn)
                # Build image_url for skins at insert time so inventory can display it later
                skin_img_file = item.get('image_filename')
                skin_img_url = f"/static/images/skins/{skin_img_file}" if skin_img_file else None
                row = await conn.fetchrow("""
                    INSERT INTO inventory
                    (user_id, item_name, item_type, rarity, price, condition,
                     is_stattrak, status, case_id, float_value, image_url)
                    VALUES ($1,$2,'weapon',$3,$4,$5,$6,'kept',$7,$8,$9)
                    RETURNING id
                """, user_id, item["name"], item["rarity"], item["price"],
                    item["condition"], item["is_stattrak"], req.case_id, item["float"],
                    skin_img_url)
                item["id"] = row["id"]
                items.append(item)

                if item["rarity"] == "Gold":
                    await conn.execute(
                        "UPDATE users SET total_golds = total_golds + 1 WHERE user_id = $1",
                        user_id
                    )
                    await conn.execute("""
                        UPDATE quests SET progress = progress + 1
                        WHERE user_id=$1 AND quest_type='get_golds' AND completed=FALSE
                    """, user_id)
                    await conn.execute("""
                        UPDATE quests SET completed=TRUE
                        WHERE user_id=$1 AND quest_type='get_golds' AND progress >= required AND completed=FALSE
                    """, user_id)

            await conn.execute(
                "UPDATE users SET total_opens = total_opens + $1, total_spent = total_spent + $2 WHERE user_id = $3",
                qty, total_cost, user_id
            )
            # Update open_cases and earn_money quest progress
            await conn.execute("""
                UPDATE quests SET progress = progress + $2
                WHERE user_id=$1 AND quest_type='open_cases' AND completed=FALSE
            """, user_id, qty)
            await conn.execute("""
                UPDATE quests SET completed=TRUE
                WHERE user_id=$1 AND quest_type='open_cases' AND progress >= required AND completed=FALSE
            """, user_id)
            item_value = sum(int(it["price"]) for it in items)
            await conn.execute("""
                UPDATE quests SET progress = progress + $2
                WHERE user_id=$1 AND quest_type='earn_money' AND completed=FALSE
            """, user_id, item_value)
            await conn.execute("""
                UPDATE quests SET completed=TRUE
                WHERE user_id=$1 AND quest_type='earn_money' AND progress >= required AND completed=FALSE
            """, user_id)
            # Grant XP: 10 per case, bonus 25 for each gold
            xp_amount = qty * 10
            gold_count = sum(1 for it in items if it.get("rarity") == "Gold")
            xp_amount += gold_count * 25
            await grant_xp(user_id, xp_amount, conn)
            # Update live feed
            if items:
                best = max(items, key=lambda x: x["price"])
                session_data = shared.get_session(request.cookies.get("session_token") or "") or {}
                username = session_data.get("username", "Someone")
                await conn.execute("""
                    INSERT INTO live_feed (user_id, username, item_name, rarity, rarity_emoji, case_type, float_value)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                """, user_id, html.escape(str(username)), best["name"], best["rarity"],
                    RARITY_EMOJIS.get(best["rarity"], ""), req.case_id, best["float"])
                # Trim feed to 100 rows
                await conn.execute("""
                    DELETE FROM live_feed WHERE id NOT IN
                    (SELECT id FROM live_feed ORDER BY created_at DESC LIMIT 100)
                """)

    for it in items:
        it.setdefault("display_name", it.get("name", ""))
    for item in items:
        if item.get('image_filename'):
            item['image_url'] = f"/static/images/skins/{item['image_filename']}"
        else:
            item['image_url'] = None
    return {"success": True, "items": items, "total_cost": total_cost}

# ─── STICKER CAPSULE ──────────────────────────────────────────────
@app.get("/api/capsules")
async def list_capsules():
    capsules = []
    for cid, c in STICKER_CAPSULES.items():
        eff = await get_effective_capsule(cid, c["price"])
        capsules.append({
            "id": cid, "name": c["name"], "emoji": fix_surrogate_emoji(c.get("emoji", "🧷")),
            "image": c.get("image", ""),
            "sticker_count": len(c.get("stickers", [])),
            "price": eff["price"], "featured": eff["featured"],
        })
    return {"capsules": capsules}

class OpenStickerRequest(BaseModel):
    capsule: str
    quantity: int = 1

@app.post("/api/sticker")
async def open_sticker(req: OpenStickerRequest, request: Request):
    await check_rate_limit(request, RATE_CASE)
    user_id = await require_auth(request)
    capsule_id = req.capsule
    if not capsule_id or capsule_id not in STICKER_CAPSULES:
        raise HTTPException(400, "Invalid capsule")

    capsule = STICKER_CAPSULES[capsule_id]
    eff = await get_effective_capsule(capsule_id, capsule['price'])
    unit_price = eff['price']

    # Mirrors /api/open-case's bulk-quantity discount tiers so pricing feels
    # consistent between cases and capsules.
    qty = max(1, min(req.quantity, 25))
    discount = {1: 1.0, 5: 0.95, 10: 0.90, 15: 0.85, 20: 0.80, 25: 0.75}.get(qty, 1.0)
    total_cost = round(unit_price * qty * discount, 2)

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)

            if not await deduct_balance(user_id, total_cost, conn):
                raise HTTPException(400, "Insufficient balance")

            items = []
            for _ in range(qty):
                sticker = get_random_sticker(capsule_id)
                if not sticker:
                    continue

                # Build image_url for sticker so inventory can display it later
                sticker_img = sticker.get('image', '')
                sticker_img_file = sticker_img.split('/')[-1] if sticker_img else ''
                sticker_img_url = f"/static/images/stickers/{sticker_img_file}" if sticker_img_file else None

                row = await conn.fetchrow("""
                    INSERT INTO inventory
                        (user_id, item_name, item_type, rarity, price, is_stattrak, image_url)
                    VALUES ($1, $2, 'sticker', $3, $4, $5, $6)
                    RETURNING id
                """, user_id, sticker['name'], sticker['rarity'], sticker['price'], sticker['is_stattrak'],
                    sticker_img_url)

                sticker['id'] = row['id']   # attach ID for frontend keep/sell
                sticker['image_url'] = sticker_img_url   # frontend reads image_url, not image
                items.append(sticker)

            if not items:
                raise HTTPException(500, "Failed to generate sticker")

            # Track sticker count, spending, and grant XP
            await conn.execute(
                "UPDATE users SET total_stickers = total_stickers + $1, total_spent = total_spent + $2 WHERE user_id = $3",
                len(items), total_cost, user_id
            )
            await grant_xp(user_id, len(items) * 5, conn)

            new_balance = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", user_id)

            return {
                "success": True,
                "items": items,
                "total_cost": total_cost,
                "new_balance": float(new_balance)
            }

@app.post("/api/sell-item")
async def sell_item(request: Request):
    await check_rate_limit(request, RATE_WRITE)
    body     = await request.json()
    user_id  = await require_auth(request)
    item_id  = body.get("item_id")

    # Validate item_id
    if item_id is None:
        raise HTTPException(400, "item_id is required")
    try:
        item_id = int(item_id)
    except (ValueError, TypeError):
        raise HTTPException(400, "item_id must be an integer")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Atomic ownership check + status transition in one statement so
            # concurrent sell requests can't both pass and double-credit balance.
            item = await conn.fetchrow(
                "UPDATE inventory SET status='sold' WHERE id=$1 AND user_id=$2 AND status='kept' AND protected=FALSE RETURNING price",
                item_id, user_id
            )
            if not item:
                is_protected = await conn.fetchval(
                    "SELECT protected FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept'", item_id, user_id
                )
                if is_protected:
                    raise HTTPException(400, "This item is protected — unprotect it first to sell")
                raise HTTPException(404, "Item not found or already sold")
            sell_price = round(float(item["price"]) * 0.70, 2)
            await add_balance(user_id, sell_price, conn)
            # Update quest progress for selling
            await conn.execute("""
                UPDATE quests SET progress = progress + 1
                WHERE user_id=$1 AND quest_type='sell_items' AND completed=FALSE
            """, user_id)
            await conn.execute("""
                UPDATE quests SET completed=TRUE
                WHERE user_id=$1 AND quest_type='sell_items' AND progress >= required AND completed=FALSE
            """, user_id)
    return {"success": True, "sell_price": sell_price}

@app.post("/api/sell-items")
async def sell_items(request: Request):
    await check_rate_limit(request, RATE_WRITE)
    body = await request.json()
    user_id = await require_auth(request)
    item_ids = body.get("item_ids")

    if not isinstance(item_ids, list) or not item_ids:
        raise HTTPException(400, "item_ids must be a non-empty list")
    if len(item_ids) > 200:
        raise HTTPException(400, "Too many items in one request (max 200)")
    try:
        item_ids = [int(i) for i in item_ids]
    except (ValueError, TypeError):
        raise HTTPException(400, "item_ids must be integers")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Atomic ownership check + status transition, same as single-item
            # sell — anything already sold, not owned, or protected is
            # silently excluded rather than failing the whole batch.
            rows = await conn.fetch(
                "UPDATE inventory SET status='sold' WHERE id = ANY($1) AND user_id=$2 AND status='kept' AND protected=FALSE RETURNING id, price",
                item_ids, user_id
            )
            if not rows:
                raise HTTPException(404, "No matching items found to sell")
            total_sell_price = round(sum(float(r["price"]) for r in rows) * 0.70, 2)
            sold_count = len(rows)
            await add_balance(user_id, total_sell_price, conn)
            await conn.execute("""
                UPDATE quests SET progress = progress + $2
                WHERE user_id=$1 AND quest_type='sell_items' AND completed=FALSE
            """, user_id, sold_count)
            await conn.execute("""
                UPDATE quests SET completed=TRUE
                WHERE user_id=$1 AND quest_type='sell_items' AND progress >= required AND completed=FALSE
            """, user_id)
    return {
        "success": True,
        "sold_count": sold_count,
        "total_sell_price": total_sell_price,
        "sold_ids": [r["id"] for r in rows],
    }

@app.post("/api/keep-item")
async def keep_item(request: Request):
    body = await request.json()
    user_id = await require_auth(request)
    item_id = body.get("item_id")
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # FOR UPDATE prevents two concurrent keep-item requests from both
            # passing the ownership check on the same item.
            item = await conn.fetchrow(
                "SELECT id FROM inventory WHERE id=$1 AND user_id=$2 FOR UPDATE", item_id, user_id
            )
            if not item:
                raise HTTPException(404, "Item not found")
            await conn.execute("UPDATE inventory SET status='kept' WHERE id=$1", item_id)
            # Track inventory size for achievements (atomic with the status update)
            await conn.execute(
                "UPDATE users SET total_inventory_items = (SELECT COUNT(*) FROM inventory WHERE user_id=$1 AND status='kept') WHERE user_id=$1",
                user_id
            )
    return {"success": True}

# ============================================================
# QUESTS
# ============================================================

@app.get("/api/quests")
async def get_quests(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        await ensure_user_exists(user_id, conn=conn)
        async with conn.transaction():
            # Lock the user row so concurrent GET /api/quests requests serialise
            # here and only one of them performs the daily reset.
            await conn.fetchval("SELECT 1 FROM users WHERE user_id=$1 FOR UPDATE", user_id)
            needs_refresh = await conn.fetchval("""
                SELECT NOT EXISTS (
                    SELECT 1 FROM quests
                    WHERE user_id=$1 AND created_at::date = NOW()::date
                )
            """, user_id)
            if needs_refresh:
                await conn.execute("DELETE FROM quests WHERE user_id=$1", user_id)
                for qt, qi in QUEST_TYPES.items():
                    await conn.execute("""
                        INSERT INTO quests (user_id, quest_type, progress, required, reward)
                        VALUES ($1,$2,0,$3,$4)
                    """, user_id, qt, qi["base_required"], qi["base_reward"])
            rows = await conn.fetch(
                "SELECT * FROM quests WHERE user_id=$1 ORDER BY created_at", user_id
            )
    return {"quests": [dict(r) for r in rows]}

@app.post("/api/claim")
async def claim_quests(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Atomically claim and return rewards in one statement to prevent
            # double-payout from concurrent requests (TOCTOU race).
            claimed = await conn.fetch(
                """UPDATE quests SET claimed=true
                   WHERE user_id=$1 AND completed=true AND claimed=false
                   RETURNING reward""",
                user_id
            )
            if not claimed:
                raise HTTPException(400, "No quests ready to claim")
            total      = sum(r["reward"] for r in claimed)
            num_quests = len(claimed)
            await add_balance(user_id, total, conn)
            # Track quests completed and grant XP (50 XP per quest)
            await conn.execute(
                "UPDATE users SET total_quests_completed = total_quests_completed + $1 WHERE user_id = $2",
                num_quests, user_id
            )
            await grant_xp(user_id, num_quests * 50, conn)
    return {"success": True, "total_reward": total, "message": f"Claimed ${total:,.0f}!"}

# ============================================================
# TRADE-UP
# ============================================================

class TradeRequest(BaseModel):
    rarity: str
    item_ids: List[int]

@app.post("/api/quick-trade")
async def quick_trade(req: TradeRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)
    rarity_config = {
        "Blue":   {"count": 10, "next": "Purple"},
        "Purple": {"count": 10, "next": "Pink"},
        "Pink":   {"count": 10, "next": "Red"},
        "Red":    {"count": 5,  "next": "Gold"},
    }
    cfg = rarity_config.get(req.rarity)
    if not cfg:
        raise HTTPException(400, "Invalid rarity")
    if len(req.item_ids) != cfg["count"]:
        raise HTTPException(400, f"Need exactly {cfg['count']} items")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Verify ownership and lock all rows before deletion
            rows = await conn.fetch(
                "SELECT id, tier FROM inventory WHERE id = ANY($1::int[]) AND user_id=$2 AND rarity=$3 AND status='kept' AND protected=FALSE FOR UPDATE",
                req.item_ids, user_id, req.rarity
            )
            if len(rows) != len(req.item_ids):
                raise HTTPException(400, "One or more items not valid, not owned, or protected")
            # Delete traded items
            await conn.execute(
                "DELETE FROM inventory WHERE id = ANY($1::int[])", req.item_ids
            )
            # Generate new item
            if req.rarity == "Red":
                # Red → Gold: pick a real glove/knife from the global Gold pool
                gold_pool = shared.GOLD_ITEMS_POOL
                if gold_pool:
                    template = shared.secure_choice(gold_pool)
                    is_st = shared.secure_random() < 0.1
                    fv = generate_skin_float()
                    cond = get_skin_condition(fv)
                    price = calculate_item_value("Gold", cond, None, is_st)
                    trade_img_file = os.path.basename(template.get("skin_image") or "") if template.get("skin_image") else None
                    trade_img_url = f"/static/images/skins/{trade_img_file}" if trade_img_file else None
                    new_item = {
                        "name": f"{'StatTrak™ ' if is_st else ''}{template['name']}",
                        "rarity": "Gold", "condition": cond,
                        "tier": None, "is_stattrak": is_st,
                        "float": fv, "price": price, "image_url": trade_img_url,
                    }
                else:
                    new_item = {
                        "name": "Mystery Gold Common",
                        "rarity": "Gold", "condition": "Factory New",
                        "tier": "Common", "is_stattrak": False, "float": 0.0,
                        "price": float(GOLD_VALUES.get("Common", 150)),
                    }
            else:
                next_rarity = cfg["next"]
                possible = ALL_ITEMS_BY_RARITY.get(next_rarity, [])
                if not possible:
                    raise HTTPException(400, f"No items available for rarity {next_rarity}")
                template = shared.secure_choice(possible)
                is_st = shared.secure_random() < 0.1
                fv = generate_skin_float()
                cond = get_skin_condition(fv)
                price = calculate_item_value(next_rarity, cond, template.get("tier"), is_st)
                name = f"{'StatTrak™ ' if is_st else ''}{template['name']}"
                # Build image_url so inventory displays correctly
                trade_img_file = os.path.basename(template.get("skin_image") or "") if template.get("skin_image") else None
                trade_img_url = f"/static/images/skins/{trade_img_file}" if trade_img_file else None
                new_item = {
                    "name": name, "rarity": next_rarity, "condition": cond,
                    "tier": template.get("tier"), "is_stattrak": is_st,
                    "float": fv, "price": price, "image_url": trade_img_url,
                }
            row = await conn.fetchrow("""
                INSERT INTO inventory (user_id, item_name, item_type, rarity, price, condition,
                    is_stattrak, status, float_value, image_url, tier)
                VALUES ($1,$2,'weapon',$3,$4,$5,$6,'kept',$7,$8,$9) RETURNING id
            """, user_id, new_item["name"], new_item["rarity"], new_item["price"],
                new_item["condition"], new_item["is_stattrak"], new_item.get("float", 0.0),
                new_item.get("image_url"), new_item.get("tier"))
            new_item["id"] = row["id"]
            await conn.execute(
                "UPDATE users SET total_trades=total_trades+1 WHERE user_id=$1", user_id
            )
            await conn.execute("""
                UPDATE quests SET progress = progress + 1
                WHERE user_id=$1 AND quest_type='trade_up' AND completed=FALSE
            """, user_id)
            await conn.execute("""
                UPDATE quests SET completed=TRUE
                WHERE user_id=$1 AND quest_type='trade_up' AND progress >= required AND completed=FALSE
            """, user_id)
    return {"success": True, "new_item": new_item,
            "message": f"Traded {cfg['count']} {req.rarity} → {new_item['name']}!"}

# ============================================================
# SKIN UPGRADE
# ============================================================
@app.post("/api/skin-upgrade")
async def skin_upgrade_endpoint(request: Request):
    await check_rate_limit(request, RATE_WRITE)
    body = await request.json()
    try:
        user_id = await require_auth(request)
        item_id = body.get("item_id")
        rarity_order = ["Blue", "Purple", "Pink", "Red", "Gold"]
        upgrade_cost  = {"Blue": 10, "Purple": 50, "Pink": 200, "Red": 1000}
        success_odds  = {"Blue": 0.80, "Purple": 0.60, "Pink": 0.40, "Red": 0.25}

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                item = await conn.fetchrow(
                    "SELECT * FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept' FOR UPDATE",
                    item_id, user_id
                )
                if not item:
                    raise HTTPException(404, "Item not found")
                if item["protected"]:
                    raise HTTPException(400, "This item is protected — unprotect it first to upgrade")
                if item["item_type"] != "weapon":
                    raise HTTPException(400, "Only weapon items can be upgraded")
                if item["rarity"] == "Gold":
                    raise HTTPException(400, "Gold items can't be upgraded")

                idx = rarity_order.index(item["rarity"])
                next_rarity = rarity_order[idx + 1]
                cost = upgrade_cost.get(item["rarity"], 10)

                ok = await deduct_balance(user_id, cost, conn)
                if not ok:
                    raise HTTPException(400, f"Need ${cost} to upgrade")

                await conn.execute("DELETE FROM inventory WHERE id=$1", item_id)

                success = shared.secure_random() < success_odds.get(item["rarity"], 0.5)

                if success:
                    possible = ALL_ITEMS_BY_RARITY.get(next_rarity, [])
                    if not possible:
                        raise HTTPException(400, f"No items available for rarity {next_rarity}")
                    template = shared.secure_choice(possible)
                    is_st = shared.secure_random() < 0.1
                    fv = generate_skin_float()
                    cond = get_skin_condition(fv)
                    price = calculate_item_value(next_rarity, cond, template.get("tier"), is_st)
                    name = f"{'StatTrak™ ' if is_st else ''}{template['name']}"
                    # Build image_url so inventory displays correctly
                    upgrade_img_file = os.path.basename(template.get("skin_image") or "") if template.get("skin_image") else None
                    upgrade_img_url = f"/static/images/skins/{upgrade_img_file}" if upgrade_img_file else None

                    await conn.execute("""
                        INSERT INTO inventory
                            (user_id, item_name, item_type, rarity, price,
                             condition, is_stattrak, status, float_value, image_url, tier)
                        VALUES ($1,$2,'weapon',$3,$4,$5,$6,'kept',$7,$8,$9)
                    """, user_id, name, next_rarity, price, cond, is_st, fv, upgrade_img_url,
                        template.get("tier"))

                    await conn.execute("""
                        INSERT INTO skin_upgrades
                            (user_id, item_id, input_rarity, output_rarity, success)
                        VALUES ($1,$2,$3,$4,true)
                    """, user_id, item_id, item["rarity"], next_rarity)

                    return {
                        "success": True,
                        "upgraded": True,
                        "new_rarity": next_rarity,
                        "new_item_name": name,
                        "new_price": price
                    }
                else:
                    await conn.execute("""
                        INSERT INTO skin_upgrades
                            (user_id, item_id, input_rarity, output_rarity, success)
                        VALUES ($1,$2,$3,$4,false)
                    """, user_id, item_id, item["rarity"], next_rarity)

                    return {
                        "success": True,
                        "upgraded": False,
                        "old_item_name": item["item_name"],
                        "cost": cost
                    }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Skin upgrade error")
        raise HTTPException(500, f"Upgrade error: {str(e)}")

# ============================================================
# BALANCE / CLAIMS
# ============================================================

@app.get("/api/balance")
async def get_balance_endpoint(request: Request):
    user_id = await require_auth(request)
    bal = await get_user_balance(user_id)
    return {"balance": bal}

@app.post("/api/hourly")
async def claim_hourly(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                "SELECT last_hourly, total_hourly_claimed FROM users WHERE user_id=$1 FOR UPDATE",
                user_id
            )
            if not user:
                raise HTTPException(404, "User not found")
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            last_h = user["last_hourly"]
            if last_h and last_h.tzinfo is not None:
                last_h = last_h.replace(tzinfo=None)
            if last_h and (now - last_h).total_seconds() < 3600:
                remaining = int(3600 - (now - last_h).total_seconds())
                raise HTTPException(400, f"Next claim in {remaining // 60}m {remaining % 60}s")
            total = (user["total_hourly_claimed"] or 0) + 1
            reward = 75 + (250 if total % 10 == 0 else 0)
            await conn.execute("""
                UPDATE users SET balance=balance+$1, last_hourly=$2, total_hourly_claimed=$3
                WHERE user_id=$4
            """, reward, now, total, user_id)
    return {"success": True, "reward": reward, "total_claimed": total}

@app.post("/api/weekly")
async def claim_weekly(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                "SELECT last_weekly, total_weekly_claimed FROM users WHERE user_id=$1 FOR UPDATE",
                user_id
            )
            if not user:
                raise HTTPException(404, "User not found")
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            last_w = user["last_weekly"]
            if last_w and last_w.tzinfo is not None:
                last_w = last_w.replace(tzinfo=None)
            if last_w and (now - last_w).total_seconds() < 604800:
                remaining = int(604800 - (now - last_w).total_seconds())
                days = remaining // 86400
                hrs  = (remaining % 86400) // 3600
                raise HTTPException(400, f"Next claim in {days}d {hrs}h")
            total = (user["total_weekly_claimed"] or 0) + 1
            reward = 5000
            await conn.execute("""
                UPDATE users SET balance=balance+$1, last_weekly=$2, total_weekly_claimed=$3
                WHERE user_id=$4
            """, reward, now, total, user_id)
    return {"success": True, "reward": reward, "total_claimed": total}

# ============================================================
# LEADERBOARD
# ============================================================

@app.get("/api/leaderboard/game-wins")
async def leaderboard_game_wins(game: str, limit: int = 10):
    VALID_GAMES = {
        "coinflip", "dice", "limbo", "hilo", "dragon_tiger", "keno",
        "crash", "mines", "plinko", "tower", "shotgun", "ladder",
        "roulette", "slide", "mystery_box", "russian_roulette",
        "baccarat", "blackjack", "live_race",
        "slots_classic", "slots_cs2", "slots_jackpot", "slots_bomb",
        # Multiplayer PvP games (Sessions 1-9) -- newly wired into game_logs
        # this session (Session 10 Part A2), or already logging but missing
        # from this set.
        "dice_duel", "weapon_duel", "reaction_duel", "case_draft_duel",
        "item_wager_duel", "item_trade_up_duel", "case_battles",
        "ladder_race", "mines_race", "speed_case_race",
        "skin_bingo", "live_case_auction", "battle_royale_mines",
        "live_keno", "live_roulette", "live_blackjack", "koth_ladder", "sync_slots",
    }
    if game not in VALID_GAMES:
        raise HTTPException(400, "Invalid game type")
    limit = max(1, min(limit, 100))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.user_id, u.username, COUNT(*) AS wins
            FROM game_logs gl
            JOIN users u ON gl.user_id = u.user_id
            WHERE gl.game_type = $1 AND gl.result = 'win' AND u.user_id > 0
            GROUP BY u.user_id, u.username
            ORDER BY wins DESC
            LIMIT $2
        """, game, limit)
    return {"users": [dict(r) for r in rows]}


@app.get("/api/leaderboard/{board_type}")
async def leaderboard(board_type: str, limit: int = 10):
    col_map = {
        "money":   ("balance",           "💰"),
        "opens":   ("total_opens",       "📦"),
        "golds":   ("total_golds",       "⭐"),
        "trades":  ("total_trades",      "🔄"),
        "games":   ("total_games_played","🎮"),
        "tickets": ("tickets",           "🎟️"),
        "streak":  ("win_streak",        "🔥"),
    }
    limit = max(1, min(limit, 100))

    pool = await get_db()
    async with pool.acquire() as conn:
        if board_type == "profit":
            rows = await conn.fetch("""
                SELECT u.user_id, u.username,
                       COALESCE(SUM(gl.win_amount - gl.bet_amount), 0) AS value
                FROM users u
                LEFT JOIN game_logs gl ON gl.user_id = u.user_id
                WHERE u.user_id > 0
                GROUP BY u.user_id, u.username
                ORDER BY value DESC
                LIMIT $1
            """, limit)
        elif board_type == "ticket_wins":
            rows = await conn.fetch("""
                SELECT u.user_id, u.username,
                       COALESCE(SUM(tg.tickets_won), 0) AS value
                FROM users u
                LEFT JOIN ticket_games tg ON tg.user_id = u.user_id
                    AND tg.status = 'completed'
                WHERE u.user_id > 0
                GROUP BY u.user_id, u.username
                ORDER BY value DESC
                LIMIT $1
            """, limit)
        elif board_type in col_map:
            col = col_map[board_type][0]
            col_ident = await conn.fetchval("SELECT quote_ident($1)", col)
            rows = await conn.fetch(
                f"SELECT user_id, username, {col_ident} AS value FROM users "
                f"WHERE user_id > 0 ORDER BY {col_ident} DESC LIMIT $1",
                limit
            )
        else:
            raise HTTPException(400, "Invalid leaderboard type")

    return {"users": [convert_decimals(dict(r)) for r in rows]}


@app.get("/api/stats/wins-today")
async def stats_wins_today():
    """Real replacement for games.html's Math.random()-simulated 'Wins
    Today' pill -- an honest count now that Session 10 wired game_logs
    logging into the 10 PvP games that were previously silent."""
    pool = await get_db()
    async with pool.acquire() as conn:
        wins_today = await conn.fetchval(
            "SELECT COUNT(*) FROM game_logs WHERE result='win' AND created_at >= CURRENT_DATE"
        )
    return {"wins_today": int(wins_today or 0)}

# ============================================================
# STATS & STREAKS
# ============================================================

@app.get("/api/stats")
async def get_stats(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        streak = await conn.fetchrow(
            "SELECT * FROM user_streaks WHERE user_id=$1", user_id
        )
    if not user:
        raise HTTPException(404, "User not found")
    return {
        "total_opens":        int(user["total_opens"] or 0),
        "total_golds":        int(user["total_golds"] or 0),
        "total_trades":       int(user["total_trades"] or 0),
        "total_games_played": int(user["total_games_played"] or 0),
        "level":              int(user["level"] or 1),
        "xp":                 int(user["xp"] or 0),
        "prestige":           int(user["prestige"] or 0),
        "win_streak":         int(user["win_streak"] or 0),
        "current_streak":     int(streak["current_streak"] if streak else 0),
        "best_streak":        int(streak["best_streak"] if streak else 0),
    }

@app.post("/api/user/streak/update")
async def update_streak(request: Request):
    body = await request.json()
    user_id = await require_auth(request)
    is_gold = body.get("is_gold", False)
    gold_inc = 1 if is_gold else 0
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO user_streaks (user_id, current_streak, best_streak, golds_in_streak)
            VALUES ($1, $3, $3, $3)
            ON CONFLICT (user_id) DO UPDATE SET
                current_streak = CASE WHEN $2 THEN user_streaks.current_streak + 1 ELSE 0 END,
                best_streak    = CASE WHEN $2 THEN GREATEST(user_streaks.best_streak,
                                          user_streaks.current_streak + 1)
                                      ELSE user_streaks.best_streak END,
                golds_in_streak = CASE WHEN $2 THEN user_streaks.golds_in_streak + $3
                                       ELSE 0 END,
                updated_at     = NOW()
            RETURNING current_streak
        """, user_id, is_gold, gold_inc)
        new_streak = row["current_streak"]
    return {"current_streak": new_streak}

# ============================================================
# LIVE FEED
# ============================================================

@app.get("/api/live-feed")
async def live_feed(limit: int = 20):
    limit = max(1, min(limit, 100))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM live_feed ORDER BY created_at DESC LIMIT $1", limit
        )
    return [dict(r) for r in rows]


@app.get("/api/lobby-ticker")
async def lobby_ticker(limit: int = 20):
    """Real lobby ticker — recent notable events from all games & case opens."""
    limit = max(1, min(limit, 50))
    pool = await get_db()
    events = []

    async with pool.acquire() as conn:
        # 1. Crash cashouts with high multipliers
        rows = await conn.fetch("""
            SELECT gl.user_id, u.username, gl.win_amount, gl.multiplier,
                   gl.created_at, 'crash' AS event_type
            FROM game_logs gl
            JOIN users u ON u.user_id = gl.user_id
            WHERE gl.game_type = 'crash' AND gl.win_amount > 0 AND gl.multiplier >= 2.0
            ORDER BY gl.created_at DESC LIMIT $1
        """, limit)
        for r in rows:
            events.append({
                "type": "crash",
                "username": r["username"],
                "multiplier": float(r["multiplier"]),
                "win_amount": float(r["win_amount"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
            })

        # 2. Slots big wins
        rows = await conn.fetch("""
            SELECT gl.user_id, u.username, gl.win_amount, gl.multiplier,
                   gl.created_at, 'slots' AS event_type
            FROM game_logs gl
            JOIN users u ON u.user_id = gl.user_id
            WHERE gl.game_type = 'slots' AND gl.win_amount >= 500
            ORDER BY gl.created_at DESC LIMIT $1
        """, limit)
        for r in rows:
            events.append({
                "type": "slots",
                "username": r["username"],
                "win_amount": float(r["win_amount"]),
                "multiplier": float(r["multiplier"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
            })

        # 3. Mines wins (safe tiles cleared)
        rows = await conn.fetch("""
            SELECT gl.user_id, u.username, gl.win_amount, gl.multiplier,
                   gl.created_at, gl.meta, 'mines' AS event_type
            FROM game_logs gl
            JOIN users u ON u.user_id = gl.user_id
            WHERE gl.game_type = 'mines' AND gl.result = 'win'
            ORDER BY gl.created_at DESC LIMIT $1
        """, limit)
        for r in rows:
            meta_raw = r["meta"]
            if isinstance(meta_raw, str):
                meta = json.loads(meta_raw) if meta_raw else {}
            else:
                meta = meta_raw or {}
            events.append({
                "type": "mines",
                "username": r["username"],
                "win_amount": float(r["win_amount"]),
                "multiplier": float(r["multiplier"]),
                "tiles_cleared": meta.get("tiles_cleared", meta.get("revealed_count", 0)),
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
            })

        # 4. Case opens — rare or gold drops from live_feed
        rows = await conn.fetch("""
            SELECT lf.username, lf.item_name, lf.rarity, lf.rarity_emoji,
                   lf.created_at, lf.float_value
            FROM live_feed lf
            WHERE lf.rarity IN ('Gold', 'Red', 'Pink')
            ORDER BY lf.created_at DESC LIMIT $1
        """, limit)
        for r in rows:
            events.append({
                "type": "case_open",
                "username": r["username"] or "Player",
                "item_name": r["item_name"],
                "rarity": r["rarity"],
                "rarity_emoji": r["rarity_emoji"] or "",
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
            })

        # 5. Coinflip / dice wins
        for gt in ("coinflip", "dice"):
            rows = await conn.fetch("""
                SELECT gl.user_id, u.username, gl.win_amount, gl.multiplier,
                       gl.created_at
                FROM game_logs gl
                JOIN users u ON u.user_id = gl.user_id
                WHERE gl.game_type = $1 AND gl.win_amount >= 1000
                ORDER BY gl.created_at DESC LIMIT $2
            """, gt, limit)
            for r in rows:
                events.append({
                    "type": f"{gt}_win",
                    "username": r["username"],
                    "win_amount": float(r["win_amount"]),
                    "multiplier": float(r["multiplier"]),
                    "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                })

    # Sort all events by recency, take top N
    events.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return events[:limit]

# ============================================================
# INVENTORY VALUE HISTORY
# ============================================================

@app.get("/api/profile/value-history")
async def profile_value_history(request: Request):
    user_id = await get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT value, item_count, snapped_at
            FROM inventory_value_snapshots
            WHERE user_id = $1
            ORDER BY snapped_at ASC
            LIMIT 30
        """, user_id)
    return [convert_decimals(dict(r)) for r in rows]

async def _snapshot_inventory_value(user_id: int, pool):
    """Take a daily inventory value snapshot for the given user."""
    try:
        async with pool.acquire() as conn:
            # Only once per 20 hours to avoid daily clock drift issues
            recent = await conn.fetchval("""
                SELECT 1 FROM inventory_value_snapshots
                WHERE user_id = $1 AND snapped_at > NOW() - INTERVAL '20 hours'
                LIMIT 1
            """, user_id)
            if recent:
                return
            row = await conn.fetchrow("""
                SELECT COALESCE(SUM(price), 0) AS total_value,
                       COUNT(*) AS item_count
                FROM inventory
                WHERE user_id = $1 AND status = 'kept'
            """, user_id)
            if row:
                await conn.execute("""
                    INSERT INTO inventory_value_snapshots (user_id, value, item_count)
                    VALUES ($1, $2, $3)
                """, user_id, row['total_value'], row['item_count'])
    except Exception as e:
        logger.debug(f"Snapshot error for {user_id}: {e}")

# ============================================================
# SKIN IMAGE
# ============================================================

from shared import get_skin_image_filename
@app.get("/api/skin-image")
async def get_skin_image(name: str):
    filename = get_skin_image_filename(name)
    if filename:
        allowed_dirs = ["static/images/skins", "CS2-Simulator/assets/skins"]
        for base_dir in allowed_dirs:
            safe = _safe_static_path(base_dir, filename)
            if safe and os.path.exists(safe):
                return FileResponse(safe)

    default = "static/images/default_skin.png"
    if os.path.exists(default):
        return FileResponse(default)

    raise HTTPException(404, "Skin image not found")

# ============================================================
# GAMES CATALOG (for hub page)
# ============================================================

@app.get("/api/games/catalog")
async def games_catalog():
    return GAME_CATALOG

# ============================================================
# PREMIUM / TICKETS
# ============================================================

@app.get("/api/premium-status")
async def premium_status():
    return {"enabled": STRIPE_ENABLED, "message": "Premium features require payment setup"}

@app.get("/api/ticket-balance")
async def ticket_balance(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT tickets FROM users WHERE user_id=$1", user_id
        )
    return {"tickets": int(val or 0)}

# ============================================================
# GOALS / DONATION TRACKER
# ============================================================

@app.get("/api/goals")
async def get_goals():
    pool = await get_db()
    async with pool.acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE user_id > 0")
        donated = await conn.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM donations WHERE status='completed'"
        )
    return {
        "users":     int(users or 0),
        "donations": float(donated or 0),
    }

# ============================================================
# ADMIN ROUTES
# ============================================================

@app.get("/api/admin/stats")
async def admin_stats(request: Request, _=Depends(require_admin)):
    pool = await get_db()
    async with pool.acquire() as conn:
        users     = await conn.fetchval("SELECT COUNT(*) FROM users WHERE user_id > 0")
        eco       = await conn.fetchval("SELECT COALESCE(SUM(balance),0) FROM users WHERE user_id > 0")
        opens     = await conn.fetchval("SELECT COALESCE(SUM(total_opens),0) FROM users")
        golds     = await conn.fetchval("SELECT COALESCE(SUM(total_golds),0) FROM users")
        inv_value = await conn.fetchval("SELECT COALESCE(SUM(price),0) FROM inventory WHERE status='kept'")
    return {
        "total_users":       int(users or 0),
        "total_economy":     float(eco or 0),
        "total_opens":       int(opens or 0),
        "total_golds":       int(golds or 0),
        "total_inv_value":   float(inv_value or 0),
        "sessions_active":   len(shared.sessions),
    }

@app.post("/api/admin/give-balance")
async def admin_give_balance(request: Request, _=Depends(require_admin)):
    body = await request.json()
    target_id = int(body.get("user_id", 0))
    amount    = float(body.get("amount", 0))
    if not target_id or amount <= 0:
        raise HTTPException(400, "Invalid params")
    pool = await get_db()
    async with pool.acquire() as conn:
        await add_balance(target_id, amount, conn)
    return {"success": True}

@app.post("/api/admin/reset-balance")
async def admin_reset_balance(request: Request, _=Depends(require_admin)):
    body = await request.json()
    target_id = int(body.get("user_id", 0))
    # Bug 185 fix: floor at 0 so an admin can't accidentally set a negative
    # balance, which would lock the user out of all game/case-open endpoints.
    amount    = max(0.0, float(body.get("amount", 1000)))
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET balance=$1 WHERE user_id=$2", amount, target_id
        )
    return {"success": True}

# ============================================================
# STRIPE WEBHOOK
# ============================================================

@app.post("/webhook/stripe")
async def stripe_webhook_legacy(request: Request):
    """Forwarded to routes.premium stripe_webhook — this stub kept for router compatibility."""
    try:
        from routes.premium import stripe_webhook as _premium_webhook
        return await _premium_webhook(request)
    except Exception:
        raise HTTPException(503, "Stripe not configured")

# ─── Missing admin settings endpoint ──────────────────────────
@app.get("/api/admin/settings")
async def admin_settings_public(request: Request):
    """Public fallback for admin/settings – returns safe defaults for non‑admins."""
    user_id = await get_user_id_from_session(request)
    if not user_id or user_id not in ADMIN_USER_IDS:
        return {"settings": {"maintenance_mode": "false", "maintenance_message": "We'll be back soon!"}}
    # For admins, fetch real settings from DB if table exists
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM admin_settings")
        settings = {r["key"]: r["value"] for r in rows}
        defaults = {
            "site_name": "CS2CaseBot",
            "default_currency": "$",
            "support_discord_link": "https://discord.gg/mU33pc7TDE",
            "maintenance_mode": "false",
            "maintenance_message": "We'll be back soon!",
        }
        defaults.update(settings)
    return {"settings": defaults}

# ─── Alias for quests (frontend calls /api/user/me/quests) ──
@app.get("/api/user/me/quests")
async def get_quests_alias(request: Request):
    """Alias for /api/quests – frontend compatibility."""
    return await get_quests(request)

# ─── Ticket purchases removed -- tickets are earned only, never bought with
# real money (see routes/premium.py). This legacy /api/buy-tickets alias
# forwarded to the now-deleted tickets_buy() endpoint, so it's gone too. ──

# ============================================================
# MISSING ALIAS ROUTES  (index.html calls these paths)
# ============================================================

# Games in index.html tab use /api/games/* paths — alias to real routes
@app.post("/api/games/hourly")
async def games_hourly_alias(request: Request):
    """Alias for /api/hourly — index.html games tab."""
    return await claim_hourly(request)

@app.post("/api/games/weekly")
async def games_weekly_alias(request: Request):
    """Alias for /api/weekly — index.html games tab."""
    return await claim_weekly(request)

@app.get("/api/games/stats")
async def games_stats(request: Request):
    """Game W/L stats for the index.html games tab."""
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT coinflip_wins, dice_wins, mines_wins, slots_wins FROM users WHERE user_id=$1",
            user_id
        )
        # Count losses from game_logs
        cf_loss = await conn.fetchval(
            "SELECT COUNT(*) FROM game_logs WHERE user_id=$1 AND game_type='coinflip' AND result='loss'", user_id
        ) or 0
        dice_loss = await conn.fetchval(
            "SELECT COUNT(*) FROM game_logs WHERE user_id=$1 AND game_type='dice' AND result='loss'", user_id
        ) or 0
        mines_loss = await conn.fetchval(
            "SELECT COUNT(*) FROM game_logs WHERE user_id=$1 AND game_type='mines' AND result='loss'", user_id
        ) or 0
        slots_loss = await conn.fetchval(
            "SELECT COUNT(*) FROM game_logs WHERE user_id=$1 AND game_type='slots' AND result='loss'", user_id
        ) or 0
    if not user:
        return {"coinflip": {"wins": 0, "losses": 0}, "dice": {"wins": 0, "losses": 0},
                "mines": {"wins": 0, "losses": 0}, "slots": {"wins": 0, "losses": 0}}
    return {
        "coinflip": {"wins": int(user["coinflip_wins"] or 0), "losses": int(cf_loss)},
        "dice":     {"wins": int(user["dice_wins"] or 0),     "losses": int(dice_loss)},
        "mines":    {"wins": int(user["mines_wins"] or 0),    "losses": int(mines_loss)},
        "slots":    {"wins": int(user["slots_wins"] or 0),    "losses": int(slots_loss)},
    }

@app.post("/api/games/coinflip/create")
async def coinflip_create(request: Request):
    """Coinflip game in index.html — simple PvC."""
    body = await request.json()
    user_id = await require_auth(request)
    # Bug 182 fix: clamp to max bet like all route-file games (MAX_BET = 750_000).
    amount = max(10.0, min(750_000.0, float(body.get("amount", 100))))
    if amount < 10:
        raise HTTPException(400, "Minimum bet is $10")
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, amount, conn):
                raise HTTPException(400, "Insufficient balance")
            user_wins = shared.secure_random() < 0.5
            if user_wins:
                win = round(amount * 1.9, 2)   # 5% house edge
                await add_balance(user_id, win, conn)
                await conn.execute("UPDATE users SET coinflip_wins=coinflip_wins+1 WHERE user_id=$1", user_id)
            else:
                win = 0
            await conn.execute("""
                INSERT INTO game_logs (user_id, game_type, bet_amount, win_amount, multiplier, result)
                VALUES ($1,'coinflip',$2,$3,$4,$5)
            """, user_id, amount, win, 1.9 if user_wins else 0, 'win' if user_wins else 'loss')
    return {"success": True, "user_wins": user_wins, "amount": amount, "win": win}

@app.post("/api/games/dice/play")
async def dice_play(request: Request):
    """Dice game in index.html."""
    body = await request.json()
    user_id = await require_auth(request)
    amount     = max(10.0, min(750_000.0, float(body.get("amount", 100))))
    bet_type   = body.get("bet_type", "over")   # 'over' | 'under' | 'exact'
    bet_number = int(body.get("bet_number", 7))
    if amount < 10:
        raise HTTPException(400, "Minimum bet is $10")
    # Dice roll is 2-12 (11 values). Validate bet_number per type to prevent
    # trivially-guaranteed wins (e.g. bet_number=1 "over" always wins).
    if bet_type == "over":
        if not (2 <= bet_number <= 11):
            raise HTTPException(400, "For 'over', bet_number must be 2–11")
    elif bet_type == "under":
        if not (3 <= bet_number <= 12):
            raise HTTPException(400, "For 'under', bet_number must be 3–12")
    elif bet_type == "exact":
        if not (2 <= bet_number <= 12):
            raise HTTPException(400, "For 'exact', bet_number must be 2–12")
    else:
        raise HTTPException(400, "Invalid bet_type; must be 'over', 'under', or 'exact'")
    roll = shared.secure_randint(2, 12)
    # Multiplier formula uses 11 (actual number of outcomes in 2-12) so that
    # EV is correct. "over": wins = 12-n outcomes; "under": wins = n-2 outcomes.
    if bet_type == "over":
        win = roll > bet_number
        mult = round(11 / max(1, 12 - bet_number), 2)
    elif bet_type == "under":
        win = roll < bet_number
        mult = round(11 / max(1, bet_number - 2), 2)
    else:   # exact
        win = roll == bet_number
        mult = 11.0
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, amount, conn):
                raise HTTPException(400, "Insufficient balance")
            win_amount = round(amount * mult * 0.95, 2) if win else 0
            if win:
                await add_balance(user_id, win_amount, conn)
                await conn.execute("UPDATE users SET dice_wins=dice_wins+1 WHERE user_id=$1", user_id)
            await conn.execute("""
                INSERT INTO game_logs (user_id, game_type, bet_amount, win_amount, multiplier, result)
                VALUES ($1,'dice',$2,$3,$4,$5)
            """, user_id, amount, win_amount, mult, 'win' if win else 'loss')
    return {"success": True, "win": win, "roll": roll, "win_amount": win_amount,
            "multiplier": mult, "bet_type": bet_type, "bet_number": bet_number}

@app.post("/api/games/slots/play")
async def slots_play(request: Request):
    """Slots game in index.html."""
    body = await request.json()
    user_id = await require_auth(request)
    amount = max(10.0, min(750_000.0, float(body.get("amount", 100))))
    if amount < 10:
        raise HTTPException(400, "Minimum bet is $10")
    symbols = [shared.secure_choice(SLOT_SYMBOLS) for _ in range(3)]
    emojis  = [s["emoji"] for s in symbols]
    combo   = "".join(emojis)
    mult    = SLOT_PAYOUTS.get(combo, 0)
    # Near-miss: two matching but not three
    if not mult and emojis[0] == emojis[1]:
        mult = 0
    win_amount = round(amount * mult * 0.96, 2) if mult else 0
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, amount, conn):
                raise HTTPException(400, "Insufficient balance")
            if win_amount:
                await add_balance(user_id, win_amount, conn)
                await conn.execute("UPDATE users SET slots_wins=slots_wins+1 WHERE user_id=$1", user_id)
            await conn.execute("""
                INSERT INTO game_logs (user_id, game_type, bet_amount, win_amount, multiplier, result)
                VALUES ($1,'slots',$2,$3,$4,$5)
            """, user_id, amount, win_amount, float(mult), 'win' if win_amount else 'loss')
    return {"success": True, "symbols": emojis, "win_amount": win_amount,
            "multiplier": mult, "bet_amount": amount}

@app.post("/api/games/mines/start")
async def mines_start(request: Request):
    """Mines mini-game in index.html."""
    body = await request.json()
    user_id = await require_auth(request)
    amount     = max(10.0, min(750_000.0, float(body.get("amount", 100))))
    grid_size  = int(body.get("grid_size", 5))
    mine_count = int(body.get("mine_count", 3))
    if amount < 10:
        raise HTTPException(400, "Minimum bet is $10")
    # Validate grid and mine count. mine_count=0 would place no mines, giving
    # guaranteed 4.8x payouts. grid_size must be 3-7 to keep games manageable.
    grid_size  = max(3, min(7, grid_size))
    total_tiles = grid_size * grid_size
    if mine_count < 1 or mine_count >= total_tiles:
        raise HTTPException(400, f"mine_count must be 1–{total_tiles - 1} for a {grid_size}×{grid_size} grid")
    mine_positions = shared.secure_shuffle(list(range(total_tiles)))[:mine_count]
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            if not await deduct_balance(user_id, amount, conn):
                raise HTTPException(400, "Insufficient balance")
            row = await conn.fetchrow("""
                INSERT INTO mines_games
                    (user_id, bet_amount, grid_size, mine_count, mine_positions, status)
                VALUES ($1,$2,$3,$4,$5,'active') RETURNING id
            """, user_id, amount, grid_size, mine_count, mine_positions)
    return {
        "success": True, "game_id": row["id"], "grid_size": grid_size,
        "mine_count": mine_count, "bet_amount": amount,
        "tiles": [{"index": i, "revealed": False} for i in range(total_tiles)]
    }

@app.post("/api/games/mines/reveal")
async def mines_reveal(request: Request):
    """Reveal a tile in an active mines game."""
    body = await request.json()
    user_id = await require_auth(request)
    game_id  = int(body.get("game_id", 0))
    tile_idx = int(body.get("tile_index", 0))
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # FOR UPDATE prevents concurrent reveals from racing on the same game
            # row and overwriting each other's tile appends or double-counting
            # a re-submitted tile index.
            game = await conn.fetchrow(
                "SELECT * FROM mines_games WHERE id=$1 AND user_id=$2 AND status='active' FOR UPDATE",
                game_id, user_id
            )
            if not game:
                raise HTTPException(404, "Game not found or already ended")
            total_tiles_g = game["grid_size"] ** 2
            if tile_idx < 0 or tile_idx >= total_tiles_g:
                raise HTTPException(400, f"tile_index must be 0–{total_tiles_g - 1}")
            mine_positions = list(game["mine_positions"])
            revealed       = list(game["revealed_tiles"] or [])
            if tile_idx in revealed:
                raise HTTPException(400, "Tile already revealed")
            if tile_idx in mine_positions:
                await conn.execute(
                    "UPDATE mines_games SET status='lost', exploded=true WHERE id=$1", game_id
                )
                await conn.execute(
                    "INSERT INTO game_logs (user_id,game_type,bet_amount,win_amount,multiplier,result) VALUES ($1,'mines',$2,0,0,'loss')",
                    user_id, float(game["bet_amount"])
                )
                return {"success": True, "hit_mine": True, "mine_positions": mine_positions}
            revealed.append(tile_idx)
            safe_count = len(revealed)
            total_safe = game["grid_size"]**2 - game["mine_count"]
            mult = round(1 + (safe_count / total_safe) * 4, 2)
            await conn.execute(
                "UPDATE mines_games SET revealed_tiles=$1, multiplier=$2 WHERE id=$3",
                revealed, mult, game_id
            )
    return {"success": True, "hit_mine": False, "tile_index": tile_idx,
            "safe_count": safe_count, "multiplier": mult, "revealed": revealed}

@app.post("/api/games/mines/cashout")
async def mines_cashout(request: Request):
    """Cash out an active mines game."""
    body = await request.json()
    user_id = await require_auth(request)
    game_id = int(body.get("game_id", 0))
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            game = await conn.fetchrow(
                "SELECT * FROM mines_games WHERE id=$1 AND user_id=$2 AND status='active' FOR UPDATE",
                game_id, user_id
            )
            if not game:
                raise HTTPException(404, "Game not found")
            mult    = float(game["multiplier"] or 1.0)
            bet     = float(game["bet_amount"])
            win     = round(bet * mult * 0.96, 2)
            await add_balance(user_id, win, conn)
            await conn.execute("UPDATE mines_games SET status='won' WHERE id=$1", game_id)
            await conn.execute("UPDATE users SET mines_wins=mines_wins+1 WHERE user_id=$1", user_id)
            await conn.execute("""
                INSERT INTO game_logs (user_id,game_type,bet_amount,win_amount,multiplier,result)
                VALUES ($1,'mines',$2,$3,$4,'win')
            """, user_id, bet, win, mult)
    return {"success": True, "win": win, "multiplier": mult}

@app.post("/api/open-premium-case")
async def open_premium_case(request: Request):
    """Premium case opening — same as regular for now, requires tickets."""
    body = await request.json()
    user_id = await require_auth(request)
    case_id  = body.get("case_id")
    # Bug 183/184 fix: clamp quantity to [1, 25] so negative values can't exploit
    # "tickets - (-n)" to grant free tickets, and large values can't DoS the DB.
    quantity = max(1, min(25, int(body.get("quantity", 1))))
    case = CASES.get(case_id)
    if not case:
        raise HTTPException(400, "Invalid case")
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchval("""
                UPDATE users SET tickets=tickets-$1
                WHERE user_id=$2 AND tickets >= $1
                RETURNING tickets
            """, quantity, user_id)
            if updated is None:
                raise HTTPException(400, f"Need {quantity} ticket(s) — insufficient balance")
            items = []
            for _ in range(quantity):
                item = get_random_item(case_id)
                if not item:
                    continue
                item.setdefault("display_name", item.get("name", ""))
                skin_img_file = item.get('image_filename')
                skin_img_url = f"/static/images/skins/{skin_img_file}" if skin_img_file else None
                row = await conn.fetchrow("""
                    INSERT INTO inventory
                    (user_id,item_name,item_type,rarity,price,condition,is_stattrak,status,case_id,float_value,image_url)
                    VALUES ($1,$2,'weapon',$3,$4,$5,$6,'kept',$7,$8,$9) RETURNING id
                """, user_id, item["name"], item["rarity"], item["price"],
                    item["condition"], item["is_stattrak"], case_id, item["float"], skin_img_url)
                item["id"] = row["id"]
                items.append(item)
            await conn.execute(
                "UPDATE users SET total_opens=total_opens+$1 WHERE user_id=$2", quantity, user_id
            )
    return {"success": True, "items": items, "tickets_used": quantity}

@app.post("/api/sell-batch")
async def sell_batch(request: Request):
    """Sell multiple inventory items at once."""
    body     = await request.json()
    user_id  = await require_auth(request)
    item_ids = body.get("item_ids", [])
    if not item_ids:
        raise HTTPException(400, "No items provided")
    if len(item_ids) > 100:
        raise HTTPException(400, "Maximum 100 items per batch")
    # Sanitise: ensure all IDs are ints
    try:
        item_ids = [int(i) for i in item_ids if i is not None]
    except (ValueError, TypeError):
        raise HTTPException(400, "All item_ids must be integers")
    if not item_ids:
        raise HTTPException(400, "No valid item IDs provided")
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Atomic ownership+status transition matches sell_item; prevents
            # concurrent calls from double-crediting the same items. Protected
            # items are silently excluded, same as already-sold/not-owned ones.
            rows = await conn.fetch("""
                UPDATE inventory SET status='sold'
                WHERE id = ANY($1::int[]) AND user_id=$2 AND status='kept' AND protected=FALSE
                RETURNING id, price
            """, item_ids, user_id)
            if not rows:
                raise HTTPException(404, "No valid items found")
            total = round(sum(float(r["price"]) * 0.70 for r in rows), 2)
            ids   = [r["id"] for r in rows]
            await add_balance(user_id, total, conn)
            # Update sell_items quest progress for each item sold (mirrors sell_item)
            n_sold = len(ids)
            await conn.execute("""
                UPDATE quests SET progress = progress + $1
                WHERE user_id=$2 AND quest_type='sell_items' AND completed=FALSE
            """, n_sold, user_id)
            await conn.execute("""
                UPDATE quests SET completed=TRUE
                WHERE user_id=$1 AND quest_type='sell_items' AND progress >= required AND completed=FALSE
            """, user_id)
    return {
        "success": True,
        "count": len(ids),
        "total_sell_price": total,
        "message": f"Sold {len(ids)} item(s) for ${total:,.2f}",
    }

# ============================================================
# CSRF TOKEN
# ============================================================

@app.get("/api/csrf-token")
async def get_csrf_token(response: Response):
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        "csrf_token", token,
        max_age=3600,
        httponly=False,
        samesite="lax",
        secure=os.getenv("SECURE_COOKIES", "false").lower() == "true",
    )
    return {"csrf_token": token}

# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/api/ping")
async def ping():
    return {"pong": True}