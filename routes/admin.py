# ============================================================
# routes/admin.py
# CS2CaseBot | Admin Panel API
#
# All routes prefixed /api/admin
# Every route requires the caller to be in ADMIN_USER_IDS.
#
# Endpoints (matching admin.html api() calls):
#   GET  /stats              — dashboard KPIs
#   GET  /users              — paginated user list with search
#   GET  /users/{id}         — user detail + inventory summary
#   POST /users/{id}/balance — adjust balance (+ or -)
#   POST /users/{id}/ban     — ban user
#   POST /users/{id}/unban   — unban user
#   GET  /cases              — all cases with price/featured
#   POST /cases/{id}/price   — update case price
#   POST /cases/{id}/featured— toggle featured flag
#   GET  /analytics/economy  — economy health stats
#   GET  /games/settings     — per-game settings
#   POST /games/settings     — update a game setting
#   GET  /giveaways          — list giveaways
#   POST /giveaway/create    — create giveaway
#   POST /giveaways/{id}/draw— draw winners
#   POST /inventory/deposit  — secret item deposit (+ Discord DM)
#   GET  /announcements      — list announcements
#   POST /announcements      — create announcement
#   GET  /settings           — site-wide settings
#   POST /settings           — save settings
#   GET  /audit-log          — paginated audit log
#   POST /beta/users         — add beta tester
#   GET  /live-feed          — recent case opens
#   POST /premium/toggle     — toggle premium mode
#   GET  /backup/create      — trigger DB backup
#   POST /fire-sale          — start a fire sale discount
# ============================================================

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

import asyncpg
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel

import shared
from shared import (
    logger, get_db, require_auth, ADMIN_USER_IDS,
    deduct_balance, add_balance, convert_decimals, CASES, FEATURED_CASES,
    STICKER_CAPSULES, fix_surrogate_emoji,
    _invalidate_vip_cache, invalidate_game_enabled_cache,
    invalidate_case_override_cache, invalidate_capsule_override_cache,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# ── Admin guard ───────────────────────────────────────────────
async def require_admin(request: Request) -> int:
    """Dependency: must be logged in AND in ADMIN_USER_IDS."""
    user_id = await require_auth(request)
    if user_id not in ADMIN_USER_IDS:
        raise HTTPException(403, "Admin access required")
    return user_id

# ── Audit logger ─────────────────────────────────────────────
async def audit(conn, admin_id: int, action_type: str,
                target_id: int = None, target_username: str = None,
                details: dict = None):
    try:
        admin_row = await conn.fetchrow(
            "SELECT username FROM users WHERE user_id=$1", admin_id
        )
        admin_username = admin_row["username"] if admin_row else str(admin_id)
        await conn.execute("""
            INSERT INTO admin_audit_log
                (admin_id, admin_username, action_type,
                 target_id, target_username, details)
            VALUES ($1,$2,$3,$4,$5,$6)
        """, admin_id, admin_username, action_type,
            target_id, target_username,
            json.dumps(details or {}))
    except Exception as e:
        logger.warning(f"Audit log failed: {e}")

# ── Settings helpers (Fix 19: TTL cache + invalidation) ──────
_settings_cache:    dict  = {}
_settings_cache_ttl = 30   # seconds
_settings_cache_at  = 0.0
_settings_cache_lock = asyncio.Lock()

async def get_settings(pool) -> dict:
    global _settings_cache, _settings_cache_at
    now = time.monotonic()
    if now - _settings_cache_at < _settings_cache_ttl and _settings_cache:
        return _settings_cache
    async with _settings_cache_lock:
        # Double-check after acquiring lock
        now = time.monotonic()
        if now - _settings_cache_at < _settings_cache_ttl and _settings_cache:
            return _settings_cache
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM admin_settings")
            _settings_cache    = {r["key"]: r["value"] for r in rows}
            _settings_cache_at = now
            return _settings_cache

def invalidate_settings_cache():
    global _settings_cache_at
    _settings_cache_at = 0.0   # force next read to hit DB

async def set_setting(conn, key: str, value: str):
    await conn.execute("""
        INSERT INTO admin_settings (key, value)
        VALUES ($1,$2)
        ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()
    """, key, value)

# ============================================================
# DASHBOARD — /stats
# ============================================================

@router.get("/stats")
async def admin_stats(admin_id: int = Depends(require_admin)):
    pool = await get_db()
    async with pool.acquire() as conn:
        # Users
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
        new_24h     = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '24 hours'"
        ) or 0
        total_bal   = await conn.fetchval(
            "SELECT COALESCE(SUM(balance),0) FROM users"
        ) or 0

        # Case opens
        total_opens = await conn.fetchval(
            "SELECT COALESCE(SUM(total_opens),0) FROM users"
        ) or 0
        opens_24h   = await conn.fetchval(
            "SELECT COUNT(*) FROM live_feed WHERE created_at > NOW() - INTERVAL '24 hours'"
        ) or 0

        # Revenue (sum of all bets placed)
        total_rev = await conn.fetchval(
            "SELECT COALESCE(SUM(bet_amount),0) FROM game_logs"
        ) or 0
        rev_30d   = await conn.fetchval(
            "SELECT COALESCE(SUM(bet_amount),0) FROM game_logs "
            "WHERE created_at > NOW() - INTERVAL '30 days'"
        ) or 0

        # VIP subscribers
        active_vip = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE vip_tier != 'none' AND vip_tier IS NOT NULL AND vip_expires_at > NOW()"
        ) or 0
        vip_by_tier = await conn.fetch(
            "SELECT vip_tier, COUNT(*) as cnt FROM users WHERE vip_tier != 'none' AND vip_tier IS NOT NULL AND vip_expires_at > NOW() GROUP BY vip_tier"
        )

        # Ticket economy
        tickets_in_circulation = await conn.fetchval(
            "SELECT COALESCE(SUM(tickets),0) FROM users"
        ) or 0
        tickets_sold_30d = await conn.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM ticket_transactions WHERE source='purchase' AND created_at > NOW() - INTERVAL '30 days'"
        ) or 0

    return {
        "users": {
            "total_users": int(total_users),
            "new_users_24h": int(new_24h),
            "total_balance_in_economy": float(total_bal),
        },
        "case_opens": {
            "total_opens": int(total_opens),
            "opens_24h":   int(opens_24h),
        },
        "revenue": {
            "total_revenue": float(total_rev),
            "revenue_30d":   float(rev_30d),
        },
        "vip": {
            "active_subscribers": int(active_vip),
            "by_tier": {r["vip_tier"]: int(r["cnt"]) for r in vip_by_tier},
        },
        "tickets": {
            "in_circulation": int(tickets_in_circulation),
            "sold_30d":       int(tickets_sold_30d),
        },
    }

