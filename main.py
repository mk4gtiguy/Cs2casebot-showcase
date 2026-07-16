import discord
from discord.ext import commands
from discord import app_commands
import os
import asyncpg
import asyncio
import logging
import secrets
import time as _time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from dotenv import load_dotenv
from fastapi import HTTPException

# Secure RNG helpers (Fix 1)
from shared import (
    secure_random, secure_randint, secure_choice, secure_shuffle, deduct_balance,
    add_balance, credit_win, apply_house, HOUSE_EDGE, clamp_bet,
    require_game_enabled, log_game,
    RARITY_EMOJIS, TRADE_UP_PROGRESSION, DROP_RATES,
    generate_skin_float, get_skin_condition, calculate_item_value,
    get_random_item, get_random_sticker, CASES, STICKER_CAPSULES,
    QUEST_TYPES, ALL_ITEMS_BY_RARITY,
    FEATURED_CASES, get_effective_case, get_effective_capsule,
    fix_surrogate_emoji, GAME_CATALOG, get_sticker_image, get_skin_image_filename,
)

# Discord-native re-implementations of a few games reuse the website's own
# pure game-logic functions directly (spin/evaluate/weapon-grant helpers) --
# this module only builds an APIRouter() at import time, no FastAPI app
# startup side effects, so importing it here is safe and avoids duplicating
# the actual RNG/payout logic in two places.
from routes.games_easy import (
    spin_classic_reel, evaluate_classic,
    spin_cs2_reel, CS2_SPECIAL, CS2_EMOJI_TO_RARITY,
    _grant_cs2_rarity_weapon, _insert_granted_weapon,
    _bonus_round_sessions, BONUS_ROUND_TTL_SECS,
)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if os.path.exists('.env'):
    load_dotenv()

TOKEN = os.getenv('DISCORD_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')

# ============================================
# CHANNEL CONFIGURATION
# ============================================

SUPPORT_CHANNEL_ID = 1516670656266113085
BOT_CHANNEL_ID = SUPPORT_CHANNEL_ID
ANNOUNCEMENTS_CHANNEL_ID = 1516670527391928330

# ============================================
# OTHER CONFIG
# ============================================

KO_FI_URL = "https://ko-fi.com/mk4gtiguy"
DASHBOARD_URL = "https://cs2casebot.xyz/"
DISCORD_INVITE_URL = "https://discord.gg/mU33pc7TDE"

bot = commands.Bot(command_prefix='!', intents=discord.Intents.all())
db_pool = None


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Global fallback for slash-command errors. Without this, an error
    raised during Discord's own argument transformation (e.g. a bad
    channel/user/role selection) never reaches the command's own body at
    all -- interaction.response is never called, and the user just sees
    a silent 'The application did not respond' with zero feedback, while
    the real error only ever shows up in server logs."""
    if isinstance(error, app_commands.TransformerError) and error.type == discord.AppCommandOptionType.channel:
        # A channel-type option converts by looking the picked channel up in
        # this bot's own LOCAL cache for that guild (Guild.get_channel) --
        # not by re-checking the picked channel's type. That lookup comes
        # back empty (raising exactly this error) for two very different
        # reasons that produce byte-identical error text: (a) the bot has no
        # guild member at all in this server -- happens when whoever clicked
        # "Add App" only granted the applications.commands scope, not bot,
        # so slash commands register but no bot user ever joins -- or (b) the
        # bot IS a member but that one channel denies it View Channel via its
        # own permission overrides. Check guild.me first since (a) is the
        # more fundamental (and, in practice, more common) case.
        channel_name = getattr(error.value, "name", None)
        where = f'"{channel_name}"' if channel_name else "that channel"
        if interaction.guild is None or interaction.guild.me is None:
            message = (
                "❌ I don't actually have a bot member in this server — whoever added me likely used "
                "an install link that only granted the \"applications.commands\" scope, not \"bot\". "
                "Re-invite me using an OAuth2 URL with BOTH the \"bot\" and \"applications.commands\" "
                "scopes checked (and View Channels/Send Messages under bot permissions), then try again."
            )
        else:
            message = (
                f"❌ I can't see {where} — my bot role is missing the \"View Channel\" permission there "
                "(check the channel's own permission overrides, not just the server-wide role permissions). "
                "Grant it access, or pick a channel I can already see, then try again."
            )
    elif isinstance(error, app_commands.TransformerError):
        message = (
            "❌ That selection wasn't valid for this command — please pick a value from "
            "Discord's own suggestion list rather than typing it manually, then try again. "
            "If the bot was just added to this server, Discord can take a minute to finish "
            "syncing commands here — try again shortly."
        )
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"❌ Slow down — try again in {error.retry_after:.1f}s."
    elif isinstance(error, app_commands.MissingPermissions):
        message = "❌ You don't have permission to use this command."
    else:
        message = "❌ Something went wrong running that command. Please try again."
    logger.error(f"Slash command error in '{interaction.command.name if interaction.command else '?'}': {error}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception as e:
        logger.warning(f"Could not deliver error message to user for failed command: {e}")

# ── Shared module integration ─────────────────────────────────
# This lets admin.py DM users through the Discord bot.
try:
    import shared as _shared

    async def _bot_notify(user_id: int, message: str):
        """Send a Discord DM — called by the web server's admin routes."""
        try:
            user = await bot.fetch_user(user_id)
            await user.send(message)
        except Exception as e:
            logger.warning(f"bot_notify failed for {user_id}: {e}")

    _shared.bot_notify = _bot_notify
except ImportError:
    pass  # shared.py not present — web server not running alongside

# Fix 3: Jackpot state is now DB-backed � in-memory globals removed.
# Lock kept for I/O serialisation only.
jackpot_lock = asyncio.Lock()

# Fix 3: DB-backed jackpot helpers
async def jackpot_enter(user_id: int, amount: float):
    """Deduct balance and add to jackpot pot — all in one transaction."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "UPDATE users SET balance = balance - $1 WHERE user_id = $2 AND balance >= $1",
                amount, user_id
            )
            if result == "UPDATE 0":
                return False   # insufficient balance
            await conn.execute(
                "UPDATE jackpot_state SET pot = pot + $1, updated_at = NOW() WHERE id = 1",
                amount
            )
            await conn.execute(
                "INSERT INTO jackpot_entries (user_id, amount) VALUES ($1, $2)",
                user_id, amount
            )
    return True

async def jackpot_draw() -> tuple:
    """Pick winner weighted by entry amount, clear state. Returns (winner_id, pot)."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            entries = await conn.fetch("SELECT user_id, amount FROM jackpot_entries")
            pot = await conn.fetchval("SELECT pot FROM jackpot_state WHERE id = 1") or 0
            if not entries:
                return None, 0, 0
            # Weighted selection using secure RNG
            total = sum(float(e['amount']) for e in entries)
            pick = secure_random() * total
            cumulative = 0.0
            winner_id = entries[-1]['user_id']
            for e in entries:
                cumulative += float(e['amount'])
                if pick <= cumulative:
                    winner_id = e['user_id']
                    break
            win_amount = int(float(pot) * 0.95)
            await conn.execute("UPDATE jackpot_state SET pot = 0 WHERE id = 1")
            await conn.execute("DELETE FROM jackpot_entries")
            await conn.execute(
                "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                win_amount, winner_id
            )
            return winner_id, win_amount, float(pot)

# ============================================
# CHANNEL PERMISSION CHECK
# ============================================

async def is_bot_channel(interaction: discord.Interaction):
    if not interaction.guild:
        return True
    try:
        async with db_pool.acquire() as conn:
            setting = await conn.fetchrow("""
                SELECT bot_channel_id FROM guild_settings WHERE guild_id = $1
            """, interaction.guild_id)
        if not setting or setting['bot_channel_id'] is None:
            return True
        if interaction.channel_id != setting['bot_channel_id']:
            await interaction.response.send_message(
                f"❌ Please use bot commands in <#{setting['bot_channel_id']}>!",
                ephemeral=True
            )
            return False
        return True
    except Exception as e:
        logger.error(f"is_bot_channel error: {e}")
        return False

# ============================================
# THEMED EMBED HELPER
# ============================================

# Mirrors static/dashboard.js's SKIN_RARITY_COLORS exactly, so a Discord
# embed for e.g. a Gold weapon grant uses the identical gold the website
# uses for the same rarity, instead of an unrelated Discord.Color.gold().
RARITY_EMBED_COLORS = {
    'Blue':   0x4488ff,
    'Purple': 0xaa00ff,
    'Pink':   0xff69b4,
    'Red':    0xff4444,
    'Gold':   0xffd700,
}

_STANDARD_FOOTER = f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}"


def _themed_embed(title, description=None, color=None, rarity=None,
                   item_image_url=None, footer_extra=None):
    """One shared embed builder so every command's "card" shares the same
    footer/branding instead of the ~8 slightly different footer strings that
    had accumulated across the file. Pass `rarity` (a skin rarity string) to
    color-match the dashboard's own rarity colors, or `item_image_url` (a
    stored inventory image_url, e.g. "/static/images/skins/x.webp") to show
    the actual item as a thumbnail -- previously only /profile ever set an
    embed image at all."""
    if color is None and rarity:
        color = RARITY_EMBED_COLORS.get(rarity)
    if color is None:
        color = discord.Color.blue()
    elif isinstance(color, int):
        color = discord.Color(color)
    embed = discord.Embed(title=title, description=description, color=color)
    if item_image_url:
        url = item_image_url if item_image_url.startswith('http') else f"{DASHBOARD_URL.rstrip('/')}{item_image_url}"
        embed.set_thumbnail(url=url)
    footer_text = f"{footer_extra} | {_STANDARD_FOOTER}" if footer_extra else _STANDARD_FOOTER
    embed.set_footer(text=footer_text)
    return embed

# ============================================
# EMOJIS
# ============================================

CASE_EMOJIS = {
    "cs:go_weapon_case": "📦",
    "esports_2013_case": "🎯",
    "operation_phoenix_weapon_case": "⚡",
    "huntsman_weapon_case": "🔥",
    "operation_breakout_weapon_case": "💎",
    "esports_2014_summer_case": "🌟",
    "operation_vanguard_weapon_case": "🎨",
    "chroma_case": "🌈",
    "chroma_2_case": "💥",
    "falchion_case": "🌅",
    "shadow_case": "⚠️",
    "revolver_case": "🤲",
    "operation_wildfire_case": "🎪",
    "chroma_3_case": "🏹",
    "gamma_case": "🗡️",
    "gamma_2_case": "🛡️",
    "glove_case": "👑",
    "spectrum_case": "🎰",
    "operation_hydra_case": "🎲",
    "spectrum_2_case": "🎳",
    "clutch_case": "🎭",
    "horizon_case": "🎪",
    "danger_zone_case": "🎯",
    "prisma_case": "🎱",
    "shattered_web_case": "🔫",
    "cs20_case": "🌙",
    "prisma_2_case": "🎂",
    "fracture_case": "💎",
    "operation_broken_fang_case": "⚡",
    "snakebite_case": "🌊",
    "operation_riptide_case": "🌪️",
    "dreams_and_nightmares_case": "🎇",
    "recoil_case": "📦",
    "revolution_case": "🎯",
    "kilowatt_case": "⚡",
    "gallery_case": "🔥",
    "fever_case": "💎"
}
# ============================================
# DATABASE FUNCTIONS
# ============================================

async def init_db():
    global db_pool
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        logger.error("❌ DATABASE_URL not set!")
        return False
    try:
        db_pool = await asyncpg.create_pool(db_url, min_size=5, max_size=20)
        logger.info("✅ Database pool ready!")

        # shared.py keeps its own separate db_pool global (used by
        # server.py's process) that this bot process never initializes on
        # its own. deduct_balance/add_balance/credit_win/etc. all call
        # shared.get_db() unconditionally even when a conn is already passed
        # in, so without this they'd raise "Database pool not initialized"
        # the moment any Discord command tries to reuse those helpers.
        # Point shared's global at this SAME pool rather than opening a
        # second one -- get_db()'s return value is never actually used by
        # this bot's own commands (they always pass their own conn), this
        # purely satisfies that eager not-None check.
        import shared as _shared_pool_bridge
        _shared_pool_bridge.db_pool = db_pool

        # Ensure tables exist
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance DECIMAL(15,2) DEFAULT 1000,
                    credits INTEGER DEFAULT 0,
                    tickets INTEGER DEFAULT 0,
                    total_opens INTEGER DEFAULT 0,
                    total_premium_opens INTEGER DEFAULT 0,
                    total_golds INTEGER DEFAULT 0,
                    total_trades INTEGER DEFAULT 0,
                    total_games_played INTEGER DEFAULT 0,
                    win_streak INTEGER DEFAULT 0,
                    coinflip_wins INTEGER DEFAULT 0,
                    dice_wins INTEGER DEFAULT 0,
                    mines_wins INTEGER DEFAULT 0,
                    slots_wins INTEGER DEFAULT 0,
                    daily_streak INTEGER DEFAULT 0,
                    last_daily TIMESTAMP,
                    last_hourly TIMESTAMP,
                    last_weekly TIMESTAMP,
                    total_hourly_claimed INTEGER DEFAULT 0,
                    total_weekly_claimed INTEGER DEFAULT 0,
                    xp INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 1,
                    prestige INTEGER DEFAULT 0,
                    vip_tier TEXT DEFAULT 'none',
                    vip_expires_at TIMESTAMP,
                    vip_boost_multiplier DECIMAL(5,2) DEFAULT 1.0,
                    is_banned BOOLEAN DEFAULT FALSE,
                    ban_reason TEXT,
                    ban_expires TIMESTAMP,
                    avatar_url TEXT,
                    settings JSONB DEFAULT '{}',
                    total_spent DECIMAL(15,2) DEFAULT 0,
                    total_wagered DECIMAL(15,2) DEFAULT 0,
                    lifetime_golds INTEGER DEFAULT 0,
                    jackpot_wins INTEGER DEFAULT 0,
                    total_stickers INTEGER DEFAULT 0,
                    total_inventory_items INTEGER DEFAULT 0,
                    total_quests_completed INTEGER DEFAULT 0,
                    referral_code TEXT,
                    referred_by BIGINT,
                    google_id TEXT,
                    google_email TEXT,
                    google_avatar_url TEXT,
                    primary_provider TEXT DEFAULT 'discord',
                    last_seen TIMESTAMPTZ,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    item_name TEXT NOT NULL,
                    item_type TEXT DEFAULT 'weapon',
                    rarity TEXT,
                    price DECIMAL(15,2),
                    condition TEXT,
                    is_stattrak BOOLEAN DEFAULT FALSE,
                    status TEXT DEFAULT 'kept',
                    case_id TEXT,
                    float_value DECIMAL(10,4),
                    image_url TEXT,
                    tier TEXT,
                    applied_stickers JSONB DEFAULT '[]'::jsonb,
                    in_loadout BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # /loadout_toggle and /loadout below read/write these -- defensive
            # fallback in case this bot process starts before server.py has
            # ever run (which normally creates them); harmless no-op otherwise.
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

            # /itemvshouse below writes here -- same defensive-fallback reasoning
            # as loadouts/loadout_items above (routes/item_house_jackpot.py's own
            # init_house_wager_table() normally creates this via server.py).
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS item_house_wagers (
                    id            SERIAL PRIMARY KEY,
                    user_id       BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    inventory_id  INTEGER NOT NULL REFERENCES inventory(id) ON DELETE RESTRICT,
                    item_name     TEXT NOT NULL,
                    rarity        TEXT,
                    condition     TEXT,
                    is_stattrak   BOOLEAN DEFAULT FALSE,
                    float_value   DECIMAL(10,4),
                    image_url     TEXT,
                    value         DECIMAL(15,2) NOT NULL CHECK (value >= 0.50),
                    won           BOOLEAN NOT NULL,
                    payout_value  DECIMAL(15,2) NOT NULL DEFAULT 0,
                    created_at    TIMESTAMP DEFAULT NOW()
                )
            """)

            # Discord-only Live Case Auction pool -- deliberately separate from
            # routes/live_case_auction.py's own case_auction_rounds/bids tables.
            # The website's live rooms are tracked in server.py's own in-memory
            # dict (a different OS process this bot can't reach), so Discord
            # runs its own independent auction pool with the same game rules
            # rather than trying to bridge into that process's memory.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS discord_case_auctions (
                    id              SERIAL PRIMARY KEY,
                    case_id         TEXT NOT NULL,
                    channel_id      BIGINT NOT NULL,
                    message_id      BIGINT,
                    status          TEXT NOT NULL DEFAULT 'bidding' CHECK (status IN ('bidding','settled','cancelled')),
                    current_bid     DECIMAL(15,2) NOT NULL DEFAULT 0,
                    high_bidder_id  BIGINT,
                    bid_deadline    TIMESTAMPTZ NOT NULL,
                    won_item_name   TEXT,
                    won_item_value  DECIMAL(15,2),
                    created_at      TIMESTAMPTZ DEFAULT NOW(),
                    resolved_at     TIMESTAMPTZ
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS discord_case_auction_bids (
                    id          SERIAL PRIMARY KEY,
                    auction_id  INTEGER NOT NULL REFERENCES discord_case_auctions(id) ON DELETE CASCADE,
                    user_id     BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    amount      DECIMAL(15,2) NOT NULL,
                    refunded    BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_discord_case_auction_bids_auction ON discord_case_auction_bids(auction_id)")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id BIGINT PRIMARY KEY,
                    name TEXT,
                    bot_channel_id BIGINT,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            # /settimezone + _engagement_reminder_loop() below read/write these.
            for col_sql in [
                "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS timezone_offset INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS last_daily_reminder_date DATE",
                "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS last_weekly_reminder_date DATE",
                "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS last_hourly_reminder_at TIMESTAMPTZ",
            ]:
                try:
                    await conn.execute(col_sql)
                except asyncpg.exceptions.DuplicateColumnError:
                    pass

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS quests (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    quest_type TEXT,
                    progress INTEGER DEFAULT 0,
                    required INTEGER,
                    reward INTEGER,
                    completed BOOLEAN DEFAULT FALSE,
                    claimed BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS giveaways (
                    id SERIAL PRIMARY KEY,
                    creator_id BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                    message_id BIGINT,
                    channel_id BIGINT,
                    prize TEXT,
                    prize_amount DECIMAL(10,2),
                    winner_count INTEGER DEFAULT 1,
                    end_time TIMESTAMP,
                    ends_at TIMESTAMP,
                    status TEXT DEFAULT 'active',
                    ended BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS giveaway_entries (
                    id SERIAL PRIMARY KEY,
                    giveaway_id INTEGER REFERENCES giveaways(id) ON DELETE CASCADE,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE (giveaway_id, user_id)
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS coinflip_games (
                    id SERIAL PRIMARY KEY,
                    creator_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    opponent_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    amount DECIMAL(15,2),
                    winner_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    status TEXT DEFAULT 'waiting',
                    created_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS dice_games (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    amount DECIMAL(15,2),
                    bet_type TEXT,
                    bet_number INTEGER,
                    roll_number INTEGER,
                    result TEXT,
                    multiplier DECIMAL(10,2),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS mines_games (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    bet_amount DECIMAL(15,2),
                    grid_size INTEGER DEFAULT 5,
                    mine_count INTEGER DEFAULT 3,
                    status TEXT DEFAULT 'active',
                    mine_positions INTEGER[],
                    revealed_tiles INTEGER[] DEFAULT '{}',
                    multiplier DECIMAL(10,2) DEFAULT 1.0,
                    exploded BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS slots_games (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    bet_amount DECIMAL(15,2),
                    spin_result TEXT[],
                    multiplier DECIMAL(10,2),
                    win_amount DECIMAL(15,2),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_achievements (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    achievement_id TEXT,
                    unlocked_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_streaks (
                    user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                    current_streak INTEGER DEFAULT 0,
                    best_streak INTEGER DEFAULT 0,
                    golds_in_streak INTEGER DEFAULT 0,
                    total_session_opens INTEGER DEFAULT 0,
                    current_case_id TEXT,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                    theme TEXT DEFAULT 'casino',
                    spin_speed TEXT DEFAULT 'normal',
                    sound_enabled BOOLEAN DEFAULT TRUE,
                    feed_enabled BOOLEAN DEFAULT TRUE,
                    confetti_mode TEXT DEFAULT 'always',
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS live_feed (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    username TEXT,
                    item_name TEXT,
                    rarity TEXT,
                    rarity_emoji TEXT,
                    case_type TEXT,
                    float_value DECIMAL(10,4),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS donations (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    amount DECIMAL(15,2),
                    donor_name TEXT,
                    donor_email TEXT,
                    payment_provider TEXT DEFAULT 'stripe',
                    stripe_payment_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS ticket_purchases (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    amount INTEGER,
                    cost_usd DECIMAL(10,2),
                    stripe_session_id TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS skin_upgrades (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    item_id INTEGER,
                    input_rarity TEXT,
                    output_rarity TEXT,
                    success BOOLEAN,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
        
        asyncio.create_task(keep_db_alive())
        asyncio.create_task(_engagement_reminder_loop())
        return True
    except Exception as e:
        logger.error(f"❌ Database error: {e}")
        return False

async def keep_db_alive():
    while True:
        await asyncio.sleep(300)
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("SELECT 1")
            logger.debug("Database keep-alive ping successful")
        except Exception as e:
            logger.error(f"Database keep-alive failed: {e}")
            old_pool = db_pool
            if old_pool:
                try:
                    await old_pool.close()
                except Exception:
                    pass
            await init_db()

async def ensure_user_exists(user_id: int, username: str = None, conn=None):
    """CRITICAL FIX: Ensure user exists before any transaction"""
    try:
        if conn is None:
            async with db_pool.acquire() as conn:
                return await ensure_user_exists(user_id, username, conn)
        
        await conn.execute("""
            INSERT INTO users (user_id, username, balance, created_at, updated_at)
            VALUES ($1, $2, 1000, NOW(), NOW())
            ON CONFLICT (user_id) DO NOTHING
        """, user_id, username or f"User_{user_id}")
        return True
    except Exception as e:
        logger.error(f"ensure_user_exists error for {user_id}: {e}")
        return False

async def get_balance(user_id, conn=None):
    if conn is None:
        async with db_pool.acquire() as conn:
            return await get_balance(user_id, conn)
    
    await ensure_user_exists(user_id, conn=conn)
    user = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
    if not user:
        return 1000
    return user['balance']

async def create_daily_quests(user_id, conn=None):
    if conn is None:
        async with db_pool.acquire() as conn:
            return await create_daily_quests(user_id, conn)
    
    await ensure_user_exists(user_id, conn=conn)
    
    last_quest = await conn.fetchrow("""
        SELECT created_at FROM quests WHERE user_id = $1
        ORDER BY created_at DESC LIMIT 1
    """, user_id)

    if last_quest and last_quest['created_at'].date() == datetime.now(timezone.utc).date():
        return

    await conn.execute("DELETE FROM quests WHERE user_id = $1", user_id)

    for quest_type, quest_info in QUEST_TYPES.items():
        required = quest_info["base_required"]
        reward = quest_info["base_reward"]
        user = await conn.fetchrow("SELECT total_opens FROM users WHERE user_id = $1", user_id)
        if user and user['total_opens'] > 100:
            required = int(required * 1.5)
            reward = int(reward * 1.2)
        await conn.execute("""
            INSERT INTO quests (user_id, quest_type, progress, required, reward, completed, claimed, created_at)
            VALUES ($1, $2, 0, $3, $4, false, false, NOW())
        """, user_id, quest_type, required, reward)

async def update_quest_progress(user_id, quest_type, increment=1, conn=None):
    if conn is None:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                return await update_quest_progress(user_id, quest_type, increment, conn)

    await ensure_user_exists(user_id, conn=conn)

    try:
        # FOR UPDATE prevents a TOCTOU race where two concurrent callers both
        # read the same progress value and each increments by the same amount,
        # producing only one net increment instead of two.
        quest = await conn.fetchrow("""
            SELECT id, progress, required FROM quests
            WHERE user_id = $1 AND quest_type = $2 AND completed = false AND claimed = false
            FOR UPDATE
        """, user_id, quest_type)

        if quest:
            new_progress = quest['progress'] + increment
            if new_progress >= quest['required']:
                await conn.execute("""
                    UPDATE quests SET progress = $1, completed = true WHERE id = $2
                """, quest['required'], quest['id'])
                logger.info(f"Quest {quest_type} completed for user {user_id}")
                return True
            else:
                await conn.execute("""
                    UPDATE quests SET progress = $1 WHERE id = $2
                """, new_progress, quest['id'])
                return True
        return False
    except Exception as e:
        logger.error(f"Quest update error: {e}")
        return False

@bot.event
async def on_ready():
    if not db_pool:
        await init_db()

    # ── Load admin/moderator IDs from env into shared module ──
    try:
        import shared as _shared
        admin_env = os.getenv('ADMIN_USER_IDS', '')
        mod_env   = os.getenv('MODERATOR_USER_IDS', '')
        if admin_env:
            _shared.ADMIN_USER_IDS.update(
                int(x.strip()) for x in admin_env.split(',') if x.strip()
            )
        if mod_env:
            _shared.MODERATOR_USER_IDS.update(
                int(x.strip()) for x in mod_env.split(',') if x.strip()
            )
        logger.info(f"👑 Admin IDs: {_shared.ADMIN_USER_IDS}")
    except Exception as e:
        logger.warning(f"Could not load admin IDs: {e}")

    logger.info(f'✅ {bot.user} is now online!')
    logger.info(f'🎮 Bot is ready on {len(bot.guilds)} servers')
    logger.info(f'📦 Total cases loaded: {len(CASES)}')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=f"CS2 Cases | Join: {DISCORD_INVITE_URL}"))
    try:
        synced = await bot.tree.sync()
        logger.info(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        logger.error(f"Error syncing commands: {e}")

    # Global command syncs can take a while to propagate to every guild's
    # Discord client cache, which has caused stale/incorrectly-typed command
    # option pickers on some servers (e.g. /setchannel's channel option
    # rejecting a selection with "Failed to convert X to TextChannel").
    # Pushing a guild-specific copy to every currently-joined guild on each
    # restart makes the current command definitions available everywhere
    # in seconds rather than waiting on that propagation delay.
    guild_sync_ok = 0
    for _guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=_guild)
            await bot.tree.sync(guild=_guild)
            guild_sync_ok += 1
        except Exception as e:
            logger.warning(f"Guild-specific command sync failed for {_guild.id} ({_guild.name}): {e}")
    logger.info(f"✅ Guild-specific command sync refreshed for {guild_sync_ok}/{len(bot.guilds)} servers")

    # Recover any giveaways that were running before a restart.
    try:
        now_utc = datetime.utcnow()
        async with db_pool.acquire() as conn:
            pending = await conn.fetch(
                "SELECT id, end_time FROM giveaways WHERE ended = false AND end_time > $1",
                now_utc
            )
            expired = await conn.fetch(
                "SELECT id FROM giveaways WHERE ended = false AND end_time <= $1",
                now_utc
            )
        for row in pending:
            delay = max(0.0, (row['end_time'] - now_utc).total_seconds())
            asyncio.create_task(_run_giveaway(row['id'], delay))
        for row in expired:
            asyncio.create_task(_run_giveaway(row['id'], 0))
        logger.info(f"🎉 Recovered {len(pending)} pending + {len(expired)} expired giveaways")
    except Exception as e:
        logger.error(f"Giveaway recovery failed: {e}")

    # Resume any Discord Live Case Auctions still mid-bidding after a restart,
    # same recovery idea as the giveaway sweep above.
    try:
        async with db_pool.acquire() as conn:
            stuck = await conn.fetch(
                "SELECT id, channel_id, message_id FROM discord_case_auctions WHERE status = 'bidding'"
            )
        for row in stuck:
            asyncio.create_task(_run_discord_auction(row['id']))
            # The old CaseAuctionView instance died with the process --
            # reattach a fresh one to the still-open message so its
            # 💰 Place Bid button keeps responding after a restart.
            if row['channel_id'] and row['message_id']:
                try:
                    channel = bot.get_channel(row['channel_id'])
                    if channel:
                        message = await channel.fetch_message(row['message_id'])
                        await message.edit(view=CaseAuctionView(row['id']))
                except Exception as e:
                    logger.warning(f"Failed to reattach case auction view for {row['id']}: {e}")
        logger.info(f"🎨 Recovered {len(stuck)} in-progress Discord case auctions")
    except Exception as e:
        logger.error(f"Case auction recovery failed: {e}")


def _chunk_text(text: str, limit: int = 2000) -> list:
    """Greedily pack lines under Discord's per-message character limit.
    A single line longer than the limit is hard-sliced as a last resort."""
    chunks = []
    current = ""
    for line in text.split("\n"):
        piece = line if not current else "\n" + line
        if len(current) + len(piece) <= limit:
            current += piece
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


async def _relay_announcement(message: discord.Message):
    """Echo an admin's post in the support server's announcements channel
    into every other guild's configured bot channel. Fire-and-forget --
    a failure in one guild (deleted channel, lost permission) must not
    stop delivery to the rest."""
    header = f"📢 **Announcement from {message.guild.name}:**"
    body_parts = [header]
    if message.content:
        body_parts.append(message.content)
    for attachment in message.attachments:
        body_parts.append(attachment.url)
    full_text = "\n".join(body_parts)
    chunks = _chunk_text(full_text)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id, bot_channel_id FROM guild_settings WHERE bot_channel_id IS NOT NULL"
        )

    for row in rows:
        if row['guild_id'] == message.guild.id:
            continue  # don't echo back into the source server
        channel = bot.get_channel(row['bot_channel_id'])
        if channel is None:
            continue
        try:
            for chunk in chunks:
                await channel.send(chunk)
        except Exception as e:
            logger.warning(f"Announcement relay failed for guild {row['guild_id']}: {e}")
        await asyncio.sleep(0.3)


@bot.event
async def on_message(message: discord.Message):
    if (
        not message.author.bot
        and message.channel.id == ANNOUNCEMENTS_CHANNEL_ID
        and message.author.id in _shared.ADMIN_USER_IDS
    ):
        asyncio.create_task(_relay_announcement(message))
    await bot.process_commands(message)


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Auto-register a fallback bot channel so a new server starts
    receiving relayed announcements immediately, without needing an
    admin to run /setchannel first. Doesn't overwrite an existing
    row (ON CONFLICT DO NOTHING) in case this ever fires for a guild
    that's already configured (e.g. re-invited after being kicked)."""
    # Global slash-command definitions (registered via bot.tree.sync() in
    # on_ready) can take a while to propagate to a BRAND NEW guild's Discord
    # client cache -- in the meantime that guild's command picker can show a
    # stale/incorrectly-typed option list (e.g. letting a non-text channel be
    # picked for /setchannel's channel option), which then fails to convert
    # server-side with a confusing "Failed to convert X to TextChannel" error.
    # Copying the current global commands into a guild-specific registration
    # and syncing just that guild makes the up-to-date definitions available
    # to it in seconds instead of waiting on slow global propagation.
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except Exception as e:
        logger.warning(f"Guild-specific command sync failed for {guild.id} ({guild.name}): {e}")

    channel = guild.system_channel
    if channel is None or not channel.permissions_for(guild.me).send_messages:
        channel = next(
            (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
            None,
        )
    if channel is None:
        logger.warning(f"No sendable channel found in new guild {guild.id} ({guild.name})")
        return

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings (guild_id, name, bot_channel_id, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (guild_id) DO NOTHING
        """, guild.id, guild.name, channel.id)

    try:
        embed = discord.Embed(
            title="👋 Thanks for adding CS2CaseBot!",
            description=f"I've set {channel.mention} as your bot channel for now — use `/setchannel` anytime to point me at a different one.",
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
        await channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"Welcome message failed for guild {guild.id}: {e}")


# ============================================
# XP SYSTEM
# ============================================

async def add_xp(user_id: int, amount: int, conn=None):
    if conn is None:
        async with db_pool.acquire() as conn:
            return await add_xp(user_id, amount, conn)
    
    await ensure_user_exists(user_id, conn=conn)
    
    async with conn.transaction():
        user = await conn.fetchrow(
            "SELECT xp, level, prestige FROM users WHERE user_id = $1 FOR UPDATE",
            user_id
        )
        if not user:
            return
        
        new_xp = (user['xp'] or 0) + amount
        current_level = user['level'] or 1
        # Track prestige locally so each milestone in the same call sees the
        # running total (not the stale value from the initial SELECT).
        prestige = user['prestige'] or 0
        leveled_up = False

        xp_needed = current_level * 50 + 100

        while new_xp >= xp_needed:
            new_xp -= xp_needed
            current_level += 1
            xp_needed = current_level * 50 + 100
            leveled_up = True

            reward = current_level * 50
            await conn.execute(
                "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                reward, user_id
            )

            if current_level % 50 == 0:
                prestige += 1
                await conn.execute(
                    "UPDATE users SET prestige = $1 WHERE user_id = $2",
                    prestige, user_id
                )
                bonus = prestige * 1000
                await conn.execute(
                    "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                    bonus, user_id
                )
        
        await conn.execute(
            "UPDATE users SET xp = $1, level = $2 WHERE user_id = $3",
            new_xp, current_level, user_id
        )
        
        return {'level': current_level, 'xp': new_xp, 'leveled_up': leveled_up}

# ============================================
# PAGINATED INVENTORY VIEW
# ============================================

def _inventory_item_image_url(item):
    """Resolve the /static image path for one inventory row, by item_type --
    weapons and stickers live under different directories/lookup tables."""
    if item['item_type'] == 'weapon':
        filename = get_skin_image_filename(item['item_name'])
        return f"/static/images/skins/{filename}" if filename else None
    else:
        filename = get_sticker_image(item['item_name'])
        return f"/static/images/stickers/{filename}" if filename else None


class InventoryView(discord.ui.View):
    """Two modes on one view: the default compact text list (fast overview
    of everything), and a "🖼️ Gallery" mode that shows one item at a time
    with its actual image -- Discord embeds only support one image each, so
    a picture per row in the list isn't possible without losing the
    overview, hence the toggle instead of replacing the list outright."""
    def __init__(self, items, user, items_per_page=10):
        super().__init__(timeout=120)
        self.items = items
        self.user = user
        self.items_per_page = items_per_page
        self.current_page = 0
        self.total_pages = max(1, (len(items) + items_per_page - 1) // items_per_page)
        self.gallery_mode = False
        self.gallery_index = 0
        self.message = None
        self._action_buttons = []

    def get_embed(self):
        return self._get_gallery_embed() if self.gallery_mode else self._get_list_embed()

    def _get_list_embed(self):
        start = self.current_page * self.items_per_page
        end = start + self.items_per_page
        page_items = self.items[start:end]

        total_value = sum(float(item['price']) for item in self.items)

        embed = discord.Embed(title=f"📦 {self.user.display_name}'s Inventory", color=discord.Color.gold())

        weapon_list = ""
        sticker_list = ""

        for item in page_items:
            stattrak = "ⓢ™️ " if item['is_stattrak'] else ""
            rarity_emoji = RARITY_EMOJIS.get(item['rarity'], "")
            float_display = f" | Float: {item.get('float_value', 0.0000):.4f}" if item.get('float_value') is not None else ""
            item_text = f"**ID:{item['id']}** {stattrak}{rarity_emoji} {item['item_name']} - ${float(item['price']):,.2f}{float_display}\n"
            if item['item_type'] == 'weapon':
                if len(weapon_list) + len(item_text) < 1024:
                    weapon_list += item_text
            else:
                if len(sticker_list) + len(item_text) < 1024:
                    sticker_list += item_text

        if weapon_list:
            embed.add_field(name="🎮 Weapons", value=weapon_list, inline=False)
        if sticker_list:
            embed.add_field(name="⭐ Stickers", value=sticker_list, inline=False)

        embed.add_field(name="💰 Total Inventory Value", value=f"${total_value:,.2f}", inline=False)
        embed.set_footer(text=f"Page {self.current_page + 1}/{self.total_pages} | 💖 Support: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")

        return embed

    def _get_gallery_embed(self):
        item = self.items[self.gallery_index]
        stattrak = "StatTrak™ " if item['is_stattrak'] else ""
        rarity_emoji = RARITY_EMOJIS.get(item['rarity'], "")
        is_weapon = item['item_type'] == 'weapon'

        embed = _themed_embed(
            f"{'🎮' if is_weapon else '⭐'} {stattrak}{rarity_emoji} {item['item_name']}",
            rarity=item['rarity'] if is_weapon else None,
            color=None if is_weapon else STICKER_RARITY_COLORS.get(item['rarity'], 0x808080),
            item_image_url=_inventory_item_image_url(item),
        )
        embed.add_field(name="ID", value=str(item['id']), inline=True)
        embed.add_field(name="Rarity", value=item['rarity'], inline=True)
        embed.add_field(name="Value", value=f"${float(item['price']):,.2f}", inline=True)
        if is_weapon:
            embed.add_field(name="Condition", value=item.get('condition') or 'N/A', inline=True)
            if item.get('float_value') is not None:
                embed.add_field(name="Float", value=f"{item['float_value']:.4f}", inline=True)
        embed.set_footer(text=f"Item {self.gallery_index + 1}/{len(self.items)} | 💖 Support: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
        return embed

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ This inventory belongs to someone else!", ephemeral=True)
            return

        if self.gallery_mode:
            self.gallery_index = (self.gallery_index - 1) % len(self.items)
        else:
            self.current_page = (self.current_page - 1) % self.total_pages
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ This inventory belongs to someone else!", ephemeral=True)
            return

        if self.gallery_mode:
            self.gallery_index = (self.gallery_index + 1) % len(self.items)
        else:
            self.current_page = (self.current_page + 1) % self.total_pages
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="🖼️ Gallery", style=discord.ButtonStyle.primary)
    async def toggle_gallery(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ This inventory belongs to someone else!", ephemeral=True)
            return
        if not self.items:
            await interaction.response.send_message("❌ Your inventory is empty.", ephemeral=True)
            return

        self.gallery_mode = not self.gallery_mode
        if self.gallery_mode:
            self.gallery_index = min(self.current_page * self.items_per_page, len(self.items) - 1)
            self._add_action_buttons()
        else:
            self._remove_action_buttons()
        button.label = "📋 List" if self.gallery_mode else "🖼️ Gallery"
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    # ── Gallery action buttons (Sell / Upgrade / Loadout) --------------
    # Added dynamically only while in gallery mode -- matches the web
    # dashboard's inventory, where clicking an item card offers these same
    # actions directly instead of needing a separate command.

    def _add_action_buttons(self):
        if self._action_buttons:
            return
        sell_btn = discord.ui.Button(label="💰 Sell", style=discord.ButtonStyle.danger, row=1)
        sell_btn.callback = self._sell_current_item
        upgrade_btn = discord.ui.Button(label="⭐ Upgrade", style=discord.ButtonStyle.success, row=1)
        upgrade_btn.callback = self._upgrade_current_item
        loadout_btn = discord.ui.Button(label="🎽 Loadout", style=discord.ButtonStyle.primary, row=1)
        loadout_btn.callback = self._toggle_loadout_current_item
        self._action_buttons = [sell_btn, upgrade_btn, loadout_btn]
        for b in self._action_buttons:
            self.add_item(b)

    def _remove_action_buttons(self):
        for b in self._action_buttons:
            self.remove_item(b)
        self._action_buttons = []

    async def _refresh_after_item_removed(self, interaction: discord.Interaction):
        """Common cleanup after a gallery action consumes the current item
        (sell, or an upgrade -- which always replaces/destroys the old row)."""
        self.items.pop(self.gallery_index)
        if not self.items:
            self._remove_action_buttons()
            self.gallery_mode = False
            await interaction.followup.send("📦 Your inventory is now empty.", ephemeral=True)
            self.total_pages = 1
            await self.message.edit(embed=discord.Embed(title=f"📦 {self.user.display_name}'s Inventory", description="Empty.", color=discord.Color.gold()), view=self)
            return
        self.gallery_index = min(self.gallery_index, len(self.items) - 1)
        self.total_pages = max(1, (len(self.items) + self.items_per_page - 1) // self.items_per_page)
        await self.message.edit(embed=self.get_embed(), view=self)

    async def _sell_current_item(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ This inventory belongs to someone else!", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        item = self.items[self.gallery_index]
        result = await _do_sell_item(self.user.id, self.user.display_name, item['id'])
        if not result["ok"]:
            reason = "This item is protected — unprotect it first to sell." if result["reason"] == "protected" else "Item not found."
            await interaction.followup.send(f"❌ {reason}", ephemeral=True)
            return
        await interaction.followup.send(f"💰 Sold **{item['item_name']}** for ${result['sell_price']:,.2f}!", ephemeral=True)
        await self._refresh_after_item_removed(interaction)

    async def _upgrade_current_item(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ This inventory belongs to someone else!", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        item = self.items[self.gallery_index]
        result = await skin_upgrade(self.user.id, item['id'])
        if not result["success"]:
            await interaction.followup.send(f"❌ {result['error']}", ephemeral=True)
            return
        if result.get("upgraded"):
            await interaction.followup.send(f"⭐ Success! {result['old_item_name']} → {result['new_item_name']} ({result['new_rarity']})", ephemeral=True)
        else:
            await interaction.followup.send(f"💔 Upgrade failed! Lost {result['old_item_name']}. Cost: ${result['cost']}", ephemeral=True)
        await self._refresh_after_item_removed(interaction)

    async def _toggle_loadout_current_item(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ This inventory belongs to someone else!", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        item = self.items[self.gallery_index]
        result = await _do_toggle_loadout(self.user.id, item['id'])
        if not result["ok"]:
            await interaction.followup.send("❌ Item not found.", ephemeral=True)
            return
        action = "added to" if result["in_loadout"] else "removed from"
        await interaction.followup.send(f"🎽 {result['item_name']} {action} your loadout.", ephemeral=True)

# ============================================
# ECONOMY COMMANDS
# ============================================

@bot.tree.command(name="balance", description="Check your balance")
async def balance(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()
    async with db_pool.acquire() as conn:
        await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)
        user = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", interaction.user.id)
        if not user:
            await conn.execute("INSERT INTO users (user_id, balance) VALUES ($1, $2)", interaction.user.id, 1000)
            bal = 1000
        else:
            bal = user['balance']
    embed = _themed_embed("💰 Balance", color=discord.Color.green())
    embed.add_field(name=interaction.user.display_name, value=f"${bal:,.2f}", inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="daily", description="Claim your daily reward")
async def daily(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)

            # SELECT FOR UPDATE locks the row so concurrent daily claims queue up
            # rather than both passing the date check simultaneously.
            user = await conn.fetchrow(
                "SELECT daily_streak, last_daily, balance FROM users WHERE user_id = $1 FOR UPDATE",
                interaction.user.id
            )
            if not user:
                await conn.execute("INSERT INTO users (user_id, balance) VALUES ($1, $2)", interaction.user.id, 1000)
                await create_daily_quests(interaction.user.id, conn)
                user = await conn.fetchrow(
                    "SELECT daily_streak, last_daily, balance FROM users WHERE user_id = $1 FOR UPDATE",
                    interaction.user.id
                )

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            last_daily = user['last_daily']
            if last_daily and last_daily.tzinfo is not None:
                last_daily = last_daily.replace(tzinfo=None)
            streak = user['daily_streak'] or 0

            if last_daily and last_daily.date() == now.date():
                embed = _themed_embed("⏰ Already Claimed", description="You've already claimed today's daily reward!", color=discord.Color.red())
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            if last_daily and last_daily.date() == (now - timedelta(days=1)).date():
                streak += 1
            else:
                streak = 1

            reward = 500 + (streak * 100)
            jackpot_hit = secure_randint(1, 1000000) == 1

            if jackpot_hit:
                reward += 50000

            streak_bonus = {10: 25, 25: 75, 50: 250, 100: 1000}.get(streak, 0)
            reward += streak_bonus

            await conn.execute("UPDATE users SET balance = balance + $1, daily_streak = $2, last_daily = $3 WHERE user_id = $4", reward, streak, now, interaction.user.id)

            updated_user = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", interaction.user.id)
            new_balance = updated_user['balance'] if updated_user else reward

        # Send jackpot notification outside the transaction so a Discord error
        # cannot roll back the already-committed balance update.
        if jackpot_hit:
            embed2 = _themed_embed("🎰🎰🎰 JACKPOT! 🎰🎰🎰", description="You won an additional **$50,000**!", color=discord.Color.gold())
            await interaction.followup.send(embed2)

        embed = _themed_embed("🎁 Daily Reward Claimed!", color=discord.Color.green())
        embed.add_field(name="Reward", value=f"${reward:,.2f}", inline=True)
        embed.add_field(name="Streak", value=f"{streak} days", inline=True)
        embed.add_field(name="New Balance", value=f"${new_balance:,.2f}", inline=True)

        if streak_bonus:
            embed.add_field(name="🏆 Streak Bonus", value=f"${streak_bonus} added!", inline=True)

        # daily_streak quest updated outside transaction — uses its own connection
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="transfer", description="Transfer money to another user")
async def transfer(interaction: discord.Interaction, user: discord.User, amount: float):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()
    if amount <= 0:
        await interaction.followup.send("Amount must be positive!", ephemeral=True)
        return
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)
            await ensure_user_exists(user.id, user.display_name, conn)

            # Atomic deduct: WHERE balance >= $1 prevents negative balance under concurrency
            updated = await conn.fetchrow(
                "UPDATE users SET balance = balance - $1 WHERE user_id = $2 AND balance >= $1 RETURNING balance",
                amount, interaction.user.id
            )
            if not updated:
                await interaction.followup.send("Insufficient balance!", ephemeral=True)
                return
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, user.id)
            new_balance = float(updated['balance'])
        embed = _themed_embed("💸 Transfer Complete", color=discord.Color.green())
        embed.add_field(name="Sender", value=interaction.user.display_name, inline=True)
        embed.add_field(name="Receiver", value=user.display_name, inline=True)
        embed.add_field(name="Amount", value=f"${amount:,.2f}", inline=True)
        embed.add_field(name="Your New Balance", value=f"${new_balance:,.2f}", inline=True)
        await interaction.followup.send(embed=embed)

# ============================================
# CASE COMMANDS
# ============================================

async def _case_id_autocomplete(interaction: discord.Interaction, current: str):
    current = (current or "").lower()
    choices = []
    for cid, data in CASES.items():
        name = data.get('name', cid)
        if current in cid.lower() or current in name.lower():
            choices.append(app_commands.Choice(name=f"{data.get('emoji', '📦')} {name} (${data.get('price', 0):.2f})", value=cid))
        if len(choices) >= 25:
            break
    return choices


async def _capsule_id_autocomplete(interaction: discord.Interaction, current: str):
    current = (current or "").lower()
    choices = []
    for cid, data in STICKER_CAPSULES.items():
        name = data.get('name', cid)
        if current in cid.lower() or current in name.lower():
            choices.append(app_commands.Choice(name=f"{fix_surrogate_emoji(data.get('emoji', '📦'))} {name} (${data.get('price', 0):.2f})", value=cid))
        if len(choices) >= 25:
            break
    return choices


def _make_own_inventory_autocomplete(item_type: str = None):
    """Factory for a command's item_id-style autocomplete over the CLICKING
    user's own kept inventory, optionally filtered to one item_type -- used
    by /sell, /upgrade, /loadout_toggle, /apply_sticker, /remove_sticker so
    none of them require typing a raw inventory ID from memory."""
    async def _autocomplete(interaction: discord.Interaction, current: str):
        current_lower = (current or "").lower()
        query = "SELECT id, item_name, price FROM inventory WHERE user_id=$1 AND status='kept'"
        params = [interaction.user.id]
        if item_type:
            query += " AND item_type=$2"
            params.append(item_type)
        query += " ORDER BY created_at DESC LIMIT 200"
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        choices = []
        for row in rows:
            if current_lower in row['item_name'].lower() or current_lower in str(row['id']):
                label = f"#{row['id']} {row['item_name']} (${float(row['price']):.2f})"
                choices.append(app_commands.Choice(name=label[:100], value=row['id']))
            if len(choices) >= 25:
                break
        return choices
    return _autocomplete


_sell_item_autocomplete = _make_own_inventory_autocomplete()
_upgrade_item_autocomplete = _make_own_inventory_autocomplete('weapon')
_loadout_item_autocomplete = _make_own_inventory_autocomplete('weapon')
_weapon_id_autocomplete = _make_own_inventory_autocomplete('weapon')
_sticker_id_autocomplete = _make_own_inventory_autocomplete('sticker')


@bot.tree.command(name="cases", description="View available cases")
async def list_cases(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    case_items = list(CASES.items())
    chunks = [case_items[i:i+20] for i in range(0, len(case_items), 20)]

    # Resolve admin price overrides + fire sale discounts once up front so the
    # (synchronous) paginated view doesn't need to await mid-render.
    prices = {}
    for case_id, case_data in case_items:
        eff = await get_effective_case(case_id, case_data['price'], case_id in FEATURED_CASES)
        prices[case_id] = eff['price']

    class CaseView(discord.ui.View):
        def __init__(self, chunks_data):
            super().__init__(timeout=120)
            self.chunks = chunks_data
            self.current_page = 0
            self.total_pages = len(chunks_data)
            self.message = None

        def get_embed(self):
            embed = discord.Embed(
                title=f"📦 Available Cases ({len(CASES)}) - Page {self.current_page + 1}/{self.total_pages}",
                color=discord.Color.blue()
            )
            for case_id, case_data in self.chunks[self.current_page]:
                embed.add_field(
                    name=f"{case_data['emoji']} {case_data['name']}",
                    value=f"Price: ${prices[case_id]:.2f}\nUse: `/open {case_id}`",
                    inline=True
                )
            odds_text = " · ".join(
                f"{RARITY_EMOJIS.get(r, '')} {r} {DROP_RATES[r]}%"
                for r in ("Blue", "Purple", "Pink", "Red", "Gold")
            )
            embed.add_field(name="🎲 Odds (same for every case)", value=odds_text, inline=False)
            embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
            return embed
        
        @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
        async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.current_page = (self.current_page - 1) % self.total_pages
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        
        @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
        async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.current_page = (self.current_page + 1) % self.total_pages
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        
        async def on_timeout(self):
            for item in self.children:
                item.disabled = True
            if self.message:
                try:
                    await self.message.edit(view=self)
                except Exception:
                    pass

    view = CaseView(chunks)
    response = await interaction.followup.send(embed=view.get_embed(), view=view)
    view.message = response

async def _do_open_case(user_id: int, display_name: str, case_id: str) -> dict:
    """Deduct + roll + insert + quest/xp for a single case open. Shared by
    the /open command and OpenAgainView's button so a repeat open never
    needs to re-run /cases or retype anything."""
    if case_id not in CASES:
        return {"ok": False, "reason": "invalid_case"}
    case_data = CASES[case_id]
    price = (await get_effective_case(case_id, case_data['price'], case_id in FEATURED_CASES))['price']

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, display_name, conn)

            # Atomic deduct: WHERE balance >= $1 prevents negative balance under concurrency.
            deducted = await conn.fetchval(
                "UPDATE users SET balance = balance - $1, total_opens = total_opens + 1 WHERE user_id = $2 AND balance >= $1 RETURNING user_id",
                price, user_id
            )
            if not deducted:
                return {"ok": False, "reason": "insufficient_balance", "price": price, "case_data": case_data}

            item = get_random_item(case_id)
            if item is None:
                return {"ok": False, "reason": "roll_error"}

            price_value = float(item['price'])
            await conn.execute("""INSERT INTO inventory
                (user_id, item_name, item_type, rarity, price, condition, is_stattrak, float_value)
                VALUES ($1, $2, 'weapon', $3, $4, $5, $6, $7)""",
                user_id, item['name'], item['rarity'], price_value,
                item.get('condition', 'Field-Tested'), item['is_stattrak'],
                item.get('float', 0.0000))

            if item['rarity'] == "Gold":
                await conn.execute("UPDATE users SET total_golds = total_golds + 1 WHERE user_id = $1", user_id)
                await update_quest_progress(user_id, "get_golds", 1, conn)

            await update_quest_progress(user_id, "open_cases", 1, conn)
            await update_quest_progress(user_id, "earn_money", int(price), conn)

            new_balance = await get_balance(user_id, conn)
            await add_xp(user_id, 25, conn)

    return {"ok": True, "item": item, "price": price, "new_balance": new_balance, "case_data": case_data}


def _case_open_embed(case_id: str, result: dict) -> discord.Embed:
    item = result["item"]
    case_data = result["case_data"]
    image_filename = item.get('image_filename')
    embed = _themed_embed(
        f"🔑 Opening {case_data['emoji']} {case_data['name']}...",
        rarity=item['rarity'],
        item_image_url=f"/static/images/skins/{image_filename}" if image_filename else None,
    )
    embed.add_field(name="✨ You got:", value=f"**{item['display_name']}**", inline=False)
    embed.add_field(name="Rarity", value=item['rarity'], inline=True)
    embed.add_field(name="Condition", value=item.get('condition', 'N/A'), inline=True)
    embed.add_field(name="🔢 Float", value=f"{item.get('float', 0.0000):.4f}", inline=True)
    embed.add_field(name="Value", value=f"${item['price']:,.2f}", inline=True)
    if item['is_stattrak']:
        embed.add_field(name="🔥 StatTrak™", value="Rare StatTrak™ variant!", inline=False)
    embed.add_field(name="💰 Cost", value=f"${result['price']:.2f}", inline=True)
    embed.add_field(name="💰 New Balance", value=f"${result['new_balance']:,.2f}", inline=True)
    return embed


class OpenAgainView(discord.ui.View):
    """A single button that re-opens the same case for whoever clicks it --
    anyone in the channel, not just the original opener, so trying the same
    case someone just pulled from is a single click instead of re-running
    /cases to look the ID back up. No timeout: keeps working indefinitely so
    a rapid-fire opening session never needs to retype the command."""
    def __init__(self, case_id: str):
        super().__init__(timeout=None)
        self.case_id = case_id
        case_data = CASES.get(case_id, {})
        self.children[0].label = f"🔓 Open {case_data.get('name', case_id)} Again"

    @discord.ui.button(label="🔓 Open Again", style=discord.ButtonStyle.success)
    async def open_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        result = await _do_open_case(interaction.user.id, interaction.user.display_name, self.case_id)
        if not result["ok"]:
            if result["reason"] == "insufficient_balance":
                embed = _themed_embed("❌ Insufficient Balance", description=f"You need ${result['price']:.2f} to open this case!", color=discord.Color.red())
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send("❌ Error opening case. Please try again.", ephemeral=True)
            return
        await interaction.followup.send(embed=_case_open_embed(self.case_id, result), view=OpenAgainView(self.case_id))


@bot.tree.command(name="open", description="Open a case")
@app_commands.describe(case="Case to open — start typing a name to search")
@app_commands.autocomplete(case=_case_id_autocomplete)
async def open_case(interaction: discord.Interaction, case: str):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    case_id = case.lower()
    try:
        result = await _do_open_case(interaction.user.id, interaction.user.display_name, case_id)
        if not result["ok"]:
            if result["reason"] == "invalid_case":
                await interaction.followup.send("❌ Unknown case. Start typing a case name and pick one from the list that pops up.", ephemeral=True)
            elif result["reason"] == "insufficient_balance":
                embed = _themed_embed("❌ Insufficient Balance", description=f"You need ${result['price']:.2f} to open this case!", color=discord.Color.red())
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send("❌ Error opening case. Please try again.", ephemeral=True)
            return
        await interaction.followup.send(embed=_case_open_embed(case_id, result), view=OpenAgainView(case_id))
    except Exception as e:
        logger.error(f"Open case error: {e}")
        await interaction.followup.send(f"❌ Error opening case: {str(e)[:100]}", ephemeral=True)

# ============================================
# BULK OPEN COMMAND
# ============================================

BULK_OPEN_DISCOUNTS = {5: 0.05, 10: 0.10, 15: 0.15, 20: 0.20, 25: 0.25}


async def _do_bulk_open_case(user_id: int, display_name: str, case_id: str, quantity: int) -> dict:
    """Deduct + roll x quantity + insert + quest/xp for a bulk case open.
    Shared by /bulkopen and BulkOpenAgainView's button."""
    if case_id not in CASES:
        return {"ok": False, "reason": "invalid_case"}
    case_data = CASES[case_id]
    price = (await get_effective_case(case_id, case_data['price'], case_id in FEATURED_CASES))['price']
    discount_percent = int(BULK_OPEN_DISCOUNTS[quantity] * 100)
    total_cost = round(price * quantity * (1 - BULK_OPEN_DISCOUNTS[quantity]), 2)

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, display_name, conn)

            # Atomic deduct + opens increment; WHERE balance >= $1 prevents
            # negative balance under concurrent bulkopen requests.
            deducted = await conn.fetchval(
                "UPDATE users SET balance = balance - $1, total_opens = total_opens + $2 WHERE user_id = $3 AND balance >= $1 RETURNING balance",
                total_cost, quantity, user_id
            )
            if deducted is None:
                return {"ok": False, "reason": "insufficient_balance", "total_cost": total_cost, "case_data": case_data}

            old_balance = float(deducted) + total_cost
            items = []
            for _ in range(quantity):
                item = get_random_item(case_id)
                if item:
                    price_value = float(item['price'])
                    row = await conn.fetchrow("""INSERT INTO inventory
                        (user_id, item_name, item_type, rarity, price, condition, is_stattrak, status, float_value)
                        VALUES ($1, $2, 'weapon', $3, $4, $5, $6, 'kept', $7) RETURNING id""",
                        user_id, item['name'], item['rarity'], price_value,
                        item.get('condition', 'Field-Tested'), item['is_stattrak'],
                        item.get('float', 0.0000))
                    if row:
                        item['id'] = row['id']
                    items.append(item)

                    if item['rarity'] == "Gold":
                        await conn.execute("UPDATE users SET total_golds = total_golds + 1 WHERE user_id = $1", user_id)
                        await update_quest_progress(user_id, "get_golds", 1, conn)

            if not items:
                return {"ok": False, "reason": "roll_error"}

            await update_quest_progress(user_id, "open_cases", quantity, conn)
            await update_quest_progress(user_id, "earn_money", int(total_cost), conn)

            new_balance = await get_balance(user_id, conn)
            await add_xp(user_id, quantity * 10, conn)

    return {
        "ok": True, "items": items, "total_cost": total_cost, "discount_percent": discount_percent,
        "old_balance": old_balance, "new_balance": new_balance, "case_data": case_data,
    }


def _bulk_open_embed(quantity: int, result: dict) -> discord.Embed:
    items = result["items"]
    case_data = result["case_data"]
    best_item = max(items, key=lambda i: float(i['price']))
    image_filename = best_item.get('image_filename')

    item_summary = ""
    for i, item in enumerate(items[:10], 1):
        float_display = f" (Float: {item.get('float', 0.0000):.4f})" if item.get('float') is not None else ""
        item_summary += f"{i}. {item['display_name']} - ${item['price']:.2f}{float_display}\n"
    if len(items) > 10:
        item_summary += f"... and {len(items) - 10} more items"

    embed = _themed_embed(
        f"🔑 Bulk Opened {quantity} {case_data['name']}s!",
        rarity=best_item['rarity'],
        item_image_url=f"/static/images/skins/{image_filename}" if image_filename else None,
        footer_extra=f"✨ Best pull: {best_item['display_name']}",
    )
    embed.add_field(name="📦 Items Obtained", value=item_summary[:1024], inline=False)
    embed.add_field(name="💰 Total Cost", value=f"${result['total_cost']:.2f} ({result['discount_percent']}% discount!)", inline=True)
    embed.add_field(name="💰 Previous Balance", value=f"${result['old_balance']:.2f}", inline=True)
    embed.add_field(name="💰 New Balance", value=f"${result['new_balance']:.2f}", inline=True)
    return embed


class BulkOpenAgainView(discord.ui.View):
    """Same "no retyping" idea as OpenAgainView, for bulk opens -- re-opens
    the same case at the same quantity for whoever clicks it."""
    def __init__(self, case_id: str, quantity: int):
        super().__init__(timeout=None)
        self.case_id = case_id
        self.quantity = quantity
        case_data = CASES.get(case_id, {})
        self.children[0].label = f"🔓 Open {quantity} {case_data.get('name', case_id)} Again"

    @discord.ui.button(label="🔓 Open Again", style=discord.ButtonStyle.success)
    async def open_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        result = await _do_bulk_open_case(interaction.user.id, interaction.user.display_name, self.case_id, self.quantity)
        if not result["ok"]:
            if result["reason"] == "insufficient_balance":
                embed = _themed_embed("❌ Insufficient Balance", description=f"You need ${result['total_cost']:.2f} to open {self.quantity} {result['case_data']['name']}s!", color=discord.Color.red())
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send("❌ Error opening cases. Please try again.", ephemeral=True)
            return
        await interaction.followup.send(embed=_bulk_open_embed(self.quantity, result), view=BulkOpenAgainView(self.case_id, self.quantity))


@bot.tree.command(name="bulkopen", description="Open multiple cases at once with discount (5,10,15,20,25)")
@app_commands.describe(case="Case to open — start typing a name to search", quantity="How many to open")
@app_commands.autocomplete(case=_case_id_autocomplete)
@app_commands.choices(quantity=[
    app_commands.Choice(name=f"{q} cases ({int(d * 100)}% off)", value=q) for q, d in BULK_OPEN_DISCOUNTS.items()
])
async def bulk_open(interaction: discord.Interaction, case: str, quantity: int):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    case_id = case.lower()
    try:
        result = await _do_bulk_open_case(interaction.user.id, interaction.user.display_name, case_id, quantity)
        if not result["ok"]:
            if result["reason"] == "invalid_case":
                await interaction.followup.send("❌ Unknown case. Start typing a case name and pick one from the list that pops up.", ephemeral=True)
            elif result["reason"] == "insufficient_balance":
                embed = _themed_embed("❌ Insufficient Balance", description=f"You need ${result['total_cost']:.2f} to open {quantity} {result['case_data']['name']}s!", color=discord.Color.red())
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send("❌ Error: No items were generated. Please try again.", ephemeral=True)
            return
        await interaction.followup.send(embed=_bulk_open_embed(quantity, result), view=BulkOpenAgainView(case_id, quantity))
    except Exception as e:
        logger.error(f"Bulk open error: {e}")
        await interaction.followup.send(f"❌ Error opening cases: {str(e)[:100]}", ephemeral=True)

# ============================================
# STICKER CAPSULE COMMANDS
# ============================================

@bot.tree.command(name="capsules", description="View available sticker capsules")
async def list_capsules(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    embed = discord.Embed(title=f"📦 Available Sticker Capsules ({len(STICKER_CAPSULES)})", color=discord.Color.purple())
    for capsule_id, capsule_data in STICKER_CAPSULES.items():
        price = (await get_effective_capsule(capsule_id, capsule_data['price']))['price']
        embed.add_field(name=f"{fix_surrogate_emoji(capsule_data['emoji'])} {capsule_data['name']}", value=f"Price: ${price:.2f}\nUse: `/sticker {capsule_id}`", inline=True)
    embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
    await interaction.followup.send(embed=embed)

STICKER_RARITY_COLORS = {"👑 Legendary": 0xffd700, "👑 Epic": 0xaa00ff, "👑 Rare": 0x0066cc, "👑 Common": 0x00aa00, "🔥": 0xff4444, "💫": 0xff69b4, "✨": 0xaa00ff, "⭐": 0x0066cc}


async def _do_open_sticker(user_id: int, display_name: str, capsule_id: str) -> dict:
    """Deduct + roll + insert for a sticker capsule open. Shared by /sticker
    and StickerOpenAgainView's button."""
    if capsule_id not in STICKER_CAPSULES:
        return {"ok": False, "reason": "invalid_capsule"}
    capsule_data = STICKER_CAPSULES[capsule_id]
    price = (await get_effective_capsule(capsule_id, capsule_data['price']))['price']

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, display_name, conn)

            # Atomic deduct: WHERE balance >= $1 prevents negative balance under concurrency.
            deducted = await conn.fetchval(
                "UPDATE users SET balance = balance - $1 WHERE user_id = $2 AND balance >= $1 RETURNING balance",
                price, user_id
            )
            if deducted is None:
                return {"ok": False, "reason": "insufficient_balance", "price": price, "capsule_data": capsule_data}

            sticker = get_random_sticker(capsule_id)
            if not sticker:
                return {"ok": False, "reason": "roll_error"}

            await conn.execute("INSERT INTO inventory (user_id, item_name, item_type, rarity, price, is_stattrak) VALUES ($1, $2, 'sticker', $3, $4, $5)",
                              user_id, sticker['name'], sticker['rarity'], sticker['price'], sticker['is_stattrak'])

            new_balance = await get_balance(user_id, conn)

    return {"ok": True, "sticker": sticker, "price": price, "new_balance": new_balance, "capsule_data": capsule_data}


def _sticker_open_embed(capsule_id: str, result: dict) -> discord.Embed:
    sticker = result["sticker"]
    capsule_data = result["capsule_data"]
    image_filename = get_sticker_image(sticker['name'])
    embed = _themed_embed(
        f"⭐ Opening {fix_surrogate_emoji(capsule_data['emoji'])} {capsule_data['name']}...",
        color=STICKER_RARITY_COLORS.get(sticker['rarity'], 0x808080),
        item_image_url=f"/static/images/stickers/{image_filename}" if image_filename else None,
    )
    embed.add_field(name="✨ You got:", value=f"**{sticker['name']}**", inline=False)
    embed.add_field(name="Rarity", value=sticker['rarity'], inline=True)
    embed.add_field(name="Value", value=f"${sticker['price']:.2f}", inline=True)
    if sticker['is_stattrak']:
        embed.add_field(name="🔥 StatTrak™", value="Rare StatTrak™ variant!", inline=False)
    embed.add_field(name="💰 Cost", value=f"${result['price']:.2f}", inline=True)
    embed.add_field(name="💰 New Balance", value=f"${result['new_balance']:,.2f}", inline=True)
    return embed


class StickerOpenAgainView(discord.ui.View):
    """Same "no retyping" idea as OpenAgainView, for sticker capsules."""
    def __init__(self, capsule_id: str):
        super().__init__(timeout=None)
        self.capsule_id = capsule_id
        capsule_data = STICKER_CAPSULES.get(capsule_id, {})
        self.children[0].label = f"🔓 Open {capsule_data.get('name', capsule_id)} Again"

    @discord.ui.button(label="🔓 Open Again", style=discord.ButtonStyle.success)
    async def open_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        result = await _do_open_sticker(interaction.user.id, interaction.user.display_name, self.capsule_id)
        if not result["ok"]:
            if result["reason"] == "insufficient_balance":
                embed = _themed_embed("❌ Insufficient Balance", description=f"You need ${result['price']:.2f} to open this capsule!", color=discord.Color.red())
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send("❌ Error opening sticker capsule. Please try again.", ephemeral=True)
            return
        await interaction.followup.send(embed=_sticker_open_embed(self.capsule_id, result), view=StickerOpenAgainView(self.capsule_id))


@bot.tree.command(name="sticker", description="Open a sticker capsule")
@app_commands.describe(capsule="Capsule to open — start typing a name to search")
@app_commands.autocomplete(capsule=_capsule_id_autocomplete)
async def open_sticker(interaction: discord.Interaction, capsule: str):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    capsule_id = capsule.lower()
    try:
        result = await _do_open_sticker(interaction.user.id, interaction.user.display_name, capsule_id)
        if not result["ok"]:
            if result["reason"] == "invalid_capsule":
                await interaction.followup.send("❌ Unknown capsule. Start typing a capsule name and pick one from the list that pops up.", ephemeral=True)
            elif result["reason"] == "insufficient_balance":
                embed = _themed_embed("❌ Insufficient Balance", description=f"You need ${result['price']:.2f} to open this capsule!", color=discord.Color.red())
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send("❌ Error opening sticker capsule. Please try again.", ephemeral=True)
            return
        await interaction.followup.send(embed=_sticker_open_embed(capsule_id, result), view=StickerOpenAgainView(capsule_id))
    except Exception as e:
        logger.error(f"Open sticker error: {e}")
        await interaction.followup.send(f"❌ Error opening sticker: {str(e)[:100]}", ephemeral=True)

# ============================================
# INVENTORY COMMANDS
# ============================================

@bot.tree.command(name="inventory", description="View your inventory")
async def view_inventory(interaction: discord.Interaction, filter_type: str = None, search: str = None):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    async with db_pool.acquire() as conn:
        await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)
        
        query = "SELECT id, item_name, item_type, rarity, price, is_stattrak, float_value, condition FROM inventory WHERE user_id = $1 AND status = 'kept'"
        params = [interaction.user.id]

        if filter_type:
            filter_lower = filter_type.lower()
            if filter_lower == 'weapon':
                query += " AND item_type = 'weapon'"
            elif filter_lower == 'sticker':
                query += " AND item_type = 'sticker'"

        if search:
            query += " AND LOWER(item_name) LIKE $" + str(len(params) + 1)
            params.append(f"%{search.lower()}%")

        query += " ORDER BY created_at DESC"
        items = await conn.fetch(query, *params)

        if not items:
            embed = discord.Embed(title="📦 Inventory", description="Your inventory is empty! Open some cases with `/open` or `/sticker`", color=discord.Color.blue())
            embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
            await interaction.followup.send(embed=embed)
            return

        view = InventoryView(items, interaction.user)
        response = await interaction.followup.send(embed=view.get_embed(), view=view)
        view.message = response