# ============================================================
# USERS
# ============================================================

@router.get("/users")
async def admin_users(
    limit: int = 20, offset: int = 0, search: str = "",
    admin_id: int = Depends(require_admin)
):
    limit  = max(1, min(200, limit))
    offset = max(0, offset)
    pool = await get_db()
    async with pool.acquire() as conn:
        if search:
            rows = await conn.fetch("""
                SELECT user_id, username, balance, total_opens,
                       level, prestige, is_banned, vip_tier, tickets
                FROM users
                WHERE username ILIKE $1 OR user_id::text = $2
                ORDER BY balance DESC LIMIT $3 OFFSET $4
            """, f"%{search}%", search.strip(), limit, offset)
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE username ILIKE $1 OR user_id::text=$2",
                f"%{search}%", search.strip()
            )
        else:
            rows = await conn.fetch("""
                SELECT user_id, username, balance, total_opens,
                       level, prestige, is_banned, vip_tier, tickets
                FROM users ORDER BY balance DESC LIMIT $1 OFFSET $2
            """, limit, offset)
            total = await conn.fetchval("SELECT COUNT(*) FROM users")

    return {
        "users": [convert_decimals(dict(r)) for r in rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }

@router.get("/users/{user_id}")
async def admin_user_detail(
    user_id: int, admin_id: int = Depends(require_admin)
):
    pool = await get_db()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE user_id=$1", user_id
        )
        if not user:
            raise HTTPException(404, "User not found")

        inv_total = await conn.fetchval(
            "SELECT COUNT(*) FROM inventory WHERE user_id=$1", user_id
        ) or 0
        inv_value = await conn.fetchval(
            "SELECT COALESCE(SUM(price),0) FROM inventory WHERE user_id=$1", user_id
        ) or 0

        # VIP perks row (may not exist for free users)
        vip_perks = await conn.fetchrow(
            "SELECT * FROM vip_perks WHERE user_id=$1", user_id
        )

        # Last 5 ticket transactions
        ticket_history = await conn.fetch("""
            SELECT amount, source, created_at FROM ticket_transactions
            WHERE user_id=$1 ORDER BY created_at DESC LIMIT 5
        """, user_id)

    return {
        "user": convert_decimals(dict(user)),
        "inventory_summary": {
            "total_items": int(inv_total),
            "total_value": float(inv_value),
        },
        "vip": {
            "tier":        user.get("vip_tier") or "none",
            "expires_at":  user.get("vip_expires_at").isoformat() if user.get("vip_expires_at") else None,
            "tickets":     int(user.get("tickets") or 0),
            "perks":       convert_decimals(dict(vip_perks)) if vip_perks else {},
        },
        "ticket_history": [convert_decimals(dict(r)) for r in ticket_history],
    }

class BalanceAdjust(BaseModel):
    amount:  float
    reason:  str = "Admin adjustment"

@router.post("/users/{user_id}/balance")
async def admin_adjust_balance(
    user_id: int, body: BalanceAdjust,
    request: Request, admin_id: int = Depends(require_admin)
):
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Fix 23: fetch new balance from the UPDATE itself, log it alongside delta
            new_bal = await conn.fetchval(
                """
                UPDATE users
                SET balance = GREATEST(0, balance + $1), updated_at = NOW()
                WHERE user_id = $2
                RETURNING balance
                """,
                body.amount, user_id
            )
            user = await conn.fetchrow(
                "SELECT username FROM users WHERE user_id=$1", user_id
            )
            await audit(conn, admin_id, "balance_adjust",
                       target_id=user_id,
                       target_username=user["username"] if user else None,
                       details={
                           "amount":      body.amount,
                           "reason":      body.reason,
                           "new_balance": float(new_bal or 0),
                       })
    return {"success": True, "new_balance": float(new_bal or 0)}

class BanBody(BaseModel):
    reason:        str = "Admin ban"
    duration_days: Optional[int] = None

@router.post("/users/{user_id}/ban")
async def admin_ban_user(
    user_id: int, body: BanBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    expires = None
    if body.duration_days:
        expires = (datetime.now(timezone.utc) + timedelta(days=body.duration_days)).replace(tzinfo=None)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE users SET is_banned=TRUE, ban_reason=$1, ban_expires=$2 WHERE user_id=$3",
                body.reason, expires, user_id
            )
            user = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
            await audit(conn, admin_id, "ban",
                       target_id=user_id,
                       target_username=user["username"] if user else None,
                       details={"reason": body.reason, "duration_days": body.duration_days})
    return {"success": True}

@router.post("/users/{user_id}/unban")
async def admin_unban_user(
    user_id: int, request: Request, admin_id: int = Depends(require_admin)
):
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE users SET is_banned=FALSE, ban_reason=NULL, ban_expires=NULL WHERE user_id=$1",
                user_id
            )
            user = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
            await audit(conn, admin_id, "unban",
                       target_id=user_id,
                       target_username=user["username"] if user else None)
    return {"success": True}

# ── Admin VIP management ──────────────────────────────────────

class GrantTicketsBody(BaseModel):
    amount: int
    reason: str = "Admin grant"

@router.post("/users/{user_id}/tickets")
async def admin_grant_tickets(
    user_id: int, body: GrantTicketsBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    """Grant or deduct tickets from a user (use negative amount to deduct)."""
    if body.amount == 0:
        raise HTTPException(400, "Amount cannot be zero")
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Atomic: only deduct when tickets won't go negative; no TOCTOU window
            new_tickets = await conn.fetchval("""
                UPDATE users SET tickets = tickets + $1
                WHERE user_id = $2 AND (tickets + $1 >= 0 OR $1 > 0)
                RETURNING tickets
            """, body.amount, user_id)
            if new_tickets is None:
                current = await conn.fetchval(
                    "SELECT tickets FROM users WHERE user_id=$1", user_id
                ) or 0
                raise HTTPException(400, f"User only has {current} tickets")
            await conn.execute("""
                INSERT INTO ticket_transactions (user_id, amount, source, metadata)
                VALUES ($1, $2, 'admin', $3)
            """, user_id, body.amount, json.dumps({"reason": body.reason, "admin_id": admin_id}))
            user = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
            await audit(conn, admin_id, "grant_tickets",
                       target_id=user_id,
                       target_username=user["username"] if user else None,
                       details={"amount": body.amount, "reason": body.reason,
                                "new_balance": int(new_tickets or 0)})
    return {"success": True, "new_ticket_balance": int(new_tickets or 0)}

class SetVIPBody(BaseModel):
    tier:      str            # silver | gold | platinum | none
    days:      int  = 30      # how long to grant

@router.post("/users/{user_id}/vip")
async def admin_set_vip(
    user_id: int, body: SetVIPBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    """Manually grant, change, or revoke a user's VIP tier."""
    valid_tiers = ("silver", "gold", "platinum", "none")
    if body.tier not in valid_tiers:
        raise HTTPException(400, f"tier must be one of: {valid_tiers}")

    VIP_BOOSTS = {"silver": 1.05, "gold": 1.10, "platinum": 1.20, "none": 1.0}
    boost = VIP_BOOSTS[body.tier]

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if body.tier == "none":
                await conn.execute("""
                    UPDATE users SET vip_tier='none', vip_expires_at=NOW(),
                    vip_boost_multiplier=1.0 WHERE user_id=$1
                """, user_id)
            else:
                expires = datetime.utcnow() + timedelta(days=body.days)
                await conn.execute("""
                    UPDATE users SET vip_tier=$1, vip_expires_at=$2,
                    vip_boost_multiplier=$3 WHERE user_id=$4
                """, body.tier, expires, boost, user_id)
                # Upsert vip_perks
                await conn.execute("""
                    INSERT INTO vip_perks (user_id, private_rooms_enabled, tournament_access)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id) DO UPDATE
                    SET private_rooms_enabled=$2, tournament_access=$3, updated_at=NOW()
                """, user_id,
                    body.tier in ("gold", "platinum"),
                    body.tier == "platinum")

            user = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
            await audit(conn, admin_id, "set_vip",
                       target_id=user_id,
                       target_username=user["username"] if user else None,
                       details={"tier": body.tier, "days": body.days})
    _invalidate_vip_cache(user_id)
    return {"success": True, "tier": body.tier}

# ── VIP list, bulk grant/revoke, and revenue stats ────────────

@router.get("/vip/users")
async def admin_vip_users(
    tier: str = "",
    limit: int = 50,
    offset: int = 0,
    admin_id: int = Depends(require_admin)
):
    """List all VIP users with tier, expiry, tickets, and total ticket spend."""
    limit  = max(1, min(200, limit))
    offset = max(0, offset)
    pool = await get_db()
    async with pool.acquire() as conn:
        where = "WHERE vip_tier != 'none' AND vip_tier IS NOT NULL AND vip_expires_at > NOW()"
        params: list = []
        if tier in ("silver", "gold", "platinum"):
            where += " AND vip_tier = $1"
            params.append(tier)

        idx = len(params)
        rows = await conn.fetch(f"""
            SELECT u.user_id, u.username, u.vip_tier, u.vip_expires_at,
                   u.tickets, u.stripe_customer_id,
                   COALESCE(SUM(CASE WHEN tt.amount > 0 THEN tt.amount ELSE 0 END), 0) AS tickets_received,
                   COALESCE(SUM(CASE WHEN tt.source = 'purchase' THEN 1 ELSE 0 END), 0) AS purchases
            FROM users u
            LEFT JOIN ticket_transactions tt ON tt.user_id = u.user_id
            {where}
            GROUP BY u.user_id
            ORDER BY u.vip_expires_at DESC
            LIMIT ${idx+1} OFFSET ${idx+2}
        """, *params, limit, offset)

        total = await conn.fetchval(f"""
            SELECT COUNT(*) FROM users {where}
        """, *params)

    return {
        "users": [convert_decimals(dict(r)) for r in rows],
        "total": int(total or 0),
    }

class VIPGrantBody(BaseModel):
    user_id: str  # str to prevent JS BigInt truncation
    tier:    str
    days:    int = 30

@router.post("/vip/grant")
async def admin_vip_grant(
    body: VIPGrantBody, request: Request,
    admin_id: int = Depends(require_admin)
):
    """Grant a VIP tier to any user by user_id."""
    uid = int(body.user_id)
    valid = ("silver", "gold", "platinum")
    if body.tier not in valid:
        raise HTTPException(400, f"tier must be one of: {valid}")
    VIP_BOOSTS = {"silver": 1.05, "gold": 1.10, "platinum": 1.20}
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            expires = datetime.utcnow() + timedelta(days=body.days)
            await conn.execute("""
                UPDATE users SET vip_tier=$1, vip_expires_at=$2, vip_boost_multiplier=$3
                WHERE user_id=$4
            """, body.tier, expires, VIP_BOOSTS[body.tier], uid)
            await conn.execute("""
                INSERT INTO vip_perks (user_id, private_rooms_enabled, tournament_access)
                VALUES ($1,$2,$3)
                ON CONFLICT (user_id) DO UPDATE
                SET private_rooms_enabled=$2, tournament_access=$3, updated_at=NOW()
            """, uid,
                body.tier in ("gold", "platinum"),
                body.tier == "platinum")
            user = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
            await audit(conn, admin_id, "vip_grant",
                       target_id=uid,
                       target_username=user["username"] if user else None,
                       details={"tier": body.tier, "days": body.days})
    _invalidate_vip_cache(uid)
    return {"success": True, "tier": body.tier, "days": body.days}

class VIPRevokeBody(BaseModel):
    user_id: str  # str to prevent JS BigInt truncation
    reason:  str = "Admin revoke"

@router.post("/vip/revoke")
async def admin_vip_revoke(
    body: VIPRevokeBody, request: Request,
    admin_id: int = Depends(require_admin)
):
    """Immediately revoke VIP from a user."""
    uid = int(body.user_id)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                UPDATE users SET vip_tier='none', vip_expires_at=NOW(),
                vip_boost_multiplier=1.0 WHERE user_id=$1
            """, uid)
            user = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
            await audit(conn, admin_id, "vip_revoke",
                       target_id=uid,
                       target_username=user["username"] if user else None,
                       details={"reason": body.reason})
    _invalidate_vip_cache(uid)
    return {"success": True}

@router.get("/vip/stats")
async def admin_vip_stats(admin_id: int = Depends(require_admin)):
    """Revenue and subscriber breakdown by VIP tier."""
    pool = await get_db()
    async with pool.acquire() as conn:
        # Active subscribers by tier
        tier_counts = await conn.fetch("""
            SELECT vip_tier, COUNT(*) AS subscribers
            FROM users
            WHERE vip_tier != 'none' AND vip_tier IS NOT NULL AND vip_expires_at > NOW()
            GROUP BY vip_tier
        """)
        # Ticket purchases by source
        ticket_revenue = await conn.fetch("""
            SELECT source, SUM(amount) AS total_tickets, COUNT(*) AS transactions
            FROM ticket_transactions
            WHERE amount > 0
            GROUP BY source
        """)
        # Tickets spent this month
        spent_month = await conn.fetchval("""
            SELECT COALESCE(ABS(SUM(amount)), 0)
            FROM ticket_transactions
            WHERE amount < 0 AND created_at > NOW() - INTERVAL '30 days'
        """) or 0
        # Total tickets ever granted
        total_granted = await conn.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM ticket_transactions WHERE amount > 0"
        ) or 0

    return {
        "subscribers": {r["vip_tier"]: int(r["subscribers"]) for r in tier_counts},
        "ticket_sources": [convert_decimals(dict(r)) for r in ticket_revenue],
        "tickets_spent_30d": int(spent_month),
        "tickets_granted_total": int(total_granted),
    }

@router.get("/cases")
async def admin_cases(admin_id: int = Depends(require_admin)):
    pool = await get_db()
    async with pool.acquire() as conn:
        # Merge in-memory CASES with any DB overrides
        rows = await conn.fetch("SELECT id, price, featured FROM case_prices")
        price_map    = {r["id"]: float(r["price"]) for r in rows if r["price"] is not None}
        featured_map = {r["id"]: bool(r["featured"])    for r in rows}

    return {
        "cases": [
            {
                "id":       case_id,
                "name":     c["name"],
                "emoji":    c.get("emoji", "📦"),
                "price":    price_map.get(case_id, float(c.get("price", 1000))),
                "featured": featured_map.get(case_id, False),
            }
            for case_id, c in CASES.items()
        ]
    }

class PriceBody(BaseModel):
    price: float

@router.post("/cases/{case_id}/price")
async def admin_update_case_price(
    case_id: str, body: PriceBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    if body.price <= 0:
        raise HTTPException(400, "Price must be positive")
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Seed featured from the static default on first insert so a
            # price-only edit doesn't silently un-feature an already-featured case.
            await conn.execute("""
                INSERT INTO case_prices (id, price, featured)
                VALUES ($1,$2,$3)
                ON CONFLICT (id) DO UPDATE SET price=$2, updated_at=NOW()
            """, case_id, body.price, case_id in FEATURED_CASES)
            await audit(conn, admin_id, "update_case_price",
                       details={"case_id": case_id, "price": body.price})
    invalidate_case_override_cache()
    return {"success": True}

class FeaturedBody(BaseModel):
    featured: bool

@router.post("/cases/{case_id}/featured")
async def admin_toggle_featured(
    case_id: str, body: FeaturedBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Seed price from the static default on first insert so a
            # featured-only edit doesn't leave price NULL.
            default_price = float(CASES.get(case_id, {}).get("price", 1000))
            await conn.execute("""
                INSERT INTO case_prices (id, price, featured)
                VALUES ($1,$2,$3)
                ON CONFLICT (id) DO UPDATE SET featured=$3, updated_at=NOW()
            """, case_id, default_price, body.featured)
            await audit(conn, admin_id, "toggle_featured",
                       details={"case_id": case_id, "featured": body.featured})
    invalidate_case_override_cache()
    return {"success": True}

# ============================================================
# STICKER CAPSULES
# ============================================================

@router.get("/capsules")
async def admin_capsules(admin_id: int = Depends(require_admin)):
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, price, featured FROM capsule_prices")
        price_map    = {r["id"]: float(r["price"]) for r in rows if r["price"] is not None}
        featured_map = {r["id"]: bool(r["featured"])    for r in rows}

    return {
        "capsules": [
            {
                "id":       capsule_id,
                "name":     c["name"],
                "emoji":    fix_surrogate_emoji(c.get("emoji", "🧷")),
                "price":    price_map.get(capsule_id, float(c.get("price", 1))),
                "featured": featured_map.get(capsule_id, False),
            }
            for capsule_id, c in STICKER_CAPSULES.items()
        ]
    }

@router.post("/capsules/{capsule_id}/price")
async def admin_update_capsule_price(
    capsule_id: str, body: PriceBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    if body.price <= 0:
        raise HTTPException(400, "Price must be positive")
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO capsule_prices (id, price)
                VALUES ($1,$2)
                ON CONFLICT (id) DO UPDATE SET price=$2, updated_at=NOW()
            """, capsule_id, body.price)
            await audit(conn, admin_id, "update_capsule_price",
                       details={"capsule_id": capsule_id, "price": body.price})
    invalidate_capsule_override_cache()
    return {"success": True}

@router.post("/capsules/{capsule_id}/featured")
async def admin_toggle_capsule_featured(
    capsule_id: str, body: FeaturedBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            default_price = float(STICKER_CAPSULES.get(capsule_id, {}).get("price", 1))
            await conn.execute("""
                INSERT INTO capsule_prices (id, price, featured)
                VALUES ($1,$2,$3)
                ON CONFLICT (id) DO UPDATE SET featured=$3, updated_at=NOW()
            """, capsule_id, default_price, body.featured)
            await audit(conn, admin_id, "toggle_capsule_featured",
                       details={"capsule_id": capsule_id, "featured": body.featured})
    invalidate_capsule_override_cache()
    return {"success": True}

# ============================================================
# ECONOMY / ANALYTICS
# ============================================================

@router.get("/analytics/economy")
async def admin_economy(admin_id: int = Depends(require_admin)):
    pool = await get_db()
    async with pool.acquire() as conn:
        total_bal   = await conn.fetchval("SELECT COALESCE(SUM(balance),0) FROM users") or 0
        avg_bal     = await conn.fetchval("SELECT COALESCE(AVG(balance),0) FROM users") or 0
        inv_val     = await conn.fetchval(
            "SELECT COALESCE(SUM(price),0) FROM inventory"
        ) or 0
        total_opens = await conn.fetchval(
            "SELECT COALESCE(SUM(total_opens),0) FROM users"
        ) or 0
        total_golds = await conn.fetchval(
            "SELECT COALESCE(SUM(total_golds),0) FROM users"
        ) or 0
    return {
        "total_balance":         float(total_bal),
        "avg_balance":           float(avg_bal),
        "total_inventory_value": float(inv_val),
        "total_cases_opened":    int(total_opens),
        "total_golds":           int(total_golds),
    }

# ============================================================
# GAME SETTINGS
# ============================================================

@router.get("/games/settings")
async def admin_game_settings(admin_id: int = Depends(require_admin)):
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT game_name, settings FROM game_settings ORDER BY updated_at ASC NULLS FIRST"
        )
    settings = {}
    for r in rows:
        try:
            settings[r["game_name"]] = json.loads(r["settings"])
        except Exception:
            settings[r["game_name"]] = {}
    # Add defaults for any missing games
    defaults = [
        "slots", "coinflip", "dice", "crash", "mines", "plinko",
        "tower", "blackjack", "roulette", "baccarat", "poker",
        "dragon-tiger", "ladder-climb", "limbo", "russian-roulette",
        "slide", "slots-bomb", "slots-cs2", "slots-jackpot", "skin-spin",
        "bomb-defuse", "reaction", "aim-trainer", "memory-sequence",
        "keno", "float-guesser", "mystery-box", "shotgun", "hilo",
        "live-race",
    ]
    for g in defaults:
        if g not in settings:
            settings[g] = {"enabled": "true", "house_edge": "0.04"}
    return {"settings": settings}

class GameSettingBody(BaseModel):
    game_name: str
    settings:  dict

@router.post("/games/settings")
async def admin_update_game_settings(
    body: GameSettingBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Merge with existing
            existing = await conn.fetchval(
                "SELECT settings FROM game_settings WHERE game_name=$1", body.game_name
            )
            current = json.loads(existing) if existing else {}
            current.update(body.settings)
            await conn.execute("""
                INSERT INTO game_settings (game_name, settings)
                VALUES ($1,$2)
                ON CONFLICT (game_name) DO UPDATE SET settings=$2, updated_at=NOW()
            """, body.game_name, json.dumps(current))
            await audit(conn, admin_id, "update_game_settings",
                       details={"game": body.game_name, "changes": body.settings})
    invalidate_game_enabled_cache()
    return {"success": True}

# ============================================================
# GIVEAWAYS
# ============================================================

@router.get("/giveaways")
async def admin_giveaways(admin_id: int = Depends(require_admin)):
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT g.*, COUNT(e.id) as entries_count
            FROM giveaways g
            LEFT JOIN giveaway_entries e ON e.giveaway_id = g.id
            GROUP BY g.id ORDER BY g.created_at DESC LIMIT 50
        """)
    return {"giveaways": [convert_decimals(dict(r)) for r in rows]}

class GiveawayBody(BaseModel):
    prize_amount:    float
    winner_count:    int   = 1
    duration_minutes: int  = 60
    required_level:  int   = 0
    required_opens:  int   = 0

@router.post("/giveaway/create")
async def admin_create_giveaway(
    body: GiveawayBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    end_time = datetime.utcnow().replace(tzinfo=None) + timedelta(minutes=body.duration_minutes)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                INSERT INTO giveaways
                    (prize_amount, winner_count, end_time,
                     required_level, required_opens, status, created_by)
                VALUES ($1,$2,$3,$4,$5,'active',$6)
                RETURNING id
            """, body.prize_amount, body.winner_count, end_time,
                body.required_level, body.required_opens, admin_id)
            await audit(conn, admin_id, "create_giveaway",
                       details={"prize": body.prize_amount, "winners": body.winner_count})
    return {"success": True, "giveaway_id": row["id"]}

@router.post("/giveaways/{giveaway_id}/draw")
async def admin_draw_giveaway(
    giveaway_id: int,
    request: Request, admin_id: int = Depends(require_admin)
):
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            giveaway = await conn.fetchrow(
                "SELECT * FROM giveaways WHERE id=$1 FOR UPDATE", giveaway_id
            )
            if not giveaway:
                raise HTTPException(404, "Giveaway not found")
            if giveaway["status"] != "active":
                return {"success": False, "error": "Giveaway already drawn or cancelled"}

            entries = await conn.fetch(
                "SELECT user_id FROM giveaway_entries WHERE giveaway_id=$1", giveaway_id
            )
            if not entries:
                return {"success": False, "error": "No entries yet"}

            from shared import secure_shuffle
            entry_ids = [e["user_id"] for e in entries]
            n_winners = min(giveaway["winner_count"], len(entry_ids))
            winners   = secure_shuffle(entry_ids)[:n_winners]
            prize_each = float(giveaway["prize_amount"]) / n_winners

            for uid in winners:
                await add_balance(uid, prize_each, conn)

            await conn.execute(
                "UPDATE giveaways SET status='completed', drawn_at=NOW() WHERE id=$1",
                giveaway_id
            )
            await audit(conn, admin_id, "draw_giveaway",
                       details={"giveaway_id": giveaway_id, "winners": winners,
                                "prize_each": prize_each})

    return {"success": True, "winners": winners, "prize_each": prize_each}

# ============================================================
# SECRET INVENTORY DEPOSIT
# ============================================================

class DepositBody(BaseModel):
    user_id:           str  # str to prevent JS BigInt truncation
    item_name:        str
    rarity:           str = "Blue"
    condition:        str = "Field-Tested"
    is_stattrak:      bool = False
    custom_price:     Optional[float] = None
    custom_message:   str = "Thanks for playing CS2CaseBot! 🎉"
    send_notification: bool = True

@router.post("/inventory/deposit")
async def admin_inventory_deposit(
    body: DepositBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    uid = int(body.user_id)
    RARITY_PRICES = {
        "Blue": 500, "Purple": 2000, "Pink": 5000,
        "Red": 15000, "Gold": 50000,
    }
    price = body.custom_price or RARITY_PRICES.get(body.rarity, 500)

    pool = await get_db()
    async with pool.acquire() as conn:
        # Check user exists
        user = await conn.fetchrow(
            "SELECT username FROM users WHERE user_id=$1", uid
        )
        if not user:
            raise HTTPException(404, "User not found")

        async with conn.transaction():
            await conn.execute("""
                INSERT INTO inventory
                    (user_id, item_name, rarity, condition,
                     is_stattrak, price, source, acquired_at)
                VALUES ($1,$2,$3,$4,$5,$6,'admin_deposit',NOW())
            """, uid, body.item_name, body.rarity,
                body.condition, body.is_stattrak, price)

            await audit(conn, admin_id, "inventory_deposit",
                       target_id=uid,
                       target_username=user["username"],
                       details={
                           "item": body.item_name,
                           "rarity": body.rarity,
                           "price": price,
                       })

    # Try to DM user via Discord bot if notification requested
    if body.send_notification:
        try:
            # shared.bot_notify is set by main.py if the bot is running
            if hasattr(shared, 'bot_notify') and shared.bot_notify:
                await shared.bot_notify(
                    uid,
                    f"🎁 **Secret Gift!**\n"
                    f"An admin deposited **{body.item_name}** "
                    f"({body.rarity}) into your inventory!\n"
                    f"{body.custom_message}"
                )
        except Exception as e:
            logger.warning(f"Could not DM user {uid}: {e}")

    return {
        "success": True,
        "message": f"✅ {body.item_name} deposited to {user['username']}'s inventory",
    }

# ============================================================
# ANNOUNCEMENTS
# ============================================================

@router.get("/announcements")
async def admin_announcements(admin_id: int = Depends(require_admin)):
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM announcements ORDER BY created_at DESC LIMIT 50"
        )
    return {"announcements": [convert_decimals(dict(r)) for r in rows]}

class AnnouncementBody(BaseModel):
    title:   str
    message: str
    type:    str = "info"   # info | warning | event

@router.post("/announcements")
async def admin_create_announcement(
    body: AnnouncementBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                INSERT INTO announcements (title, message, type, created_by)
                VALUES ($1,$2,$3,$4) RETURNING id
            """, body.title, body.message, body.type, admin_id)
            await audit(conn, admin_id, "create_announcement",
                       details={"title": body.title, "type": body.type})
    return {"success": True, "id": row["id"]}

@router.delete("/announcements/{announcement_id}")
async def admin_delete_announcement(
    announcement_id: int,
    request: Request, admin_id: int = Depends(require_admin)
):
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "DELETE FROM announcements WHERE id=$1 RETURNING id, title", announcement_id
            )
            if not row:
                raise HTTPException(404, "Announcement not found")
            await audit(conn, admin_id, "delete_announcement",
                       details={"id": announcement_id, "title": row["title"]})
    return {"success": True}

# ============================================================
# SETTINGS
# ============================================================

@router.get("/settings")
async def admin_get_settings(admin_id: int = Depends(require_admin)):
    pool = await get_db()
    settings = await get_settings(pool)   # Fix 19: pass pool not conn
    # Defaults
    defaults = {
        "site_name": "CS2CaseBot",
        "default_currency": "$",
        "support_discord_link": "https://discord.gg/mU33pc7TDE",
        "maintenance_mode": "false",
        "maintenance_message": "We'll be back soon!",
    }
    defaults.update(settings)
    return {"settings": defaults}

@router.post("/settings")
async def admin_save_settings(
    request: Request, admin_id: int = Depends(require_admin)
):
    body = await request.json()
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for key, value in body.items():
                await set_setting(conn, key, str(value))
            await audit(conn, admin_id, "update_settings",
                       details={"keys": list(body.keys())})
    invalidate_settings_cache()   # Fix 19: invalidate after save
    return {"success": True}

# ============================================================
# AUDIT LOG
# ============================================================

@router.get("/audit-log")
async def admin_audit_log(
    limit: int = 20, offset: int = 0,
    action_type: str = "",
    admin_id: int = Depends(require_admin)
):
    limit  = max(1, min(200, limit))
    offset = max(0, offset)
    pool = await get_db()
    async with pool.acquire() as conn:
        if action_type:
            rows = await conn.fetch("""
                SELECT * FROM admin_audit_log
                WHERE action_type=$1
                ORDER BY created_at DESC LIMIT $2 OFFSET $3
            """, action_type, limit, offset)
        else:
            rows = await conn.fetch("""
                SELECT * FROM admin_audit_log
                ORDER BY created_at DESC LIMIT $1 OFFSET $2
            """, limit, offset)
    return {"logs": [convert_decimals(dict(r)) for r in rows]}

# ============================================================
# BETA TESTERS
# ============================================================

class BetaUserBody(BaseModel):
    user_id: str  # str to prevent JS BigInt truncation

@router.get("/beta/users")
async def admin_list_beta(admin_id: int = Depends(require_admin)):
    """List all beta testers."""
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT bt.user_id, u.username, bt.added_at
            FROM beta_testers bt
            LEFT JOIN users u ON u.user_id = bt.user_id
            ORDER BY bt.added_at DESC
        """)
    return {"users": [convert_decimals(dict(r)) for r in rows]}

@router.post("/beta/users")
async def admin_add_beta(
    body: BetaUserBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    uid = int(body.user_id)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO beta_testers (user_id, added_by)
                VALUES ($1,$2)
                ON CONFLICT (user_id) DO NOTHING
            """, uid, admin_id)
            await audit(conn, admin_id, "add_beta_tester", target_id=uid)
    return {"success": True}

@router.delete("/beta/users/{user_id}")
async def admin_remove_beta(
    user_id: int, admin_id: int = Depends(require_admin)
):
    """Remove a user from beta testers."""
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM beta_testers WHERE user_id=$1", user_id)
            await audit(conn, admin_id, "remove_beta_tester", target_id=user_id)
    return {"success": True}

# ============================================================
# LIVE FEED
# ============================================================

@router.get("/live-feed")
async def admin_live_feed(
    limit: int = 20,
    admin_id: int = Depends(require_admin)
):
    limit = max(1, min(200, limit))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, username, item_name, rarity, rarity_emoji,
                   case_type, float_value, created_at
            FROM live_feed
            ORDER BY created_at DESC LIMIT $1
        """, limit)
    return {"feed": [convert_decimals(dict(r)) for r in rows]}

# ============================================================
# PREMIUM TOGGLE
# ============================================================

@router.post("/premium/toggle")
async def admin_toggle_premium(
    request: Request, admin_id: int = Depends(require_admin)
):
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchval(
                "SELECT value FROM admin_settings WHERE key='premium_enabled'"
            )
            new_val = "false" if current == "true" else "true"
            await set_setting(conn, "premium_enabled", new_val)
            await audit(conn, admin_id, "toggle_premium",
                       details={"enabled": new_val})
    invalidate_settings_cache()   # Keep cache consistent after toggle
    return {"success": True, "premium_enabled": new_val == "true"}

# ============================================================
# BACKUP
# ============================================================

@router.get("/backup/create")
async def admin_create_backup(
    request: Request, admin_id: int = Depends(require_admin)
):
    """Trigger a pg_dump backup of the database."""
    import shutil, tempfile
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        raise HTTPException(500, "pg_dump not available")

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename  = f"backup_{timestamp}.sql"
    out_path  = os.path.join(tempfile.gettempdir(), filename)

    db_url = os.getenv("DATABASE_URL", "")
    try:
        proc = await asyncio.create_subprocess_exec(
            pg_dump, db_url, "-f", out_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(500, "Backup timed out")
        if proc.returncode != 0:
            err_text = stderr.decode(errors='replace')[:500]
            logger.error(f"Backup failed: {err_text}")
            raise HTTPException(500, "Backup failed")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Backup error: {e}")
        raise HTTPException(500, "Backup failed")

    pool = await get_db()
    async with pool.acquire() as conn:
        await audit(conn, admin_id, "create_backup",
                   details={"filename": filename})

    return {"success": True, "filename": filename}

# ============================================================
# FIRE SALE
# ============================================================

class FireSaleBody(BaseModel):
    name:             str
    discount_percent: int
    duration_hours:   int
    case_type:        Optional[str] = None

@router.post("/fire-sale")
async def admin_fire_sale(
    body: FireSaleBody,
    request: Request, admin_id: int = Depends(require_admin)
):
    if not 1 <= body.discount_percent <= 90:
        raise HTTPException(400, "Discount must be 1–90%")

    expires_at = datetime.now(timezone.utc) + timedelta(hours=body.duration_hours)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO fire_sales
                    (name, discount_percent, case_type, expires_at, created_by)
                VALUES ($1,$2,$3,$4,$5)
            """, body.name, body.discount_percent, body.case_type,
                expires_at, admin_id)
            await audit(conn, admin_id, "start_fire_sale",
                       details={
                           "name": body.name,
                           "discount": body.discount_percent,
                           "hours": body.duration_hours,
                       })
    invalidate_case_override_cache()
    return {"success": True}

# ============================================================
# DB TABLE INIT — called from server.py lifespan
# ============================================================

async def init_admin_tables():
    """Create all admin-specific tables if they don't exist."""
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id            BIGSERIAL PRIMARY KEY,
                admin_id      BIGINT,
                admin_username TEXT,
                action_type   TEXT NOT NULL,
                target_id     BIGINT,
                target_username TEXT,
                details       JSONB DEFAULT '{}',
                created_at    TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit_log(admin_id);
            CREATE INDEX IF NOT EXISTS idx_audit_time  ON admin_audit_log(created_at DESC);

            CREATE TABLE IF NOT EXISTS admin_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS game_settings (
                game_name  TEXT PRIMARY KEY,
                settings   JSONB DEFAULT '{}',
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS case_prices (
                id         TEXT PRIMARY KEY,
                price      NUMERIC(12,2),
                featured   BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS capsule_prices (
                id         TEXT PRIMARY KEY,
                price      NUMERIC(12,2),
                featured   BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            -- Note: giveaways table is created by server.py _init_all_tables.
            -- This admin module only adds columns via ALTER TABLE in migrations.
            -- Schema is maintained in server.py to avoid conflicts.
            -- The actual table uses TIMESTAMP (not TIMESTAMPTZ) for end_time/ends_at.

            CREATE TABLE IF NOT EXISTS giveaway_entries (
                id           BIGSERIAL PRIMARY KEY,
                giveaway_id  BIGINT REFERENCES giveaways(id) ON DELETE CASCADE,
                user_id      BIGINT,
                entered_at   TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(giveaway_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS announcements (
                id         BIGSERIAL PRIMARY KEY,
                title      TEXT NOT NULL,
                message    TEXT NOT NULL,
                type       TEXT DEFAULT 'info',
                created_by BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS beta_testers (
                user_id  BIGINT PRIMARY KEY,
                added_by BIGINT,
                added_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS fire_sales (
                id               BIGSERIAL PRIMARY KEY,
                name             TEXT NOT NULL,
                discount_percent INT NOT NULL,
                case_type        TEXT,
                expires_at       TIMESTAMPTZ NOT NULL,
                created_by       BIGINT,
                created_at       TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # Fix 3: Jackpot persistence tables
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS jackpot_state (
                id         INTEGER PRIMARY KEY DEFAULT 1,
                pot        DECIMAL(15,2) DEFAULT 0,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS jackpot_entries (
                id         SERIAL PRIMARY KEY,
                jackpot_id INTEGER DEFAULT 1,
                user_id    BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                amount     DECIMAL(15,2),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            INSERT INTO jackpot_state (id, pot) VALUES (1, 0)
            ON CONFLICT (id) DO NOTHING
        """)

        # Fix 21: Blackjack session persistence table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS blackjack_sessions (
                user_id      BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                player_cards TEXT[],
                dealer_cards TEXT[],
                bet          DECIMAL(15,2),
                status       TEXT DEFAULT 'active',
                split_hand   TEXT[],
                created_at   TIMESTAMP DEFAULT NOW(),
                updated_at   TIMESTAMP DEFAULT NOW()
            )
        """)

        # Schema migrations: add columns that may be missing from pre-existing tables
        for migration_sql in [
            "ALTER TABLE game_settings ADD COLUMN IF NOT EXISTS settings JSONB DEFAULT '{}'",
            "ALTER TABLE game_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
            # Legacy per-key-per-row schema left a NOT NULL setting_key column that
            # the current JSONB-blob code never populates — every INSERT fails on
            # it regardless of the ON CONFLICT constraint. Confirmed via direct
            # DB test (NotNullViolationError on setting_key) 2026-07-03.
            "ALTER TABLE game_settings ALTER COLUMN setting_key DROP NOT NULL",
            "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS created_by BIGINT",
            "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
            "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS type TEXT DEFAULT 'info'",
            # fire_sales / case_prices / capsule_prices may pre-date this session's
            # schema (e.g. an older/partial table already existed) — backfill columns.
            "ALTER TABLE fire_sales ADD COLUMN IF NOT EXISTS name TEXT",
            "ALTER TABLE fire_sales ADD COLUMN IF NOT EXISTS discount_percent INT",
            "ALTER TABLE fire_sales ADD COLUMN IF NOT EXISTS case_type TEXT",
            "ALTER TABLE fire_sales ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
            "ALTER TABLE fire_sales ADD COLUMN IF NOT EXISTS created_by BIGINT",
            "ALTER TABLE fire_sales ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
            "ALTER TABLE case_prices ADD COLUMN IF NOT EXISTS price NUMERIC(12,2)",
            "ALTER TABLE case_prices ADD COLUMN IF NOT EXISTS featured BOOLEAN DEFAULT FALSE",
            "ALTER TABLE case_prices ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
            "ALTER TABLE capsule_prices ADD COLUMN IF NOT EXISTS price NUMERIC(12,2)",
            "ALTER TABLE capsule_prices ADD COLUMN IF NOT EXISTS featured BOOLEAN DEFAULT FALSE",
            "ALTER TABLE capsule_prices ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()",
        ]:
            try:
                await conn.execute(migration_sql)
            except Exception as e:
                logger.warning(f"Schema migration skipped: {e}")

        # game_settings.game_name / case_prices.id / capsule_prices.id need a
        # unique constraint for the ON CONFLICT upserts to work — some of these
        # tables may pre-date this session and were created without one.
        # Just try to add a uniquely-named constraint every startup and swallow
        # "already exists" — checking pg_constraint first was unreliable (a table
        # with a PK on some *other* column looks "already constrained" even
        # though the column ON CONFLICT actually needs still has none).
        for table, col in [
            ("game_settings", "game_name"),
            ("case_prices", "id"),
            ("capsule_prices", "id"),
        ]:
            constraint_name = f"{table}_{col}_uniq"
            try:
                await conn.execute(
                    f"ALTER TABLE {table} ADD CONSTRAINT {constraint_name} UNIQUE ({col})"
                )
                logger.info(f"Added unique constraint {constraint_name}")
            except asyncpg.exceptions.DuplicateObjectError:
                pass
            except asyncpg.exceptions.UniqueViolationError:
                # Table pre-dates the constraint and accumulated duplicate rows
                # per key (from the old INSERT-without-upsert code path). Keep
                # the most recently updated row per key, drop the rest, then
                # retry adding the constraint.
                try:
                    await conn.execute(f"""
                        DELETE FROM {table} a USING {table} b
                        WHERE a.{col} = b.{col}
                          AND (a.updated_at, a.ctid) < (b.updated_at, b.ctid)
                    """)
                    await conn.execute(
                        f"ALTER TABLE {table} ADD CONSTRAINT {constraint_name} UNIQUE ({col})"
                    )
                    logger.info(f"Deduplicated {table} and added unique constraint {constraint_name}")
                except asyncpg.exceptions.DuplicateObjectError:
                    pass
                except Exception as e:
                    logger.warning(f"Dedup + unique constraint retry failed for {table}.{col}: {e}")
            except Exception as e:
                logger.warning(f"Unique constraint migration skipped for {table}.{col}: {e}")

        # Fix 28: Performance indexes
        index_statements = [
            "CREATE INDEX IF NOT EXISTS idx_inventory_user_id    ON inventory (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_inventory_rarity     ON inventory (user_id, rarity)",
            "CREATE INDEX IF NOT EXISTS idx_inventory_status     ON inventory (user_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_game_logs_user       ON game_logs (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_game_logs_type       ON game_logs (game_type)",
            "CREATE INDEX IF NOT EXISTS idx_game_logs_created    ON game_logs (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_quests_user          ON quests (user_id, completed)",
            "CREATE INDEX IF NOT EXISTS idx_jackpot_entries_user ON jackpot_entries (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_live_feed_created    ON live_feed (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_users_balance        ON users (balance DESC)",
        ]
        for stmt in index_statements:
            try:
                await conn.execute(stmt)
            except Exception as e:
                logger.warning(f"Index creation skipped (table may not exist yet): {e}")

    logger.info("✅ Admin tables ready")