# ============================================
# SELL COMMAND
# ============================================

async def _do_sell_item(user_id: int, display_name: str, item_id: int) -> dict:
    """Shared by /sell and the /inventory Gallery's 💰 Sell button."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, display_name, conn)

            item = await conn.fetchrow(
                "UPDATE inventory SET status='sold' WHERE id=$1 AND user_id=$2 AND status='kept' AND protected=FALSE RETURNING *",
                item_id, user_id
            )
            if not item:
                # Distinguish "protected" from "not found" -- same check the
                # web dashboard's own /api/sell-item makes.
                still_there = await conn.fetchval(
                    "SELECT protected FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept'",
                    item_id, user_id
                )
                if still_there:
                    return {"ok": False, "reason": "protected"}
                return {"ok": False, "reason": "not_found"}

            price_value = float(item['price']) if isinstance(item['price'], Decimal) else item['price']
            sell_price = int(price_value * 0.7)
            old_balance = await get_balance(user_id, conn)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", sell_price, user_id)
            await update_quest_progress(user_id, "sell_items", 1, conn)
            new_balance = await get_balance(user_id, conn)

    return {"ok": True, "item": item, "price_value": price_value, "sell_price": sell_price, "old_balance": old_balance, "new_balance": new_balance}


def _sell_result_embed(result: dict) -> discord.Embed:
    item = result["item"]
    embed = _themed_embed(
        "💰 Item Sold!", color=discord.Color.green(),
        rarity=item.get('rarity'), item_image_url=item.get('image_url'),
    )
    embed.add_field(name="Sold", value=item['item_name'], inline=False)
    embed.add_field(name="Received", value=f"${result['sell_price']:,.2f}", inline=True)
    embed.add_field(name="Original Value", value=f"${result['price_value']:,.2f}", inline=True)
    if item.get('float_value') is not None:
        embed.add_field(name="🔢 Float", value=f"{item['float_value']:.4f}", inline=True)
    embed.add_field(name="Previous Balance", value=f"${result['old_balance']:,.2f}", inline=True)
    embed.add_field(name="New Balance", value=f"${result['new_balance']:,.2f}", inline=True)
    return embed


@bot.tree.command(name="sell", description="Sell an item from your inventory")
@app_commands.describe(item_id="Item to sell — start typing its name to search")
@app_commands.autocomplete(item_id=_sell_item_autocomplete)
async def sell_item(interaction: discord.Interaction, item_id: int):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    try:
        result = await _do_sell_item(interaction.user.id, interaction.user.display_name, item_id)
        if not result["ok"]:
            if result["reason"] == "protected":
                await interaction.followup.send("❌ This item is protected — unprotect it first to sell.", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Item ID {item_id} not found in your inventory! Use `/inventory` to see your items.", ephemeral=True)
            return
        await interaction.followup.send(embed=_sell_result_embed(result))
    except Exception as e:
        logger.error(f"Sell error: {e}")
        await interaction.followup.send(f"❌ Error selling item: {str(e)[:100]}", ephemeral=True)

# ============================================
# STICKER APPLICATION COMMANDS
# ============================================

@bot.tree.command(name="apply_sticker", description="Apply a sticker from your inventory to a weapon")
@app_commands.describe(weapon_id="Weapon to apply the sticker to — search by name", sticker_id="Sticker to apply — search by name", slot="Sticker slot")
@app_commands.autocomplete(weapon_id=_weapon_id_autocomplete, sticker_id=_sticker_id_autocomplete)
@app_commands.choices(slot=[app_commands.Choice(name=f"Slot {i}", value=i) for i in range(4)])
async def cmd_apply_sticker(interaction: discord.Interaction, weapon_id: int, sticker_id: int, slot: int = 0):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()
    try:
        import json as _json
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                weapon = await conn.fetchrow(
                    "SELECT id, item_name, item_type, applied_stickers FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept' FOR UPDATE",
                    weapon_id, interaction.user.id
                )
                if not weapon:
                    await interaction.followup.send(f"❌ Weapon ID {weapon_id} not found in your inventory.", ephemeral=True)
                    return
                if (weapon["item_type"] or "weapon") not in ("weapon", "gold"):
                    await interaction.followup.send("❌ You can only apply stickers to weapons.", ephemeral=True)
                    return
                sticker = await conn.fetchrow(
                    "SELECT id, item_name, rarity, image_url FROM inventory WHERE id=$1 AND user_id=$2 AND item_type='sticker' AND status='kept' FOR UPDATE",
                    sticker_id, interaction.user.id
                )
                if not sticker:
                    await interaction.followup.send(f"❌ Sticker ID {sticker_id} not found in your inventory.", ephemeral=True)
                    return
                raw = weapon["applied_stickers"]
                current = _json.loads(raw) if isinstance(raw, str) else (list(raw) if raw else [])
                if any(s.get("slot") == slot for s in current):
                    await interaction.followup.send(f"❌ Slot {slot} already has a sticker. Use `/remove_sticker` first.", ephemeral=True)
                    return
                if len(current) >= 4:
                    await interaction.followup.send("❌ All 4 sticker slots are full.", ephemeral=True)
                    return
                current.append({
                    "slot": slot, "sticker_id": sticker_id,
                    "sticker_name": sticker["item_name"],
                    "sticker_image": sticker["image_url"] or "",
                    "rarity": sticker["rarity"] or "",
                })
                await conn.execute(
                    "UPDATE inventory SET applied_stickers=$1 WHERE id=$2",
                    _json.dumps(current), weapon_id
                )
                await conn.execute("UPDATE inventory SET status='sold' WHERE id=$1", sticker_id)

        embed = discord.Embed(title="🏷️ Sticker Applied!", color=discord.Color.blue())
        embed.add_field(name="Weapon", value=weapon["item_name"], inline=True)
        embed.add_field(name="Sticker", value=sticker["item_name"], inline=True)
        embed.add_field(name="Slot", value=str(slot + 1), inline=True)
        embed.add_field(name="Stickers Applied", value=f"{len(current)}/4", inline=True)
        embed.set_footer(text=f"💖 {KO_FI_URL} | 🌐 {DASHBOARD_URL}")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"apply_sticker error: {e}")
        await interaction.followup.send(f"❌ Error: {str(e)[:100]}", ephemeral=True)


@bot.tree.command(name="remove_sticker", description="Scrape a sticker off a weapon (sticker is lost)")
@app_commands.describe(weapon_id="Weapon to remove a sticker from — search by name", slot="Sticker slot")
@app_commands.autocomplete(weapon_id=_weapon_id_autocomplete)
@app_commands.choices(slot=[app_commands.Choice(name=f"Slot {i}", value=i) for i in range(4)])
async def cmd_remove_sticker(interaction: discord.Interaction, weapon_id: int, slot: int):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()
    try:
        import json as _json
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                weapon = await conn.fetchrow(
                    "SELECT id, item_name, applied_stickers FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept' FOR UPDATE",
                    weapon_id, interaction.user.id
                )
                if not weapon:
                    await interaction.followup.send(f"❌ Weapon ID {weapon_id} not found.", ephemeral=True)
                    return
                raw = weapon["applied_stickers"]
                current = _json.loads(raw) if isinstance(raw, str) else (list(raw) if raw else [])
                removed = next((s for s in current if s.get("slot") == slot), None)
                if not removed:
                    await interaction.followup.send(f"❌ No sticker in slot {slot}.", ephemeral=True)
                    return
                updated = [s for s in current if s.get("slot") != slot]
                await conn.execute(
                    "UPDATE inventory SET applied_stickers=$1 WHERE id=$2",
                    _json.dumps(updated), weapon_id
                )
        embed = discord.Embed(title="🗑️ Sticker Removed", color=discord.Color.orange())
        embed.add_field(name="Weapon", value=weapon["item_name"], inline=True)
        embed.add_field(name="Removed", value=removed.get("sticker_name", "Sticker"), inline=True)
        embed.add_field(name="Note", value="The sticker was scrapped and cannot be recovered.", inline=False)
        embed.set_footer(text=f"💖 {KO_FI_URL} | 🌐 {DASHBOARD_URL}")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"remove_sticker error: {e}")
        await interaction.followup.send(f"❌ Error: {str(e)[:100]}", ephemeral=True)


async def _do_toggle_loadout(user_id: int, item_id: int) -> dict:
    """Shared by /loadout_toggle and the /inventory Gallery's 🎽 Loadout button."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, item_name, in_loadout FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept' FOR UPDATE",
                item_id, user_id
            )
            if not row:
                return {"ok": False}

            # Toggling a bare in_loadout flag directly would drift out of sync
            # with the web dashboard's named-loadout system (server.py /
            # routes/loadouts.py), which tracks real membership via a
            # loadout_items join table and only uses in_loadout as a synced
            # cache of "in the currently ACTIVE loadout". Mirror that same
            # active-loadout-aware toggle here instead of a raw column flip,
            # or an item added via this command would vanish the next time
            # the web UI switches loadouts (since it was never actually
            # recorded as a loadout_items member).
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

    return {"ok": True, "item_name": row["item_name"], "in_loadout": new_val}


@bot.tree.command(name="loadout_toggle", description="Add or remove an item from your loadout")
@app_commands.describe(item_id="Weapon to add/remove from your loadout — search by name")
@app_commands.autocomplete(item_id=_loadout_item_autocomplete)
async def cmd_loadout_toggle(interaction: discord.Interaction, item_id: int):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()
    try:
        result = await _do_toggle_loadout(interaction.user.id, item_id)
        if not result["ok"]:
            await interaction.followup.send(f"❌ Item ID {item_id} not found.", ephemeral=True)
            return
        action = "added to" if result["in_loadout"] else "removed from"
        embed = discord.Embed(
            title="🎽 Loadout Updated",
            description=f"**{result['item_name']}** {action} your loadout.",
            color=discord.Color.gold() if result["in_loadout"] else discord.Color.light_grey()
        )
        embed.set_footer(text=f"Use /loadout to view your full loadout | 🌐 {DASHBOARD_URL}")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"loadout_toggle error: {e}")
        await interaction.followup.send(f"❌ Error: {str(e)[:100]}", ephemeral=True)


@bot.tree.command(name="loadout", description="View your equipped weapon loadout")
async def cmd_loadout(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()
    try:
        import json as _json
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, item_name, item_type, rarity, condition, float_value, is_stattrak, price, applied_stickers "
                "FROM inventory WHERE user_id=$1 AND in_loadout=TRUE AND status='kept' ORDER BY created_at DESC",
                interaction.user.id
            )
        if not rows:
            embed = discord.Embed(
                title="🎽 My Loadout",
                description="Your loadout is empty!\nUse `/loadout_toggle <item_id>` on any weapon to equip it.\nFind item IDs with `/inventory`.",
                color=discord.Color.gold()
            )
            embed.set_footer(text=f"🌐 Dashboard: {DASHBOARD_URL}")
            await interaction.followup.send(embed=embed)
            return

        embed = discord.Embed(title="🎽 My Loadout", color=discord.Color.gold())
        for item in rows[:10]:
            raw = item["applied_stickers"]
            stickers = _json.loads(raw) if isinstance(raw, str) else (list(raw) if raw else [])
            st_prefix = "🔥 StatTrak™ " if item["is_stattrak"] else ""
            cond = f" · {item['condition']}" if item["condition"] else ""
            fv   = f" · {float(item['float_value']):.4f}" if item.get("float_value") is not None else ""
            price = float(item["price"] or 0)
            sticker_line = ""
            if stickers:
                names = " · ".join(s.get("sticker_name", "Sticker") for s in sorted(stickers, key=lambda x: x.get("slot", 0)))
                sticker_line = f"\n🏷️ {names}"
            embed.add_field(
                name=f"{st_prefix}{item['item_name']}",
                value=f"`ID: {item['id']}`{cond}{fv} · ${price:,.2f}{sticker_line}",
                inline=False
            )
        if len(rows) > 10:
            embed.set_footer(text=f"Showing 10/{len(rows)} items · Full loadout at {DASHBOARD_URL}")
        else:
            embed.set_footer(text=f"🌐 Full view at {DASHBOARD_URL}")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"loadout error: {e}")
        await interaction.followup.send(f"❌ Error: {str(e)[:100]}", ephemeral=True)


# ============================================
# QUESTS COMMANDS
# ============================================

@bot.tree.command(name="quests", description="View your daily quests")
async def view_quests(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    user_id = interaction.user.id

    async with db_pool.acquire() as conn:
        await ensure_user_exists(user_id, interaction.user.display_name, conn)
        
        user = await conn.fetchrow("SELECT user_id FROM users WHERE user_id = $1", user_id)
        if not user:
            await conn.execute("INSERT INTO users (user_id, balance) VALUES ($1, $2)", user_id, 1000)
        
        quests = await conn.fetch("SELECT * FROM quests WHERE user_id = $1 AND claimed = false", user_id)
        if not quests:
            await create_daily_quests(user_id, conn)
            quests = await conn.fetch("SELECT * FROM quests WHERE user_id = $1 AND claimed = false", user_id)

        unique_quests = {}
        for quest in quests:
            if quest['quest_type'] not in unique_quests:
                unique_quests[quest['quest_type']] = quest

        embed = discord.Embed(title="📋 Daily Quests", color=discord.Color.purple(), timestamp=datetime.now(timezone.utc))
        quest_names = {"open_cases": "🔑 Open Cases", "get_golds": "✨ Find Gold Items", "earn_money": "💰 Earn Money", "trade_up": "🔄 Complete Trade-Ups", "sell_items": "💸 Sell Items", "jackpot_win": "🎲 Win Jackpot", "daily_streak": "📅 Maintain Daily Streak"}
        completed_count = 0

        for quest_type, quest in unique_quests.items():
            name = quest_names.get(quest_type, quest_type)
            status = "✅ COMPLETED" if quest['completed'] else f"Progress: {quest['progress']}/{quest['required']}"
            embed.add_field(name=name, value=f"{status}\nReward: ${quest['reward']:,}", inline=False)
            if quest['completed']:
                completed_count += 1

        if completed_count == len(unique_quests):
            embed.set_footer(text="All quests completed! Use /claim to collect your rewards!")
        else:
            embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")

        await interaction.followup.send(embed=embed)

@bot.tree.command(name="claim", description="Claim completed quest rewards")
async def claim_quests(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    user_id = interaction.user.id

    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, interaction.user.display_name, conn)

                # Atomic claim: UPDATE ... RETURNING prevents double-payout from
                # concurrent requests (same TOCTOU fix as the web /api/claim endpoint).
                claimed = await conn.fetch(
                    "UPDATE quests SET claimed = true WHERE user_id = $1 AND completed = true AND claimed = false RETURNING reward",
                    user_id
                )
                if not claimed:
                    await interaction.followup.send("❌ No completed quests to claim!", ephemeral=True)
                    return

                total_reward = sum(r['reward'] for r in claimed)
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", total_reward, user_id)

                new_balance = await get_balance(user_id, conn)

                embed = discord.Embed(title="🎉 Quests Claimed!", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
                embed.add_field(name="Total Reward", value=f"${total_reward:,.2f}", inline=False)
                embed.add_field(name="New Balance", value=f"${new_balance:,.2f}", inline=True)
                embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
                await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"Claim quests error: {e}")
        await interaction.followup.send(f"❌ Error claiming quests: {str(e)[:100]}", ephemeral=True)

# ============================================
# LEADERBOARD COMMANDS
# ============================================

@bot.tree.command(name="leaderboard_money", description="View richest users")
async def lb_money(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    async with db_pool.acquire() as conn:
        top_users = await conn.fetch("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 10")
        embed = _themed_embed("💰 Richest Users", color=discord.Color.gold())

        for idx, user in enumerate(top_users, 1):
            try:
                member = await bot.fetch_user(user['user_id'])
                medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
                embed.add_field(name=f"{medal} {member.display_name}", value=f"${user['balance']:,.2f}", inline=False)
            except Exception:
                pass

        await interaction.followup.send(embed=embed)

@bot.tree.command(name="leaderboard_opens", description="View most cases opened")
async def lb_opens(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    async with db_pool.acquire() as conn:
        top_users = await conn.fetch("SELECT user_id, total_opens FROM users ORDER BY total_opens DESC LIMIT 10")
        embed = _themed_embed("🔑 Most Cases Opened", color=discord.Color.blue())

        for idx, user in enumerate(top_users, 1):
            if user['total_opens'] > 0:
                try:
                    member = await bot.fetch_user(user['user_id'])
                    medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
                    embed.add_field(name=f"{medal} {member.display_name}", value=f"{user['total_opens']} cases", inline=False)
                except Exception:
                    pass

        await interaction.followup.send(embed=embed)

@bot.tree.command(name="leaderboard_golds", description="View most gold items found")
async def lb_golds(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    async with db_pool.acquire() as conn:
        top_users = await conn.fetch("SELECT user_id, total_golds FROM users ORDER BY total_golds DESC LIMIT 10")
        embed = _themed_embed("✨ Most Gold Items", color=discord.Color.gold())

        for idx, user in enumerate(top_users, 1):
            if user['total_golds'] > 0:
                try:
                    member = await bot.fetch_user(user['user_id'])
                    medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
                    embed.add_field(name=f"{medal} {member.display_name}", value=f"{user['total_golds']} golds", inline=False)
                except Exception:
                    pass

        await interaction.followup.send(embed=embed)

@bot.tree.command(name="leaderboard_trades", description="View most trade-ups completed")
async def lb_trades(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    async with db_pool.acquire() as conn:
        top_users = await conn.fetch("SELECT user_id, total_trades FROM users ORDER BY total_trades DESC LIMIT 10")
        embed = _themed_embed("🔄 Most Trade-Ups", color=discord.Color.purple())

        for idx, user in enumerate(top_users, 1):
            if user['total_trades'] > 0:
                try:
                    member = await bot.fetch_user(user['user_id'])
                    medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
                    embed.add_field(name=f"{medal} {member.display_name}", value=f"{user['total_trades']} trades", inline=False)
                except Exception:
                    pass

        await interaction.followup.send(embed=embed)

# ============================================
# JACKPOT COMMANDS
# ============================================

@bot.tree.command(name="jackpot", description="Join the jackpot (minimum $100)")
async def jackpot(interaction: discord.Interaction, amount: float):
    if not await is_bot_channel(interaction):
        return

    if amount < 100:
        await interaction.response.send_message("❌ Minimum bet is $100!", ephemeral=True)
        return

    await interaction.response.defer()

    await ensure_user_exists(interaction.user.id, interaction.user.display_name)

    # Fix 3 + Fix 7: DB-backed entry; determine winner inside lock but send outside
    winner_id = None
    win_amount = 0
    pot_total = 0.0

    async with jackpot_lock:
        success = await jackpot_enter(interaction.user.id, amount)
    if not success:
        await interaction.followup.send("❌ Insufficient balance!", ephemeral=True)
        return
    async with jackpot_lock:

        # Read current state to decide if jackpot should draw
        async with db_pool.acquire() as conn:
            pot_row    = await conn.fetchrow("SELECT pot FROM jackpot_state WHERE id = 1")
            entry_count = await conn.fetchval("SELECT COUNT(*) FROM jackpot_entries")
            pot_total   = float(pot_row['pot']) if pot_row else 0.0

        should_draw = entry_count >= 3 or pot_total >= 5000

        if should_draw:
            # Fix 7: draw winner inside lock (DB txn), then release before sending
            winner_id, win_amount, pot_total = await jackpot_draw()
            if winner_id:
                async with db_pool.acquire() as conn:
                    await update_quest_progress(winner_id, "jackpot_win", 1, conn)

    # Network I/O is now OUTSIDE the lock (Fix 7)
    if winner_id:
        try:
            winner_user = await bot.fetch_user(winner_id)
            winner_name = winner_user.display_name
        except Exception:
            winner_name = str(winner_id)

        winner_embed = discord.Embed(title="🏆 JACKPOT WINNER!", color=discord.Color.gold())
        winner_embed.add_field(name="Winner", value=winner_name, inline=False)
        winner_embed.add_field(name="Won",       value=f"${win_amount:,.2f}", inline=True)
        winner_embed.add_field(name="Total Pot", value=f"${pot_total:,.2f}",  inline=True)
        winner_embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
        await interaction.followup.send(embed=winner_embed)
    else:
        # Still gathering entries
        async with db_pool.acquire() as conn:
            pot_row    = await conn.fetchrow("SELECT pot FROM jackpot_state WHERE id = 1")
            entry_count = await conn.fetchval("SELECT COUNT(*) FROM jackpot_entries")
            pot_total   = float(pot_row['pot']) if pot_row else 0.0

        embed = discord.Embed(title="🎲 Joined Jackpot!", color=discord.Color.green())
        embed.add_field(name="Your Bet",       value=f"${amount:,.2f}",   inline=True)
        embed.add_field(name="Total Pot",      value=f"${pot_total:,.2f}", inline=True)
        embed.add_field(name="Total Players",  value=str(entry_count),    inline=True)
        embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
        await interaction.followup.send(embed=embed)

# ============================================
# TRADE-UP COMMANDS
# ============================================

# ============================================
# TRADE-UP COMMANDS  (Fix 15: unified helper)
# ============================================

async def _run_tradeup(
    interaction: discord.Interaction,
    input_rarity: str,
    required_count: int = 10
):
    """
    Fix 2 + Fix 15: Generic trade-up with FOR UPDATE SKIP LOCKED to prevent
    race conditions, and a single code path for all rarity levels.
    """
    output_rarity = TRADE_UP_PROGRESSION.get(input_rarity)
    if not output_rarity:
        await interaction.response.send_message("❌ Invalid rarity for trade-up.", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)

                items = await conn.fetch("""
                    SELECT id, item_name, price FROM inventory
                    WHERE user_id = $1 AND rarity = $2 AND status = 'kept'
                    ORDER BY price ASC
                    LIMIT $3
                    FOR UPDATE SKIP LOCKED
                """, interaction.user.id, input_rarity, required_count)

                if len(items) < required_count:
                    await interaction.followup.send(
                        f"❌ You need {required_count} {input_rarity} items. You only have {len(items)} available.",
                        ephemeral=True
                    )
                    return

                item_ids_to_delete = [r['id'] for r in items]
                await conn.execute("DELETE FROM inventory WHERE id = ANY($1::int[])", item_ids_to_delete)

                possible_items = list(ALL_ITEMS_BY_RARITY.get(output_rarity, []))
                if not possible_items:
                    possible_items = [{"name": f"Mystery {output_rarity} Item", "condition": "Field-Tested", "tier": None}]

                new_item_template = secure_choice(possible_items)
                float_value       = generate_skin_float()
                condition         = get_skin_condition(float_value)
                is_stattrak       = secure_random() < 0.1
                new_value         = calculate_item_value(output_rarity, condition, None, is_stattrak)
                name              = f"{'StatTrak™ ' if is_stattrak else ''}{new_item_template['name']}"

                await conn.execute("""
                    INSERT INTO inventory
                        (user_id, item_name, item_type, rarity, price, condition, is_stattrak, float_value)
                    VALUES ($1, $2, 'weapon', $3, $4, $5, $6, $7)
                """, interaction.user.id, name, output_rarity, new_value, condition, is_stattrak, float_value)
                await conn.execute(
                    "UPDATE users SET total_trades = total_trades + 1 WHERE user_id = $1",
                    interaction.user.id
                )
                await update_quest_progress(interaction.user.id, "trade_up", 1, conn)

        rarity_emoji = RARITY_EMOJIS.get(output_rarity, "")
        embed = discord.Embed(
            title=f"🔄 Trade-Up Complete! ({input_rarity} → {output_rarity})",
            color=discord.Color.purple()
        )
        embed.add_field(name="Received",    value=f"{rarity_emoji} **{name}**", inline=False)
        embed.add_field(name="Rarity",      value=f"{rarity_emoji} {output_rarity}", inline=True)
        embed.add_field(name="🔢 Float",    value=f"{float_value:.4f}",  inline=True)
        embed.add_field(name="Value",       value=f"${new_value:,.2f}",  inline=True)
        if is_stattrak:
            embed.add_field(name="🔥 StatTrak™", value="Rare variant!", inline=False)
        embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"Tradeup error ({input_rarity}→{output_rarity}): {e}")
        await interaction.followup.send(f"❌ Error during trade-up: {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="tradeup", description="Trade 10 Blue weapons for 1 Purple weapon")
async def tradeup_weapons(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return
    await _run_tradeup(interaction, "Blue")

@bot.tree.command(name="tradeup_purple", description="Trade 10 Purple weapons for 1 Pink weapon")
async def tradeup_purple(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return
    await _run_tradeup(interaction, "Purple")

@bot.tree.command(name="tradeup_pink", description="Trade 10 Pink weapons for 1 Red weapon")
async def tradeup_pink(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return
    await _run_tradeup(interaction, "Pink")

# ============================================
# QUICK TRADE COMMAND
# ============================================

@bot.tree.command(name="quicktrade", description="Quick trade-up - randomly selects items from your inventory")
@app_commands.describe(rarity="Rarity tier to trade up from")
@app_commands.choices(rarity=[
    app_commands.Choice(name="🟦 Blue → Purple", value="blue"),
    app_commands.Choice(name="🟪 Purple → Pink", value="purple"),
    app_commands.Choice(name="💗 Pink → Red", value="pink"),
])
async def quick_tradeup(interaction: discord.Interaction, rarity: str):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    rarity_map = {
        'blue': {'rarity': 'Blue', 'next': 'Purple', 'count': 10, 'emoji': '🟦'},
        'purple': {'rarity': 'Purple', 'next': 'Pink', 'count': 10, 'emoji': '🟪'},
        'pink': {'rarity': 'Pink', 'next': 'Red', 'count': 10, 'emoji': '💗'}
    }

    if rarity.lower() not in rarity_map:
        await interaction.followup.send("❌ Invalid rarity! Use: `blue`, `purple`, or `pink`", ephemeral=True)
        return

    config = rarity_map[rarity.lower()]

    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)
                
                items = await conn.fetch(
                    "SELECT id, item_name FROM inventory WHERE user_id = $1 AND rarity = $2 AND item_type = 'weapon' AND status = 'kept' LIMIT $3 FOR UPDATE SKIP LOCKED",
                    interaction.user.id, config['rarity'], config['count']
                )

                if len(items) < config['count']:
                    await interaction.followup.send(f"❌ You need {config['count']} {config['rarity']} items for quick trade-up! Only {len(items)} available (other items may be locked).", ephemeral=True)
                    return

                selected_items = list(items)
                selected_ids = [item['id'] for item in selected_items]

                for item_id in selected_ids:
                    await conn.execute("DELETE FROM inventory WHERE id = $1", item_id)

                is_stattrak = secure_random() < 0.1
                possible_items = list(ALL_ITEMS_BY_RARITY.get(config['next'], []))

                if not possible_items:
                    possible_items = [{"name": f"Mystery {config['next']} Item", "condition": "Field-Tested"}]

                new_item_template = secure_choice(possible_items)

                float_value = generate_skin_float()
                condition_from_float = get_skin_condition(float_value)
                
                new_value = calculate_item_value(config['next'], condition_from_float, None, is_stattrak)
                
                name = f"{'StatTrak™ ' if is_stattrak else ''}{new_item_template['name']}"

                await conn.execute("""INSERT INTO inventory 
                    (user_id, item_name, item_type, rarity, price, condition, is_stattrak, float_value) 
                    VALUES ($1, $2, 'weapon', $3, $4, $5, $6, $7)""",
                    interaction.user.id, name, config['next'], new_value, condition_from_float, is_stattrak, float_value)
                await conn.execute("UPDATE users SET total_trades = total_trades + 1 WHERE user_id = $1", interaction.user.id)
                await update_quest_progress(interaction.user.id, "trade_up", 1, conn)

                rarity_emoji = RARITY_EMOJIS.get(config['next'], config['emoji'])

                embed = discord.Embed(title=f"🔄 Quick Trade-Up Complete! ({config['rarity']} → {config['next']})", color=discord.Color.purple())
                embed.add_field(name="Traded Items", value=f"{config['count']} random {config['rarity']} items", inline=False)
                embed.add_field(name="Traded IDs", value=", ".join(str(id) for id in selected_ids), inline=False)
                embed.add_field(name="Received", value=f"{rarity_emoji} **{name}**", inline=False)
                embed.add_field(name="Rarity", value=f"{rarity_emoji} {config['next']}", inline=True)
                embed.add_field(name="🔢 Float", value=f"{float_value:.4f}", inline=True)
                embed.add_field(name="Value", value=f"${new_value:,.2f}", inline=True)
                if is_stattrak:
                    embed.add_field(name="🔥 StatTrak™", value="Rare variant!", inline=False)
                embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")

                await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"Quick trade error: {e}")
        await interaction.followup.send(f"❌ Error during trade-up: {str(e)[:100]}", ephemeral=True)

# ============================================
# ADMIN STATS COMMAND
# ============================================

@bot.tree.command(name="stats", description="View bot statistics (Admin only)")
@app_commands.default_permissions(administrator=True)
async def bot_stats(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    try:
        async with db_pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            total_balance = await conn.fetchval("SELECT COALESCE(SUM(balance), 0) FROM users")
            total_opens = await conn.fetchval("SELECT COALESCE(SUM(total_opens), 0) FROM users")
            total_golds = await conn.fetchval("SELECT COALESCE(SUM(total_golds), 0) FROM users")
            total_trades = await conn.fetchval("SELECT COALESCE(SUM(total_trades), 0) FROM users")

            most_valuable = await conn.fetchrow("SELECT item_name, price FROM inventory WHERE price IS NOT NULL ORDER BY price DESC LIMIT 1")
            total_inv_value = await conn.fetchval("SELECT COALESCE(SUM(price), 0) FROM inventory WHERE status = 'kept'")

            embed = discord.Embed(title="📊 Bot Statistics", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
            embed.add_field(name="👥 Total Users", value=f"{total_users:,}", inline=True)
            embed.add_field(name="💰 Total Economy Balance", value=f"${float(total_balance):,.2f}", inline=True)
            embed.add_field(name="📦 Total Cases Opened", value=f"{total_opens:,}", inline=True)
            embed.add_field(name="✨ Total Golds Found", value=f"{total_golds:,}", inline=True)
            embed.add_field(name="🔄 Total Trade-Ups", value=f"{total_trades:,}", inline=True)
            embed.add_field(name="💎 Total Inventory Value", value=f"${float(total_inv_value):,.2f}", inline=True)

            if most_valuable and most_valuable['item_name']:
                embed.add_field(name="🏆 Most Valuable Item", value=f"{most_valuable['item_name']} (${float(most_valuable['price']):,.2f})", inline=False)

            embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
            await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"Stats error: {e}")
        await interaction.followup.send(f"❌ Error fetching statistics: {str(e)[:100]}", ephemeral=True)

# ============================================
# GUILD SETTINGS COMMANDS
# ============================================

@bot.tree.command(name="setchannel", description="Set the channel for bot commands (Admin only)")
@app_commands.default_permissions(administrator=True)
async def set_bot_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need Administrator permissions to use this command!", ephemeral=True)
        return
    
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings (guild_id, name, bot_channel_id, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (guild_id) DO UPDATE SET
            bot_channel_id = $3, updated_at = NOW()
        """, interaction.guild_id, interaction.guild.name, channel.id)
    
    embed = discord.Embed(
        title="✅ Bot Channel Set!",
        description=f"Bot commands will now only work in {channel.mention}",
        color=discord.Color.green()
    )
    embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="removechannel", description="Remove channel restriction (Admin only)")
@app_commands.default_permissions(administrator=True)
async def remove_bot_channel(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need Administrator permissions to use this command!", ephemeral=True)
        return
    
    async with db_pool.acquire() as conn:
        await conn.execute("""
            UPDATE guild_settings SET bot_channel_id = NULL, updated_at = NOW() WHERE guild_id = $1
        """, interaction.guild_id)
    
    embed = _themed_embed(
        "✅ Channel Restriction Removed!",
        description="Bot commands can now be used in any channel",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="settimezone", description="Set this server's timezone for scheduled reminders (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(offset_hours="Hours from UTC, e.g. -5 for US Eastern, 1 for Central Europe")
async def cmd_set_timezone(interaction: discord.Interaction, offset_hours: app_commands.Range[int, -12, 14]):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need Administrator permissions to use this command!", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("❌ This command only works in a server.", ephemeral=True)
        return
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings (guild_id, name, timezone_offset, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (guild_id) DO UPDATE SET timezone_offset=$3, updated_at=NOW()
        """, interaction.guild_id, interaction.guild.name, offset_hours)
    sign = '+' if offset_hours >= 0 else ''
    embed = _themed_embed(
        "🕐 Timezone Set",
        description=(
            f"This server's timezone is now **UTC{sign}{offset_hours}**. "
            "Daily/weekly reward reminders will fire at 7am server-local time."
        ),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)

# ============================================
# GIVEAWAY COMMANDS
# ============================================

@bot.tree.command(name="giveaway_create", description="Create a giveaway (Admin only)")
@app_commands.default_permissions(administrator=True)
async def create_giveaway(interaction: discord.Interaction, prize: str, duration_minutes: int, winners: int = 1):
    if not await is_bot_channel(interaction):
        return

    if duration_minutes < 1 or duration_minutes > 10080:
        await interaction.response.send_message("❌ Duration must be between 1 minute and 7 days!", ephemeral=True)
        return
    if winners < 1 or winners > 10:
        await interaction.response.send_message("❌ Winners must be between 1 and 10!", ephemeral=True)
        return

    await interaction.response.defer()

    end_time = datetime.utcnow() + timedelta(minutes=duration_minutes)
    embed = discord.Embed(title="🎉 GIVEAWAY! 🎉", color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Prize", value=prize, inline=False)
    embed.add_field(name="Winners", value=winners, inline=True)
    embed.add_field(name="Ends", value=f"<t:{int(end_time.timestamp())}:R>", inline=True)
    embed.add_field(name="How to Enter", value="Click the 🎉 button below!", inline=False)
    embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            giveaway_id_result = await conn.fetchrow(
                "INSERT INTO giveaways (message_id, channel_id, prize, winner_count, end_time) VALUES ($1, $2, $3, $4, $5) RETURNING id",
                0, interaction.channel_id, prize, winners, end_time
            )
            giveaway_id = giveaway_id_result['id']

    view = discord.ui.View(timeout=duration_minutes * 60)
    button = discord.ui.Button(emoji="🎉", label="Enter Giveaway", style=discord.ButtonStyle.primary)

    async def button_callback(button_interaction: discord.Interaction):
        async with db_pool.acquire() as conn:
            await ensure_user_exists(button_interaction.user.id, button_interaction.user.display_name, conn)
            result = await conn.execute(
                "INSERT INTO giveaway_entries (giveaway_id, user_id) VALUES ($1, $2) ON CONFLICT (giveaway_id, user_id) DO NOTHING",
                giveaway_id, button_interaction.user.id
            )
        if result == "INSERT 0 0":
            await button_interaction.response.send_message("❌ You already entered this giveaway!", ephemeral=True)
        else:
            await button_interaction.response.send_message("✅ You entered the giveaway! Good luck!", ephemeral=True)

    button.callback = button_callback
    view.add_item(button)
    await interaction.followup.send(embed=embed, view=view)
    msg = await interaction.original_response()

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE giveaways SET message_id = $1 WHERE id = $2", msg.id, giveaway_id)

    asyncio.create_task(_run_giveaway(giveaway_id, duration_minutes * 60))


async def _run_giveaway(giveaway_id: int, delay_seconds: float):
    """Run a single giveaway — safe to call at startup for recovery."""
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    async with db_pool.acquire() as conn:
        # Atomic claim: only one concurrent caller (timer vs startup recovery) wins;
        # the second sees no row returned and exits early.
        giveaway = await conn.fetchrow(
            "UPDATE giveaways SET ended = true WHERE id = $1 AND ended = false RETURNING *",
            giveaway_id
        )
        if not giveaway:
            return
        channel = bot.get_channel(giveaway['channel_id'])
        if channel is None:
            return
        entries = await conn.fetch("SELECT user_id FROM giveaway_entries WHERE giveaway_id = $1", giveaway_id)
        if not entries:
            await channel.send(f"🎉 Giveaway for **{giveaway['prize']}** ended with no entries!")
        else:
            winners_list = secure_shuffle([e['user_id'] for e in entries])[:min(giveaway['winner_count'], len(entries))]
            winner_mentions = []
            for winner_id in winners_list:
                try:
                    user = await bot.fetch_user(winner_id)
                    winner_mentions.append(user.mention)
                except Exception:
                    winner_mentions.append(f"<@{winner_id}>")
            result_embed = discord.Embed(title="🏆 GIVEAWAY WINNERS! 🏆", color=discord.Color.gold())
            result_embed.add_field(name="Prize", value=giveaway['prize'], inline=False)
            result_embed.add_field(name="Winners", value=", ".join(winner_mentions), inline=False)
            result_embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
            await channel.send(embed=result_embed)

# ============================================
# SCHEDULED ENGAGEMENT BROADCASTS
# (per-guild daily/weekly/hourly reminders + hourly highlights)
# ============================================

# Reused thresholds -- not invented for Discord, matching what the website
# already uses so a "highlight" here means the same thing it does there:
# static/big-win.js's 10x multiplier threshold for its own big-win FX, and
# a flat $500 win amount (server.py's /api/lobby-ticker uses this for its
# own slots-win notability check, generalized here to any game).
BIG_WIN_MULTIPLIER_THRESHOLD = 10.0
BIG_WIN_AMOUNT_THRESHOLD = 500.0
HEAVY_ACTIVITY_GAME_COUNT = 15  # games logged in the last hour to count as "heavily active"
_ENGAGEMENT_LOOP_INTERVAL = 300  # 5 minutes
_last_hourly_highlight_utc_hour = None  # module-level: dedupes the once-per-UTC-hour broadcast


async def _get_today_leaderboard(conn, limit=5):
    """Today's top winners by total winnings (guild-timezone-agnostic --
    just UTC calendar-day, same as the website's own /api/stats/wins-today)."""
    return await conn.fetch("""
        SELECT u.user_id, u.username, COALESCE(SUM(gl.win_amount), 0) AS total_won
        FROM game_logs gl
        JOIN users u ON gl.user_id = u.user_id
        WHERE gl.created_at >= CURRENT_DATE
        GROUP BY u.user_id, u.username
        HAVING COALESCE(SUM(gl.win_amount), 0) > 0
        ORDER BY total_won DESC
        LIMIT $1
    """, limit)


def _leaderboard_lines(rows):
    lines = []
    for idx, row in enumerate(rows, 1):
        medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
        username = row['username'] or f"User {row['user_id']}"
        lines.append(f"{medal} {username} — ${float(row['total_won']):,.2f}")
    return lines


async def _append_today_leaderboard_field(embed: discord.Embed, limit: int = 3):
    """Appends a small 'Today's Top N' field -- shared by /hourly's own
    response (the literal "add it to the hourly claim" ask) and the
    scheduled daily broadcast below, so both stay based on the same query."""
    try:
        async with db_pool.acquire() as conn:
            rows = await _get_today_leaderboard(conn, limit=limit)
        if not rows:
            return
        embed.add_field(name="🏆 Today's Top 3", value="\n".join(_leaderboard_lines(rows)), inline=False)
    except Exception as e:
        logger.warning(f"today leaderboard field failed: {e}")


async def _get_hourly_highlights(conn):
    big_wins = await conn.fetch("""
        SELECT gl.user_id, u.username, gl.game_type, gl.win_amount, gl.multiplier
        FROM game_logs gl
        JOIN users u ON gl.user_id = u.user_id
        WHERE gl.created_at >= NOW() - INTERVAL '1 hour'
          AND (gl.multiplier >= $1 OR gl.win_amount >= $2)
        ORDER BY gl.win_amount DESC
        LIMIT 5
    """, BIG_WIN_MULTIPLIER_THRESHOLD, BIG_WIN_AMOUNT_THRESHOLD)
    heavy_activity = await conn.fetch("""
        SELECT gl.user_id, u.username, COUNT(*) AS game_count
        FROM game_logs gl
        JOIN users u ON gl.user_id = u.user_id
        WHERE gl.created_at >= NOW() - INTERVAL '1 hour'
        GROUP BY gl.user_id, u.username
        HAVING COUNT(*) >= $1
        ORDER BY game_count DESC
        LIMIT 3
    """, HEAVY_ACTIVITY_GAME_COUNT)
    return big_wins, heavy_activity


async def _broadcast_hourly_highlights():
    async with db_pool.acquire() as conn:
        big_wins, heavy_activity = await _get_hourly_highlights(conn)
        guilds = await conn.fetch("SELECT guild_id, bot_channel_id FROM guild_settings WHERE bot_channel_id IS NOT NULL")

    if not big_wins and not heavy_activity:
        return  # nothing notable this hour -- skip the broadcast entirely rather than post an empty embed

    embed = _themed_embed("🔥 Highlights from the Last Hour", color=discord.Color.orange())
    if big_wins:
        lines = []
        for w in big_wins:
            username = w['username'] or f"User {w['user_id']}"
            mult = float(w['multiplier'] or 0)
            lines.append(f"**{username}** won **${float(w['win_amount']):,.2f}** on {w['game_type']} ({mult:.1f}x)")
        embed.add_field(name="🏆 Big Wins", value="\n".join(lines), inline=False)
    if heavy_activity:
        lines = []
        for u in heavy_activity:
            username = u['username'] or f"User {u['user_id']}"
            lines.append(f"**{username}** played {u['game_count']} games!")
        embed.add_field(name="⚡ On Fire Right Now", value="\n".join(lines), inline=False)

    for g in guilds:
        channel = bot.get_channel(g['bot_channel_id'])
        if channel is None:
            continue
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"Highlight broadcast failed for guild {g['guild_id']}: {e}")


async def _send_daily_reminder(guild_id: int, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        return
    embed = _themed_embed(
        "🎁 Daily Reward Reminder",
        description="Your daily reward is ready — use `/daily` to claim it!",
        color=discord.Color.green(),
    )
    try:
        async with db_pool.acquire() as conn:
            rows = await _get_today_leaderboard(conn, limit=5)
        if rows:
            embed.add_field(name="🏆 Today's Leaderboard So Far", value="\n".join(_leaderboard_lines(rows)), inline=False)
    except Exception as e:
        logger.warning(f"Daily leaderboard fetch failed for guild {guild_id}: {e}")
    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"Daily reminder send failed for guild {guild_id}: {e}")


async def _send_weekly_reminder(guild_id: int, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        return
    embed = _themed_embed(
        "📅 Weekly Reward Reminder",
        description="Your weekly reward is ready — use `/weekly` to claim it!",
        color=discord.Color.gold(),
    )
    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"Weekly reminder send failed for guild {guild_id}: {e}")


async def _send_hourly_nudge(guild_id: int, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        return
    embed = _themed_embed(
        "⏰ Hourly Reward",
        description="Don't forget your hourly reward — use `/hourly` to claim it!",
        color=discord.Color.blue(),
    )
    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"Hourly nudge send failed for guild {guild_id}: {e}")


async def _run_engagement_tick():
    global _last_hourly_highlight_utc_hour
    now_utc = datetime.now(timezone.utc)

    # Hourly highlights + heavy activity -- once per UTC hour, same broadcast
    # to every guild regardless of that guild's own timezone setting (this
    # one wasn't asked to be timezone-aware, unlike the 7am reminders below).
    current_hour_key = now_utc.strftime("%Y-%m-%d-%H")
    if current_hour_key != _last_hourly_highlight_utc_hour:
        _last_hourly_highlight_utc_hour = current_hour_key
        await _broadcast_hourly_highlights()

    async with db_pool.acquire() as conn:
        guilds = await conn.fetch("""
            SELECT guild_id, bot_channel_id, timezone_offset, last_daily_reminder_date,
                   last_weekly_reminder_date, last_hourly_reminder_at
            FROM guild_settings WHERE bot_channel_id IS NOT NULL
        """)

    for g in guilds:
        local_now = now_utc + timedelta(hours=g['timezone_offset'])
        local_date = local_now.date()

        # Daily reminder + today's leaderboard, 7:00-7:05 guild-local.
        if local_now.hour == 7 and local_now.minute < 5 and g['last_daily_reminder_date'] != local_date:
            await _send_daily_reminder(g['guild_id'], g['bot_channel_id'])
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE guild_settings SET last_daily_reminder_date=$1 WHERE guild_id=$2",
                    local_date, g['guild_id']
                )

        # Weekly reminder, Sunday 7:00-7:05 guild-local (weekday(): Mon=0..Sun=6).
        if local_now.weekday() == 6 and local_now.hour == 7 and local_now.minute < 5 and g['last_weekly_reminder_date'] != local_date:
            await _send_weekly_reminder(g['guild_id'], g['bot_channel_id'])
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE guild_settings SET last_weekly_reminder_date=$1 WHERE guild_id=$2",
                    local_date, g['guild_id']
                )

        # Hourly-reward nudge, roughly every 8 hours (~3x/day) -- a rolling
        # gap check rather than 3 fixed clock slots, so it self-corrects
        # after any bot downtime instead of silently missing a slot.
        last_hourly = g['last_hourly_reminder_at']
        if last_hourly is None or (now_utc - last_hourly) >= timedelta(hours=8):
            await _send_hourly_nudge(g['guild_id'], g['bot_channel_id'])
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE guild_settings SET last_hourly_reminder_at=$1 WHERE guild_id=$2",
                    now_utc, g['guild_id']
                )


async def _engagement_reminder_loop():
    """Ticks every 5 minutes and fires whichever scheduled engagement
    broadcasts are due. Same manual while-loop shape as keep_db_alive() --
    no @tasks.loop decorator is used anywhere else in this file, so this
    matches the existing convention rather than introducing a new one."""
    while True:
        try:
            await _run_engagement_tick()
        except Exception as e:
            logger.error(f"Engagement reminder loop error: {e}")
        await asyncio.sleep(_ENGAGEMENT_LOOP_INTERVAL)


@bot.tree.command(name="giveaway_reroll", description="Reroll a giveaway (Admin only)")
@app_commands.default_permissions(administrator=True)
async def reroll_giveaway(interaction: discord.Interaction, giveaway_id: int):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    async with db_pool.acquire() as conn:
        giveaway = await conn.fetchrow("SELECT * FROM giveaways WHERE id = $1", giveaway_id)
        if not giveaway:
            await interaction.followup.send("❌ Giveaway not found!", ephemeral=True)
            return

        entries = await conn.fetch("SELECT user_id FROM giveaway_entries WHERE giveaway_id = $1", giveaway_id)
        if not entries:
            await interaction.followup.send("❌ No entries to reroll!", ephemeral=True)
            return

        new_winners = secure_shuffle([e['user_id'] for e in entries])[:min(giveaway['winner_count'], len(entries))]
        winner_mentions = []
        for winner_id in new_winners:
            try:
                user = await bot.fetch_user(winner_id)
                winner_mentions.append(user.mention)
            except Exception:
                winner_mentions.append(f"<@{winner_id}>")

        embed = discord.Embed(title="🔄 Giveaway Rerolled!", color=discord.Color.gold())
        embed.add_field(name="Prize", value=giveaway['prize'], inline=False)
        embed.add_field(name="New Winners", value=", ".join(winner_mentions), inline=False)
        embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
        await interaction.followup.send(embed=embed)

# ============================================
# DASHBOARD COMMAND
# ============================================

@bot.tree.command(name="dashboard", description="Open the CS2CaseBot web dashboard — cases, games, inventory & more")
async def dashboard(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌐 CS2CaseBot Dashboard",
        description="Everything in one place — open cases, play games, manage your inventory, trade up skins, and more.",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="📦 Cases & Drops",
        value="Open 37+ cases with live spinning reels, float values, and confetti on rare drops.",
        inline=False
    )
    embed.add_field(
        name="🎮 22+ Games",
        value="Crash · Mines · Coinflip · Dice · Slots · Roulette · Plinko · Blackjack · Poker · and more.\n"
              "**All games are available exclusively on the dashboard.**",
        inline=False
    )
    embed.add_field(
        name="🎟️ Ticket Arcade",
        value="Spend tickets to play skill-based mini-games: Reaction Time, Aim Trainer, Bomb Defuse, Float Guesser, Memory Sequence.",
        inline=False
    )
    embed.add_field(
        name="👥 Friends & PvP",
        value="Add friends, view profiles, and send ticket challenges for PvP coinflips.",
        inline=False
    )
    embed.add_field(
        name="🔗 Open Now",
        value="[**cs2casebot.xyz →**](https://cs2casebot.xyz/)",
        inline=False
    )
    embed.add_field(
        name="💬 Join Our Community",
        value=f"[Click here to join our Discord!]({DISCORD_INVITE_URL})",
        inline=False
    )
    embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
    await interaction.response.send_message(embed=embed)

# ============================================
# HELP COMMAND
# ============================================

@bot.tree.command(name="help_bot", description="Show all bot commands")
async def help_command(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return

    await interaction.response.defer()

    embed = discord.Embed(title="🎮 CS2CaseBot Commands", color=discord.Color.blue())
    embed.add_field(name="💰 Economy", value="`/balance` `/daily` `/hourly` `/weekly` `/transfer`", inline=False)
    embed.add_field(name="📦 Cases (37)", value="`/cases` `/open <case>` `/bulkopen <case> 5/10/15/20/25`", inline=False)
    embed.add_field(name="⭐ Stickers (5)", value="`/capsules` `/sticker <capsule>`", inline=False)
    embed.add_field(name="🔄 Trade-Up", value="`/tradeup` (10 cheapest Blue→Purple)\n`/tradeup_purple` (10 cheapest Purple→Pink)\n`/tradeup_pink` (10 cheapest Pink→Red)\n`/quicktrade blue/purple/pink` (same, pick the tier)", inline=False)
    embed.add_field(name="📋 Quests", value="`/quests` `/claim`", inline=False)
    embed.add_field(name="🎁 Giveaways", value="`/giveaway_create` `/giveaway_reroll` (Admin)", inline=False)
    embed.add_field(name="📦 Inventory", value="`/inventory` `/sell <id>` `/profile`", inline=False)
    embed.add_field(name="🏷️ Stickers", value="`/apply_sticker <weapon_id> <sticker_id> <slot 0-3>` — apply sticker to weapon\n`/remove_sticker <weapon_id> <slot>` — scrape sticker off (sticker lost)", inline=False)
    embed.add_field(name="🎽 Loadout", value="`/loadout` — view equipped weapons\n`/loadout_toggle <item_id>` — add/remove from loadout", inline=False)
    embed.add_field(name="🏆 Leaderboards", value="`/leaderboard_money` `/leaderboard_opens` `/leaderboard_golds` `/leaderboard_trades`", inline=False)
    embed.add_field(name="🎲 Jackpot", value="`/jackpot <amount>`", inline=False)
    embed.add_field(name="🎮 Play Right Here in Discord", value="`/coinflip` `/slots` `/cs2slots` `/itemvshouse` `/case_auction`\nUse `/games` for the full rundown of these plus everything on the dashboard.", inline=False)
    embed.add_field(name="🌐 Dashboard — 50+ More Games", value="**50+ games** (Crash, Mines, Dice, Roulette, Plinko, Blackjack, Poker & more) + 🎟️ Ticket Arcade\n→ [**cs2casebot.xyz**](https://cs2casebot.xyz/) or `/dashboard` for a full feature overview", inline=False)
    embed.add_field(name="📖 More Help", value="`/games` — every game, in Discord or on the dashboard, with how-to-play basics", inline=False)
    embed.add_field(name="📊 Admin", value="`/stats` `/setchannel` `/removechannel`", inline=False)
    embed.add_field(name="💎 Bulk Discounts", value="5:5%, 10:10%, 15:15%, 20:20%, 25:25%", inline=False)
    embed.add_field(name="💬 Join Our Community", value=f"[Click here to join our Discord!]({DISCORD_INVITE_URL})", inline=False)
    embed.set_footer(text=f"💖 Support us on Ko-fi: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
    await interaction.followup.send(embed=embed)

# ============================================
# GAMES COMMAND (full rundown: Discord-native + dashboard catalog)
# ============================================

GAME_CATALOG_TIER_LABELS = {
    "arcade":            "🎫 Arcade (Ticket Games)",
    "easy":               "🟢 Easy",
    "medium":             "🟡 Medium",
    "hard":               "🔴 Hard",
    "heavy":              "⚔️ Heavy (Multiplayer)",
    "featured":           "⭐ Featured",
    "duels":              "🥊 Duels (1v1)",
    "item_wager":         "💎 Item Wager",
    "live_table":         "🎰 Live Table (Multiplayer)",
    "elimination_race":   "🏁 Elimination / Race",
    "novel":              "🎲 Novel",
}


@bot.tree.command(name="games", description="See every game -- how to play the 5 in Discord, plus everything on the dashboard")
async def cmd_games(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()

    total_dashboard_games = sum(len(v) for v in GAME_CATALOG.values())

    embed = discord.Embed(
        title="🎮 All CS2CaseBot Games",
        description=f"5 games play right here in Discord. **{total_dashboard_games}+ more** are exclusively on the dashboard.",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="🎮 Play Right Here in Discord",
        value=(
            "`/coinflip amount call:heads|tails` — call it right to double your bet\n"
            "`/slots amount` — classic 3-reel slots, match symbols to win\n"
            "`/cs2slots amount` — CS2-rarity reels; a full match also grants a real weapon, and 🎁 queues a free bonus spin\n"
            "`/itemvshouse item_id` — stake a real inventory item for a double-or-nothing against the house (see `/inventory` for IDs)\n"
            "`/case_auction case_id:<name>` — pick a case from the autocomplete list, then click **💰 Place Bid** on the message to bid blind; the high bidder opens it"
        ),
        inline=False
    )
    for tier_key, label in GAME_CATALOG_TIER_LABELS.items():
        games = GAME_CATALOG.get(tier_key, [])
        if not games:
            continue
        names = ", ".join(f"{g['emoji']} {g['name']}" for g in games)
        embed.add_field(name=f"{label} ({len(games)})", value=names, inline=False)
    embed.add_field(
        name="🌐 Play Them All",
        value="[**cs2casebot.xyz/games →**](https://cs2casebot.xyz/games)",
        inline=False
    )
    embed.set_footer(text=f"💖 Support us: {KO_FI_URL} | 🌐 Dashboard: {DASHBOARD_URL}")
    await interaction.followup.send(embed=embed)

# ============================================
# GAME REDIRECT HELPER
# ============================================

async def _game_redirect(interaction: discord.Interaction, game_name: str):
    embed = discord.Embed(
        title=f"🎮 {game_name} — Play on the Dashboard",
        description=f"**{game_name}** and 22+ other games are available on the CS2CaseBot web dashboard with full animations, live multipliers, and a much better experience than Discord commands.",
        color=discord.Color.blue()
    )
    embed.add_field(name="🔗 Play Now", value="[**cs2casebot.xyz →**](https://cs2casebot.xyz/)", inline=False)
    embed.set_footer(text="💖 Support us on Ko-fi!")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def _game_enabled_or_none(interaction: discord.Interaction, game_name: str) -> bool:
    """require_game_enabled() raises a plain fastapi.HTTPException (no live
    request/app needed to use it outside a route) whenever an admin has
    disabled a game via the admin panel -- same check the website's own
    endpoints run. Returns False (and replies to the user) if disabled."""
    try:
        await require_game_enabled(game_name)
        return True
    except HTTPException as e:
        await interaction.followup.send(f"❌ {e.detail}", ephemeral=True)
        return False


MIN_BET, MAX_BET = 50.0, 750_000.0


@bot.tree.command(name="mines", description="Play Mines on the CS2CaseBot dashboard")
async def cmd_mines(interaction: discord.Interaction):
    await _game_redirect(interaction, "Mines")


@bot.tree.command(name="mines_reveal", description="Play Mines on the CS2CaseBot dashboard")
async def cmd_mines_reveal(interaction: discord.Interaction):
    await _game_redirect(interaction, "Mines")


@bot.tree.command(name="mines_cashout", description="Play Mines on the CS2CaseBot dashboard")
async def cmd_mines_cashout(interaction: discord.Interaction):
    await _game_redirect(interaction, "Mines")

# ============================================
# COINFLIP COMMANDS - VS COMPUTER
# ============================================

@bot.tree.command(name="coinflip", description="Flip a coin against the house")
@app_commands.describe(amount=f"Bet amount (${MIN_BET:.0f}-${MAX_BET:,.0f})", call="Heads or tails")
@app_commands.choices(call=[
    app_commands.Choice(name="Heads", value="heads"),
    app_commands.Choice(name="Tails", value="tails"),
])
async def cmd_coinflip(interaction: discord.Interaction, amount: float, call: app_commands.Choice[str]):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()
    if not await _game_enabled_or_none(interaction, "coinflip"):
        return

    bet = clamp_bet(amount, MIN_BET, MAX_BET)
    user_call = call.value
    result = secure_choice(['heads', 'tails'])
    user_wins = (result == user_call)

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)
            if not await deduct_balance(interaction.user.id, bet, conn):
                await interaction.followup.send("❌ Insufficient balance!", ephemeral=True)
                return
            win = apply_house(bet * 2) if user_wins else 0.0
            if win:
                win = await credit_win(interaction.user.id, win, conn)
            await log_game(conn, interaction.user.id, 'coinflip', bet, win,
                            {'call': user_call, 'result': result}, win_inclusive=False)

    if user_wins:
        embed = _themed_embed(
            "🪙 Coinflip — You Won!",
            description=f"Landed on **{result.upper()}** — you called **{user_call.upper()}**!",
            color=discord.Color.gold()
        )
        embed.add_field(name="Bet", value=f"${bet:,.2f}", inline=True)
        embed.add_field(name="Payout", value=f"${win:,.2f}", inline=True)
    else:
        embed = _themed_embed(
            "🪙 Coinflip — You Lost",
            description=f"Landed on **{result.upper()}** — you called **{user_call.upper()}**.",
            color=discord.Color.red()
        )
        embed.add_field(name="Bet Lost", value=f"${bet:,.2f}", inline=True)
    await interaction.followup.send(embed=embed)

# ============================================
# DICE COMMANDS
# ============================================

@bot.tree.command(name="dice", description="Play Dice on the CS2CaseBot dashboard")
async def cmd_dice(interaction: discord.Interaction):
    await _game_redirect(interaction, "Dice")

# ============================================
# SLOTS COMMANDS
# ============================================

@bot.tree.command(name="slots", description="Spin the classic 3-reel fruit slots")
@app_commands.describe(amount=f"Bet amount (${MIN_BET:.0f}-${MAX_BET:,.0f})")
async def cmd_slots(interaction: discord.Interaction, amount: float):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()
    if not await _game_enabled_or_none(interaction, "slots"):
        return

    bet = clamp_bet(amount, MIN_BET, MAX_BET)
    symbols = [spin_classic_reel() for _ in range(3)]
    mult, combo = evaluate_classic(symbols)
    win = apply_house(bet * mult) if mult else 0.0

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)
            if not await deduct_balance(interaction.user.id, bet, conn):
                await interaction.followup.send("❌ Insufficient balance!", ephemeral=True)
                return
            if win:
                win = await credit_win(interaction.user.id, win, conn)
            await log_game(conn, interaction.user.id, 'slots_classic', bet, win,
                            {'symbols': symbols, 'combo': combo, 'mult': mult}, win_inclusive=False)

    reel = " | ".join(symbols)
    if win:
        embed = _themed_embed("🎰 Slots", description=f"# {reel}\n**{combo}** — {mult}x!", color=discord.Color.gold())
        embed.add_field(name="Bet", value=f"${bet:,.2f}", inline=True)
        embed.add_field(name="Payout", value=f"${win:,.2f}", inline=True)
    else:
        embed = _themed_embed("🎰 Slots", description=f"# {reel}\nNo match — better luck next spin!", color=discord.Color.red())
        embed.add_field(name="Bet Lost", value=f"${bet:,.2f}", inline=True)
    await interaction.followup.send(embed=embed)

# ============================================
# CS2 SLOTS COMMANDS (weapon-themed, real drops)
# ============================================

class CS2BonusView(discord.ui.View):
    """A single button letting the winner claim their free bonus mini-spin.
    Mirrors routes/games_easy.py's /slots/cs2/bonus/spin -- token is popped
    from _bonus_round_sessions on click (single-use) and expires after
    BONUS_ROUND_TTL_SECS, same as the website's own bonus round."""
    def __init__(self, user_id: int, token: str):
        super().__init__(timeout=BONUS_ROUND_TTL_SECS)
        self.user_id = user_id
        self.token = token

    @discord.ui.button(label="🎁 Claim Bonus Spin", style=discord.ButtonStyle.success)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This bonus spin isn't yours!", ephemeral=True)
            return
        sess = _bonus_round_sessions.pop(self.token, None)
        button.disabled = True
        if not sess or sess["user_id"] != self.user_id:
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("❌ This bonus round was already claimed or has expired.", ephemeral=True)
            return
        if _time.time() - sess["created_at"] > BONUS_ROUND_TTL_SECS:
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("❌ This bonus round has expired.", ephemeral=True)
            return

        spins = [spin_cs2_reel() for _ in range(3)]
        emojis = [s[1] for s in spins]
        matched_rarity = None
        if len(set(emojis)) == 1 and emojis[0] != '🎁':
            matched_rarity = CS2_EMOJI_TO_RARITY.get(emojis[0])
        elif any(emojis.count(e) == 2 for e in set(emojis) if e != '🎁'):
            matched_rarity = CS2_EMOJI_TO_RARITY.get(
                next(e for e in emojis if emojis.count(e) == 2 and e != '🎁')
            )

        item_won = None
        if matched_rarity:
            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    granted = _grant_cs2_rarity_weapon(matched_rarity)
                    if granted:
                        item_won = await _insert_granted_weapon(conn, self.user_id, granted)
                    await log_game(conn, self.user_id, 'slots_cs2_bonus', 0,
                                    item_won['price'] if item_won else 0,
                                    {'emojis': emojis, 'item_won': item_won['name'] if item_won else None},
                                    win_inclusive=False)

        reel = " | ".join(emojis)
        if item_won:
            embed = _themed_embed(
                "🎁 Bonus Spin!",
                description=f"# {reel}\nYou won **{item_won['name']}** ({item_won['rarity']})!",
                rarity=item_won['rarity'],
                item_image_url=item_won.get('image_url'),
            )
        else:
            embed = _themed_embed("🎁 Bonus Spin!", description=f"# {reel}\nNo match this time.", color=discord.Color.greyple())
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=embed)

    async def on_timeout(self):
        _bonus_round_sessions.pop(self.token, None)


@bot.tree.command(name="cs2slots", description="Spin CS2-themed slots for real weapon drops")
@app_commands.describe(amount=f"Bet amount (${MIN_BET:.0f}-${MAX_BET:,.0f})")
async def cmd_cs2_slots(interaction: discord.Interaction, amount: float):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()
    if not await _game_enabled_or_none(interaction, "slots-cs2"):
        return

    bet = clamp_bet(amount, MIN_BET, MAX_BET)
    spins = [spin_cs2_reel() for _ in range(3)]
    names = [s[0] for s in spins]
    emojis = [s[1] for s in spins]
    key = ''.join(emojis)
    mult, label = CS2_SPECIAL.get(key, (0, 'miss'))
    is_full_triple = key in CS2_SPECIAL
    if mult == 0:
        if emojis.count('⭐') == 2: mult, label = 20, 'DOUBLE GOLD'
        elif emojis.count('🔴') == 2: mult, label = 8, 'DOUBLE RED'
        elif emojis.count('💗') == 2: mult, label = 4, 'DOUBLE PINK'
        elif emojis.count('🟪') == 2: mult, label = 2, 'DOUBLE PURPLE'
    win = apply_house(bet * mult) if mult else 0.0

    item_won = None
    bonus_token = None
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)
            if not await deduct_balance(interaction.user.id, bet, conn):
                await interaction.followup.send("❌ Insufficient balance!", ephemeral=True)
                return
            if win:
                win = await credit_win(interaction.user.id, win, conn)
            if is_full_triple:
                rarity = CS2_EMOJI_TO_RARITY.get(emojis[0])
                if rarity:
                    granted = _grant_cs2_rarity_weapon(rarity)
                    if granted:
                        item_won = await _insert_granted_weapon(conn, interaction.user.id, granted)
            if '🎁' in emojis:
                bonus_token = secrets.token_urlsafe(16)
                _bonus_round_sessions[bonus_token] = {"user_id": interaction.user.id, "created_at": _time.time()}

            logged_win = win + (item_won['price'] if item_won else 0)
            await log_game(conn, interaction.user.id, 'slots_cs2', bet, logged_win,
                            {'rarities': names, 'emojis': emojis, 'label': label,
                             'item_won': item_won['name'] if item_won else None},
                            win_inclusive=False)

    reel = " | ".join(emojis)
    rarity_color = item_won['rarity'] if item_won else None
    color = None if rarity_color else (discord.Color.gold() if win else discord.Color.red())
    embed = _themed_embed(
        "🔫 CS2 Slots",
        description=f"# {reel}\n**{label.upper()}**" if mult else f"# {reel}\nNo match.",
        color=color, rarity=rarity_color,
        item_image_url=item_won.get('image_url') if item_won else None,
    )
    embed.add_field(name="Bet", value=f"${bet:,.2f}", inline=True)
    if win:
        embed.add_field(name="Payout", value=f"${win:,.2f}", inline=True)
    if item_won:
        embed.add_field(name="🔫 Weapon Won", value=f"{item_won['name']} ({item_won['rarity']})", inline=False)

    if bonus_token:
        await interaction.followup.send(embed=embed, view=CS2BonusView(interaction.user.id, bonus_token))
    else:
        await interaction.followup.send(embed=embed)

# ============================================
# ITEM VS HOUSE JACKPOT
# ============================================

ITEM_HOUSE_WIN_PROBABILITY = 0.5 * (1 - HOUSE_EDGE)
ITEM_HOUSE_MIN_STAKE_VALUE = 0.50


async def _itemvshouse_autocomplete(interaction: discord.Interaction, current: str):
    """Only suggests items that are actually stakeable -- kept, unprotected,
    not equipped in a loadout, and above the minimum stake value -- so the
    dropdown never offers an item the command would just reject anyway."""
    current_lower = (current or "").lower()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, item_name, price FROM inventory
            WHERE user_id=$1 AND status='kept' AND protected=FALSE AND in_loadout=FALSE
              AND price >= $2
            ORDER BY created_at DESC LIMIT 200
        """, interaction.user.id, ITEM_HOUSE_MIN_STAKE_VALUE)
    choices = []
    for row in rows:
        if current_lower in row['item_name'].lower() or current_lower in str(row['id']):
            choices.append(app_commands.Choice(name=f"#{row['id']} {row['item_name']} (${float(row['price']):.2f})"[:100], value=row['id']))
        if len(choices) >= 25:
            break
    return choices


@bot.tree.command(name="itemvshouse", description="Stake an inventory item for a 50/50-ish double-or-nothing against the house")
@app_commands.describe(item_id="Item to stake — search by name")
@app_commands.autocomplete(item_id=_itemvshouse_autocomplete)
async def cmd_item_vs_house(interaction: discord.Interaction, item_id: int):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()

    won = False
    item = None
    payout = 0.0
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)
            item = await conn.fetchrow("""
                UPDATE inventory SET status='staked'
                WHERE id=$1 AND user_id=$2 AND status='kept'
                  AND price >= $3 AND in_loadout = FALSE AND protected = FALSE
                RETURNING item_name, rarity, price, condition, is_stattrak, float_value, image_url
            """, item_id, interaction.user.id, ITEM_HOUSE_MIN_STAKE_VALUE)
            if not item:
                await interaction.followup.send(
                    f"❌ Item not available to wager (must be a kept, unequipped, unprotected item worth at least ${ITEM_HOUSE_MIN_STAKE_VALUE:.2f}).",
                    ephemeral=True
                )
                return

            value = float(item['price'])
            won = secure_random() < ITEM_HOUSE_WIN_PROBABILITY
            if won:
                await conn.execute("UPDATE inventory SET status='kept' WHERE id=$1", item_id)
                payout = await credit_win(interaction.user.id, value, conn)
            else:
                await conn.execute("UPDATE inventory SET status='sold' WHERE id=$1", item_id)

            await conn.execute("""
                INSERT INTO item_house_wagers
                    (user_id, inventory_id, item_name, rarity, condition, is_stattrak,
                     float_value, image_url, value, won, payout_value)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """, interaction.user.id, item_id, item['item_name'], item['rarity'], item['condition'],
                item['is_stattrak'], item['float_value'], item['image_url'], value, won, payout)

    st_prefix = "StatTrak™ " if item['is_stattrak'] else ""
    if won:
        embed = _themed_embed(
            "🏛️ Item vs House — You Won!",
            description=f"**{st_prefix}{item['item_name']}** ({item['rarity']}) is safe, plus a **${payout:,.2f}** cash bonus!",
            rarity=item['rarity'], item_image_url=item['image_url'],
        )
    else:
        embed = _themed_embed(
            "🏛️ Item vs House — You Lost",
            description=f"The house keeps **{st_prefix}{item['item_name']}** ({item['rarity']}, ${value:,.2f}).",
            color=discord.Color.red(), item_image_url=item['image_url'],
        )
    await interaction.followup.send(embed=embed)

# ============================================
# LIVE CASE AUCTION (Discord-only pool -- see plan doc for why this is
# separate from routes/live_case_auction.py's own website rooms)
# ============================================

DISCORD_AUCTION_MIN_BID = 10.0
DISCORD_AUCTION_MAX_BID = 750_000.0
DISCORD_AUCTION_BIDDING_SECS = 20
DISCORD_AUCTION_MIN_INCREMENT_FLAT = 10.0
DISCORD_AUCTION_MIN_INCREMENT_PCT = 0.05


def _discord_min_next_bid(current_bid: float) -> float:
    if current_bid <= 0:
        return DISCORD_AUCTION_MIN_BID
    return round(current_bid + max(DISCORD_AUCTION_MIN_INCREMENT_FLAT, current_bid * DISCORD_AUCTION_MIN_INCREMENT_PCT), 2)


def _case_auction_embed(auction_id: int, case_id: str, current_bid: float, high_bidder_id: int = None) -> discord.Embed:
    case = CASES.get(case_id, {})
    embed = _themed_embed(
        f"🎨 Live Case Auction — {case.get('name', case_id)}",
        description="Bid on the right to open this **mystery case** — it's blind, you get whatever drops!\nClick **💰 Place Bid** below to bid.",
        footer_extra=f"Min next bid: ${_discord_min_next_bid(current_bid):.2f}",
    )
    bid_text = f"${current_bid:,.2f} by <@{high_bidder_id}>" if high_bidder_id else "No bids yet"
    embed.add_field(name="Current Bid", value=bid_text, inline=True)
    embed.add_field(name="Bidding Window", value=f"{DISCORD_AUCTION_BIDDING_SECS}s (resets on every new bid)", inline=True)
    return embed


class CaseAuctionBidModal(discord.ui.Modal, title="Place Your Bid"):
    """Opened by CaseAuctionView's button -- the auction_id is baked into the
    view/modal instance, so the bidder never has to know or type an ID."""
    amount = discord.ui.TextInput(label="Bid amount ($)", placeholder="e.g. 25.00", max_length=12)

    def __init__(self, auction_id: int):
        super().__init__()
        self.auction_id = auction_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount_val = float(str(self.amount.value).replace('$', '').replace(',', '').strip())
        except ValueError:
            await interaction.response.send_message("❌ Enter a valid number, e.g. 25.00.", ephemeral=True)
            return
        if amount_val <= 0:
            await interaction.response.send_message("❌ Bid must be a positive amount.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await _place_case_auction_bid(interaction, self.auction_id, amount_val)


class CaseAuctionView(discord.ui.View):
    """One button on the auction's own message -- clicking it opens a modal
    for the bid amount, so bidding needs zero typed IDs. No timeout since a
    single auction's bidding window can run indefinitely (it resets on every
    new bid); the button is cleared explicitly once the auction resolves."""
    def __init__(self, auction_id: int):
        super().__init__(timeout=None)
        self.auction_id = auction_id

    @discord.ui.button(label="💰 Place Bid", style=discord.ButtonStyle.success)
    async def bid(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CaseAuctionBidModal(self.auction_id))


@bot.tree.command(name="case_auction", description="Start a live blind case auction")
@app_commands.describe(case_id="Case to auction off — start typing a name to search")
@app_commands.autocomplete(case_id=_case_id_autocomplete)
async def cmd_case_auction(interaction: discord.Interaction, case_id: str):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer()
    if case_id not in CASES:
        await interaction.followup.send("❌ Unknown case. Start typing a case name and pick one from the list that pops up.", ephemeral=True)
        return

    deadline = datetime.now(timezone.utc) + timedelta(seconds=DISCORD_AUCTION_BIDDING_SECS)
    async with db_pool.acquire() as conn:
        await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)
        auction_id = await conn.fetchval("""
            INSERT INTO discord_case_auctions (case_id, channel_id, status, current_bid, bid_deadline)
            VALUES ($1, $2, 'bidding', 0, $3) RETURNING id
        """, case_id, interaction.channel_id, deadline)

    message = await interaction.followup.send(embed=_case_auction_embed(auction_id, case_id, 0), view=CaseAuctionView(auction_id))
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE discord_case_auctions SET message_id=$1 WHERE id=$2", message.id, auction_id)

    asyncio.create_task(_run_discord_auction(auction_id))


async def _place_case_auction_bid(interaction: discord.Interaction, auction_id: int, amount: float):
    """Shared by the 💰 Place Bid modal (primary path) and /case_auction_bid
    (kept as a fallback for anyone who prefers typing the command directly).
    Caller must already have deferred the interaction (ephemeral)."""
    amount = round(min(max(amount, DISCORD_AUCTION_MIN_BID), DISCORD_AUCTION_MAX_BID), 2)

    channel_id = message_id = case_id = new_bid = None
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)
            # FOR UPDATE serializes concurrent bids from different Discord
            # users on the same row -- the DB-native equivalent of the
            # website's per-room asyncio.Lock, since Discord bids arrive as
            # independent command invocations, not calls into one shared
            # in-process room object.
            auction = await conn.fetchrow(
                "SELECT * FROM discord_case_auctions WHERE id=$1 FOR UPDATE", auction_id
            )
            if not auction or auction['status'] != 'bidding':
                await interaction.followup.send("❌ Auction not found or already closed.", ephemeral=True)
                return
            if auction['bid_deadline'] <= datetime.now(timezone.utc):
                await interaction.followup.send("❌ This auction is closing — try the next one!", ephemeral=True)
                return
            min_next = _discord_min_next_bid(float(auction['current_bid']))
            if amount < min_next:
                await interaction.followup.send(f"❌ Minimum next bid is ${min_next:.2f}.", ephemeral=True)
                return
            if auction['high_bidder_id'] == interaction.user.id:
                await interaction.followup.send("❌ You're already the high bidder!", ephemeral=True)
                return
            if not await deduct_balance(interaction.user.id, amount, conn):
                await interaction.followup.send("❌ Insufficient balance!", ephemeral=True)
                return

            # Refund the previous high bidder in the same transaction as the
            # new bidder's deduction -- atomic hand-off, mirrors
            # routes/live_case_auction.py's place_bid().
            if auction['high_bidder_id'] is not None:
                await add_balance(auction['high_bidder_id'], float(auction['current_bid']), conn)
                await conn.execute(
                    "UPDATE discord_case_auction_bids SET refunded=TRUE WHERE auction_id=$1 AND user_id=$2 AND refunded=FALSE",
                    auction_id, auction['high_bidder_id']
                )

            new_deadline = datetime.now(timezone.utc) + timedelta(seconds=DISCORD_AUCTION_BIDDING_SECS)
            await conn.execute(
                "INSERT INTO discord_case_auction_bids (auction_id, user_id, amount) VALUES ($1,$2,$3)",
                auction_id, interaction.user.id, amount
            )
            await conn.execute(
                "UPDATE discord_case_auctions SET current_bid=$1, high_bidder_id=$2, bid_deadline=$3 WHERE id=$4",
                amount, interaction.user.id, new_deadline, auction_id
            )
            channel_id, message_id, case_id = auction['channel_id'], auction['message_id'], auction['case_id']
            new_bid = amount

    try:
        channel = bot.get_channel(channel_id)
        if channel and message_id:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=_case_auction_embed(auction_id, case_id, new_bid, interaction.user.id),
                                view=CaseAuctionView(auction_id))
    except Exception as e:
        logger.warning(f"Failed to update case auction embed: {e}")

    await interaction.followup.send(f"✅ Bid placed: ${new_bid:,.2f}!", ephemeral=True)


@bot.tree.command(name="case_auction_bid", description="Bid in a live case auction (or just click Place Bid on the auction message)")
@app_commands.describe(auction_id="Auction ID (shown in the auction's message)", amount="Bid amount")
async def cmd_case_auction_bid(interaction: discord.Interaction, auction_id: int, amount: float):
    if not await is_bot_channel(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await _place_case_auction_bid(interaction, auction_id, amount)


async def _run_discord_auction(auction_id: int):
    """Sleeps until the auction's bid_deadline, re-checking (a concurrent bid
    may have pushed it back) same as AuctionRoom.run_bidding()'s reschedule
    loop, then atomically resolves. Safe to call at startup for recovery."""
    while True:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT bid_deadline, status FROM discord_case_auctions WHERE id=$1", auction_id
            )
        if not row or row['status'] != 'bidding':
            return
        remaining = (row['bid_deadline'] - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            break
        await asyncio.sleep(min(2.0, max(0.1, remaining)))

    item = None
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Atomic claim: only succeeds if still 'bidding' AND genuinely past
            # its deadline. A bid placed concurrently already pushed
            # bid_deadline forward, so this simply won't match and no-ops --
            # the caller's own loop above already re-checked before arriving
            # here, so in practice this only guards the startup-recovery
            # sweep racing a still-running live task for the same auction.
            auction = await conn.fetchrow("""
                UPDATE discord_case_auctions SET status='settled'
                WHERE id=$1 AND status='bidding' AND bid_deadline <= NOW()
                RETURNING *
            """, auction_id)
            if not auction:
                return

            if auction['high_bidder_id'] is None:
                await conn.execute(
                    "UPDATE discord_case_auctions SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                    auction_id
                )
            else:
                winner_id = auction['high_bidder_id']
                item = get_random_item(auction['case_id'])
                if item:
                    skin_img_file = item.get('image_filename')
                    skin_img_url = f"/static/images/skins/{skin_img_file}" if skin_img_file else None
                    await conn.execute("""
                        INSERT INTO inventory
                            (user_id, item_name, item_type, rarity, price, condition, is_stattrak, status, float_value, image_url)
                        VALUES ($1,$2,'weapon',$3,$4,$5,$6,'kept',$7,$8)
                    """, winner_id, item['name'], item['rarity'], item['price'],
                        item.get('condition', 'Field-Tested'), item.get('is_stattrak', False),
                        item.get('float', 0.0), skin_img_url)
                    await conn.execute(
                        "UPDATE users SET total_opens = total_opens + 1, total_golds = total_golds + $2 WHERE user_id=$1",
                        winner_id, 1 if item['rarity'] == 'Gold' else 0
                    )
                    await log_game(conn, winner_id, 'live_case_auction', float(auction['current_bid']), item['price'], {
                        'auction_id': auction_id, 'case_id': auction['case_id'], 'won_item': item['name'],
                    })
                await conn.execute("""
                    UPDATE discord_case_auctions
                    SET won_item_name=$1, won_item_value=$2, resolved_at=NOW()
                    WHERE id=$3
                """, item['name'] if item else None, item['price'] if item else None, auction_id)

    try:
        channel = bot.get_channel(auction['channel_id'])
        if channel is None:
            return
        if auction['high_bidder_id'] is None:
            embed = _themed_embed("🎨 Case Auction Cancelled", description="No bids were placed.", color=discord.Color.greyple())
        else:
            case = CASES.get(auction['case_id'], {})
            item_img = f"/static/images/skins/{item['image_filename']}" if item and item.get('image_filename') else None
            embed = _themed_embed(
                "🏆 Case Auction Closed!",
                description=f"<@{auction['high_bidder_id']}> won the **{case.get('name', auction['case_id'])}** for ${float(auction['current_bid']):,.2f}!",
                rarity=item['rarity'] if item else None, item_image_url=item_img,
            )
            if item:
                embed.add_field(name="Item Received", value=f"{item['name']} ({item['rarity']}, ${item['price']:,.2f})", inline=False)
        if auction['message_id']:
            try:
                message = await channel.fetch_message(auction['message_id'])
                await message.edit(embed=embed, view=None)
                return
            except Exception:
                pass
        await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to announce case auction result: {e}")

# ============================================
# SKIN UPGRADE COMMANDS
# ============================================

@bot.tree.command(name="upgrade", description="Attempt to upgrade a skin to the next rarity")
@app_commands.describe(item_id="Weapon to upgrade — search by name")
@app_commands.autocomplete(item_id=_upgrade_item_autocomplete)
async def cmd_upgrade(interaction: discord.Interaction, item_id: int):
    if not await is_bot_channel(interaction):
        return
    
    await interaction.response.defer()
    
    result = await skin_upgrade(interaction.user.id, item_id)
    
    if not result['success']:
        await interaction.followup.send(f"❌ {result['error']}", ephemeral=True)
        return
    
    if result.get('upgraded'):
        embed = _themed_embed(
            "⭐ UPGRADE SUCCESSFUL!",
            description=f"{result['old_item_name']} → {result['new_item_name']}",
            rarity=result.get('new_rarity'),
        )
        embed.add_field(name="New Rarity", value=result['new_rarity'], inline=True)
        embed.add_field(name="New Value", value=f"${result['new_price']:,.2f}", inline=True)
    else:
        embed = _themed_embed(
            "💔 Upgrade Failed!",
            description=f"{result['old_item_name']} was lost in the upgrade attempt",
            color=discord.Color.red()
        )
        embed.add_field(name="Cost", value=f"${result['cost']:,.2f}", inline=True)

    await interaction.followup.send(embed=embed)

# ============================================
# HOURLY & WEEKLY COMMANDS
# ============================================

@bot.tree.command(name="hourly", description="Claim your hourly reward")
async def cmd_hourly(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return
    
    await interaction.response.defer()
    
    result = await claim_hourly(interaction.user.id)
    
    if not result['success']:
        await interaction.followup.send(f"⏰ {result['error']}", ephemeral=True)
        return
    
    embed = _themed_embed(
        "🕐 Hourly Claimed!",
        description=f"You received ${result['reward']:,.2f}!",
        color=discord.Color.green(),
        footer_extra="Come back in 1 hour for more!",
    )
    embed.add_field(name="Total Claims", value=str(result['total_claimed']), inline=True)
    await _append_today_leaderboard_field(embed)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="weekly", description="Claim your weekly reward")
async def cmd_weekly(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return
    
    await interaction.response.defer()
    
    result = await claim_weekly(interaction.user.id)
    
    if not result['success']:
        await interaction.followup.send(f"📅 {result['error']}", ephemeral=True)
        return
    
    embed = _themed_embed(
        "📅 Weekly Claimed!",
        description=f"You received ${result['reward']:,.2f}!",
        color=discord.Color.gold(),
        footer_extra="Come back in 7 days for more!",
    )
    embed.add_field(name="Total Claims", value=str(result['total_claimed']), inline=True)
    await interaction.followup.send(embed=embed)

# ============================================
# XP COMMANDS
# ============================================

@bot.tree.command(name="profile", description="View your profile and XP")
async def cmd_profile(interaction: discord.Interaction):
    if not await is_bot_channel(interaction):
        return
    
    await interaction.response.defer()
    
    async with db_pool.acquire() as conn:
        await ensure_user_exists(interaction.user.id, interaction.user.display_name, conn)
        user = await conn.fetchrow(
            "SELECT xp, level, prestige, balance FROM users WHERE user_id = $1",
            interaction.user.id
        )
        if not user:
            await interaction.followup.send("❌ User not found!", ephemeral=True)
            return
    
    xp = user['xp'] or 0
    level = user['level'] or 1
    prestige = user['prestige'] or 0
    balance = user['balance'] or 0
    
    xp_needed = level * 50 + 100
    
    embed = _themed_embed(f"👤 {interaction.user.display_name}'s Profile", color=discord.Color.blue())
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.add_field(name="Level", value=f"🎮 {level}", inline=True)
    embed.add_field(name="Prestige", value=f"🌟 {prestige}", inline=True)
    embed.add_field(name="XP", value=f"{xp:,} / {xp_needed:,}", inline=True)
    embed.add_field(name="💰 Balance", value=f"${balance:,.2f}", inline=True)

    progress = min(100, int((xp / xp_needed) * 100))
    bar_length = 20
    filled = int(progress / (100 / bar_length))
    bar = "█" * filled + "░" * (bar_length - filled)
    embed.add_field(name="Progress", value=f"`{bar}` {progress}%", inline=False)

    await interaction.followup.send(embed=embed)

# ============================================
# HOURLY & WEEKLY CLAIMS
# ============================================

async def claim_hourly(user_id: int) -> dict:
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            # FOR UPDATE locks the row so concurrent claims queue up
            user = await conn.fetchrow(
                "SELECT last_hourly, total_hourly_claimed FROM users WHERE user_id = $1 FOR UPDATE",
                user_id
            )
            if not user:
                return {'success': False, 'error': 'User not found'}

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            last_hourly = user['last_hourly']
            if last_hourly and last_hourly.tzinfo is not None:
                last_hourly = last_hourly.replace(tzinfo=None)

            if last_hourly and (now - last_hourly).total_seconds() < 3600:
                remaining = 3600 - (now - last_hourly).total_seconds()
                minutes = int(remaining // 60)
                return {'success': False, 'error': f'Already claimed! Next claim in {minutes} minutes'}

            reward = 75
            total_claimed = (user['total_hourly_claimed'] or 0) + 1

            if total_claimed % 10 == 0:
                reward += 250

            await conn.execute(
                """UPDATE users
                   SET balance = balance + $1, last_hourly = $2, total_hourly_claimed = $3
                   WHERE user_id = $4""",
                reward, now, total_claimed, user_id
            )

            return {'success': True, 'reward': reward, 'total_claimed': total_claimed}

async def claim_weekly(user_id: int) -> dict:
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            # FOR UPDATE locks the row so concurrent claims queue up
            user = await conn.fetchrow(
                "SELECT last_weekly, total_weekly_claimed FROM users WHERE user_id = $1 FOR UPDATE",
                user_id
            )
            if not user:
                return {'success': False, 'error': 'User not found'}

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            last_weekly = user['last_weekly']
            if last_weekly and last_weekly.tzinfo is not None:
                last_weekly = last_weekly.replace(tzinfo=None)

            if last_weekly and (now - last_weekly).total_seconds() < 604800:
                remaining = 604800 - (now - last_weekly).total_seconds()
                days = int(remaining // 86400)
                hours = int((remaining % 86400) // 3600)
                return {'success': False, 'error': f'Already claimed! Next claim in {days}d {hours}h'}

            reward = 5000
            total_claimed = (user['total_weekly_claimed'] or 0) + 1

            await conn.execute(
                """UPDATE users
                   SET balance = balance + $1, last_weekly = $2, total_weekly_claimed = $3
                   WHERE user_id = $4""",
                reward, now, total_claimed, user_id
            )

            return {'success': True, 'reward': reward, 'total_claimed': total_claimed}

# ============================================
# SKIN UPGRADE
# ============================================

async def skin_upgrade(user_id: int, item_id: int) -> dict:
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)
            
            item = await conn.fetchrow(
                "SELECT * FROM inventory WHERE id = $1 AND user_id = $2 AND status = 'kept' FOR UPDATE",
                item_id, user_id
            )
            if not item:
                return {'success': False, 'error': 'Item not found in inventory'}
            if item['protected']:
                return {'success': False, 'error': 'This item is protected — unprotect it first to upgrade'}
            if item['item_type'] != 'weapon':
                return {'success': False, 'error': 'Only weapon items can be upgraded'}

            rarity_order = ['Blue', 'Purple', 'Pink', 'Red', 'Gold']
            if item['rarity'] == 'Gold':
                return {'success': False, 'error': "Gold items can't be upgraded further"}

            current_rarity = item['rarity']
            current_index = rarity_order.index(current_rarity)
            next_rarity = rarity_order[current_index + 1] if current_index < len(rarity_order) - 1 else None

            if not next_rarity:
                return {'success': False, 'error': 'Item cannot be upgraded further'}

            chances = {'Blue': 0.8, 'Purple': 0.6, 'Pink': 0.4, 'Red': 0.25}
            success_chance = chances.get(current_rarity, 0.5)
            success = secure_random() < success_chance

            upgrade_cost = {'Blue': 10, 'Purple': 50, 'Pink': 200, 'Red': 1000}.get(current_rarity, 10)

            if not await deduct_balance(user_id, upgrade_cost, conn):
                return {'success': False, 'error': f'Insufficient balance. Upgrade costs ${upgrade_cost}'}

            await conn.execute("DELETE FROM inventory WHERE id = $1", item_id)
            
            if success:
                possible_items = list(ALL_ITEMS_BY_RARITY.get(next_rarity, []))
                
                new_item_template = secure_choice(possible_items) if possible_items else {
                    'name': f'Mystery {next_rarity} Item',
                    'rarity': next_rarity,
                    'condition': 'Field-Tested',
                    'tier': None
                }
                
                is_stattrak = secure_random() < 0.1
                float_value = generate_skin_float()
                condition_from_float = get_skin_condition(float_value)
                value = calculate_item_value(next_rarity, condition_from_float, new_item_template.get('tier'), is_stattrak)
                name = f"{'StatTrak™ ' if is_stattrak else ''}{new_item_template['name']}"
                
                await conn.execute(
                    """INSERT INTO inventory 
                       (user_id, item_name, item_type, rarity, price, condition, is_stattrak, status, float_value) 
                       VALUES ($1, $2, 'weapon', $3, $4, $5, $6, 'kept', $7)""",
                    user_id, name, next_rarity, value, condition_from_float, is_stattrak, float_value
                )
                
                await conn.execute(
                    """INSERT INTO skin_upgrades 
                       (user_id, item_id, input_rarity, output_rarity, success) 
                       VALUES ($1, $2, $3, $4, true)""",
                    user_id, item_id, current_rarity, next_rarity
                )
                
                return {
                    'success': True,
                    'upgraded': True,
                    'new_rarity': next_rarity,
                    'new_item_name': name,
                    'new_price': value,
                    'old_rarity': current_rarity,
                    'old_item_name': item['item_name']
                }
            else:
                await conn.execute(
                    """INSERT INTO skin_upgrades 
                       (user_id, item_id, input_rarity, output_rarity, success) 
                       VALUES ($1, $2, $3, $4, false)""",
                    user_id, item_id, current_rarity, next_rarity
                )
                
                return {
                    'success': True,
                    'upgraded': False,
                    'old_item_name': item['item_name'],
                    'old_rarity': current_rarity,
                    'cost': upgrade_cost
                }


# ============================================
# RUN BOT
# ============================================

if __name__ == "__main__":
    if not TOKEN:
        logger.error("❌ DISCORD_BOT_TOKEN not found!")
        exit(1)
    bot.run(TOKEN)
