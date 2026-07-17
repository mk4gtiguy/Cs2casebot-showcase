# ============================================================
# routes/premium.py
# CS2CaseBot | VIP Subscription + Ticket Economy
#
# Endpoints:
#   GET  /api/vip/status            — current tier, perks, tickets, expiry
#   GET  /api/vip/tiers             — all 3 tier options with pricing
#   POST /api/vip/subscribe         — create Stripe subscription checkout
#   POST /api/vip/cancel            — cancel VIP (sets expires_at to now)
#   POST /api/vip/premium-batch-open— spend 1 ticket, open top-5 cases
#   POST /webhook/stripe            — Stripe webhook (subscription only --
#                                     real-money ticket purchases removed)
# ============================================================

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

import shared
from shared import (
    logger, get_db, require_auth,
    add_balance, deduct_balance, ensure_user_exists,
    CASES, get_random_item, convert_decimals,
    _invalidate_vip_cache,
    secure_random, secure_choice,
    get_skin_condition, calculate_item_value, RARITY_EMOJIS,
    check_rate_limit, RATE_PAYMENT,
)

router = APIRouter(tags=["premium"])

# ============================================================
# CONFIGURATION
# ============================================================

VIP_TIERS = {
    'silver': {
        'label': '🔰 Silver',
        'price': 1.99,
        'daily_tickets': 3,
        'boost': 1.05,
        'stripe_price_id': os.getenv('STRIPE_PRICE_SILVER', 'price_silver_monthly'),
        'perks': ['Premium Case Batch', 'Priority Queue', '+5% Win Boost'],
    },
    'gold': {
        'label': '🥇 Gold',
        'price': 4.99,
        'daily_tickets': 8,
        'boost': 1.10,
        'stripe_price_id': os.getenv('STRIPE_PRICE_GOLD', 'price_gold_monthly'),
        'perks': ['All Silver perks', 'Private Poker/Battle Rooms', '+10% Win Boost'],
    },
    'platinum': {
        'label': '👑 Platinum',
        'price': 7.99,
        'daily_tickets': 15,
        'boost': 1.20,
        'stripe_price_id': os.getenv('STRIPE_PRICE_PLATINUM', 'price_platinum_monthly'),
        'perks': ['All Gold perks', 'Unlimited Batch Spins', 'VIP Tournaments', '+20% Win Boost'],
    },
}

# ============================================================
# SHARED HELPERS
# ============================================================

async def get_vip_status(user_id: int) -> dict:
    """
    Returns {'tier': 'silver'|'gold'|'platinum'|'none', 'boost': float,
             'daily_tickets': int, 'expires_at': datetime|None, 'active': bool}
    """
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT vip_tier, vip_expires_at, tickets FROM users WHERE user_id=$1",
            user_id
        )
    if not row:
        return {'tier': 'none', 'boost': 1.0, 'daily_tickets': 0,
                'expires_at': None, 'active': False, 'tickets': 0}

    tier = row['vip_tier'] or 'none'
    expires = row['vip_expires_at']
    active = (tier != 'none' and expires is not None and expires > datetime.utcnow())
    if not active:
        tier = 'none'
    cfg = VIP_TIERS.get(tier, {})
    return {
        'tier':          tier,
        'boost':         cfg.get('boost', 1.0),
        'daily_tickets': cfg.get('daily_tickets', 0),
        'expires_at':    expires,
        'active':        active,
        'tickets':       int(row['tickets'] or 0),
        'label':         cfg.get('label', ''),
        'perks':         cfg.get('perks', []),
    }

async def grant_tickets(user_id: int, amount: int, source: str, metadata: dict = None, conn=None) -> None:
    """Credit tickets and log the transaction."""
    async def _do(c):
        await c.execute(
            "UPDATE users SET tickets = tickets + $1 WHERE user_id = $2",
            amount, user_id
        )
        await c.execute("""
            INSERT INTO ticket_transactions (user_id, amount, source, metadata)
            VALUES ($1, $2, $3, $4)
        """, user_id, amount, source, json.dumps(metadata or {}))

    pool = await get_db()
    if conn:
        await _do(conn)
    else:
        async with pool.acquire() as c:
            async with c.transaction():
                await _do(c)

async def deduct_ticket(user_id: int, source: str, metadata: dict = None, conn=None) -> bool:
    """
    Deduct 1 ticket. Returns False if insufficient.
    """
    async def _do(c):
        updated = await c.fetchval("""
            UPDATE users SET tickets = tickets - 1
            WHERE user_id = $1 AND tickets >= 1
            RETURNING tickets
        """, user_id)
        if updated is None:
            return False
        await c.execute("""
            INSERT INTO ticket_transactions (user_id, amount, source, metadata)
            VALUES ($1, -1, $2, $3)
        """, user_id, source, json.dumps(metadata or {}))
        return True

    pool = await get_db()
    if conn:
        return await _do(conn)
    async with pool.acquire() as c:
        async with c.transaction():
            return await _do(c)

# ============================================================
# DB INIT — called from server.py lifespan
# ============================================================

async def init_premium_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        # VIP columns on users
        for col in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vip_tier TEXT DEFAULT 'none'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vip_expires_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vip_boost_multiplier DECIMAL(5,2) DEFAULT 1.0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vip_daily_multiplier INTEGER DEFAULT 1",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT",
        ]:
            try:
                await conn.execute(col)
            except Exception:
                pass

        # vip_perks table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vip_perks (
                user_id               BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                profile_border        TEXT DEFAULT 'none',
                avatar_frame          TEXT DEFAULT 'none',
                private_rooms_enabled BOOLEAN DEFAULT FALSE,
                tournament_access     BOOLEAN DEFAULT FALSE,
                updated_at            TIMESTAMP DEFAULT NOW()
            )
        """)

        # ticket_transactions table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ticket_transactions (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                amount     INTEGER NOT NULL,
                source     TEXT,
                metadata   JSONB,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Index for fast ticket history lookups
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ticket_tx_user
            ON ticket_transactions (user_id, created_at DESC)
        """)
    logger.info("✅ Premium tables ready")

# ============================================================
# DAILY TICKET AWARD — Background task
# ============================================================

async def daily_ticket_award_loop():
    """
    Runs forever. Awards daily tickets to active VIP users at midnight UTC.
    """
    while True:
        now = datetime.utcnow()
        # Sleep until next midnight UTC
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        await asyncio.sleep((tomorrow - now).total_seconds())
        try:
            await _run_daily_award()
        except Exception as e:
            logger.error(f"Daily ticket award error: {e}")

async def _run_daily_award():
    pool = await get_db()
    async with pool.acquire() as conn:
        active_vips = await conn.fetch("""
            SELECT user_id, vip_tier FROM users
            WHERE vip_tier != 'none'
              AND vip_tier IS NOT NULL
              AND vip_expires_at > NOW()
        """)
        awarded = 0
        for row in active_vips:
            cfg = VIP_TIERS.get(row['vip_tier'], {})
            daily = cfg.get('daily_tickets', 0)
            if daily > 0:
                # Bug 170 fix: wrap check+grant in a transaction (savepoint when
                # nested) so two concurrent award runs can't both pass the
                # idempotency check and double-grant tickets.
                async with conn.transaction():
                    already = await conn.fetchval("""
                        SELECT 1 FROM ticket_transactions
                        WHERE user_id = $1 AND source = 'daily'
                          AND created_at::date = CURRENT_DATE
                    """, row['user_id'])
                    if not already:
                        await grant_tickets(
                            row['user_id'], daily, 'daily',
                            {'tier': row['vip_tier']}, conn=conn
                        )
                        awarded += 1
        logger.info(f"✅ Daily tickets awarded to {awarded} VIP users")

# ============================================================
# VIP ENDPOINTS
# ============================================================

@router.get("/api/vip/status")
async def vip_status(request: Request):
    user_id = await require_auth(request)
    status = await get_vip_status(user_id)
    # Serialise datetime
    if status['expires_at']:
        status['expires_at'] = status['expires_at'].isoformat()
    return status

@router.get("/api/vip/tiers")
async def vip_tiers():
    return {
        "tiers": [
            {
                "id":            k,
                "label":         v['label'],
                "price":         v['price'],
                "daily_tickets": v['daily_tickets'],
                "boost_pct":     int((v['boost'] - 1) * 100),
                "perks":         v['perks'],
            }
            for k, v in VIP_TIERS.items()
        ]
    }

class SubscribeBody(BaseModel):
    tier: str  # silver | gold | platinum
    # Bug 169 fix: success_url / cancel_url removed from client input to prevent
    # open-redirect attacks where a user crafts a malicious post-payment destination.

@router.post("/api/vip/subscribe")
async def vip_subscribe(body: SubscribeBody, request: Request):
    user_id = await require_auth(request)
    await check_rate_limit(request, RATE_PAYMENT)
    if body.tier not in VIP_TIERS:
        raise HTTPException(400, "Invalid tier")

    try:
        import stripe as _stripe
        _stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')
        if not _stripe.api_key:
            raise HTTPException(503, "Payments not configured")
    except ImportError:
        raise HTTPException(503, "Payments not configured")

    cfg = VIP_TIERS[body.tier]
    price_id = cfg['stripe_price_id']

    # Look up or use existing Stripe customer
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT stripe_customer_id, username FROM users WHERE user_id=$1", user_id
        )
    if not row:
        raise HTTPException(404, "User not found")
    customer_id = row['stripe_customer_id']

    checkout_kwargs = {
        "mode":         "subscription",
        "line_items":   [{"price": price_id, "quantity": 1}],
        "success_url":  "https://cs2casebot.xyz/vip/success?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url":   "https://cs2casebot.xyz/vip",
        "metadata":     {"user_id": str(user_id), "tier": body.tier},
        "subscription_data": {"metadata": {"user_id": str(user_id), "tier": body.tier}},
    }
    if customer_id:
        checkout_kwargs["customer"] = customer_id
    # No email for Discord users — omit key entirely to avoid Stripe rejecting None

    session = _stripe.checkout.Session.create(**checkout_kwargs)
    return {"checkout_url": session.url, "session_id": session.id}

@router.post("/api/vip/cancel")
async def vip_cancel(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT stripe_customer_id FROM users WHERE user_id=$1", user_id
        )

    customer_id = row["stripe_customer_id"] if row else None
    cancelled_stripe = False

    if customer_id:
        try:
            import stripe as _stripe
            _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
            if _stripe.api_key:
                subs = _stripe.Subscription.list(
                    customer=customer_id, status="active", limit=10
                )
                for sub in subs.auto_paging_iter():
                    _stripe.Subscription.modify(sub.id, cancel_at_period_end=True)
                cancelled_stripe = True
        except Exception as _e:
            logger.warning(f"Stripe cancel failed for user {user_id}: {_e}")

    # Leave vip_expires_at unchanged — the customer.subscription.deleted
    # webhook fires at period end and revokes access then. Only immediately
    # revoke if Stripe is not configured (no billing to worry about).
    if not cancelled_stripe:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET vip_expires_at = NOW() WHERE user_id = $1",
                user_id
            )

    _invalidate_vip_cache(user_id)
    msg = (
        "Subscription cancelled. VIP benefits remain until the end of your current billing period."
        if cancelled_stripe
        else "VIP cancelled. Access has been revoked immediately."
    )
    return {"success": True, "message": msg}

# ============================================================
# TICKET STORE
# ============================================================

# Real-money ticket purchases have been removed -- tickets are earned only
# (skill mini-games, quests, referrals, VIP daily grants), never bought
# directly, so there's no link between real money and this site's RNG loot.
# The old GET /api/tickets/packs + POST /api/tickets/buy endpoints (listing
# and a real Stripe one-time-payment checkout for TICKET_PACKS) have been
# deleted rather than just unlinked from the UI, so they can't be hit
# directly either.

# ============================================================
# STRIPE WEBHOOK
# ============================================================

@router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    try:
        import stripe as _stripe
        _stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')
        webhook_secret  = os.getenv('STRIPE_WEBHOOK_SECRET', '')
        if not _stripe.api_key:
            raise HTTPException(503, "Stripe not configured")
    except ImportError:
        raise HTTPException(503, "Stripe not configured")

    payload = await request.body()
    sig     = request.headers.get("stripe-signature", "")
    try:
        event = _stripe.Webhook.construct_event(payload, sig, webhook_secret)
    except Exception:
        raise HTTPException(400, "Invalid webhook signature")

    event_type = event["type"]
    obj        = event["data"]["object"]

    # ── Checkout completed ────────────────────────────────────
    if event_type == "checkout.session.completed":
        meta    = obj.get("metadata", {})
        user_id = int(meta.get("user_id", 0))
        if not user_id:
            return {"received": True}

        if obj.get("mode") == "subscription" or obj.get("subscription"):
            # VIP subscription granted
            tier = meta.get("tier", "")
            if tier not in VIP_TIERS:
                logger.warning(f"Unknown VIP tier in webhook: {tier}")
                return {"received": True}

            cfg = VIP_TIERS[tier]

            # Defense in depth: cross-check the amount actually charged against
            # this tier's configured price, so tampered/mismatched checkout
            # metadata can't grant a higher tier than was actually paid for.
            expected_cents = round(cfg['price'] * 100)
            charged_cents  = obj.get("amount_total")
            if charged_cents is not None and charged_cents != expected_cents:
                logger.error(
                    f"Stripe webhook price mismatch: user={user_id} tier={tier} "
                    f"expected={expected_cents}c charged={charged_cents}c "
                    f"session={obj.get('id')} — refusing to grant VIP tier, needs manual review"
                )
                return {"received": True}

            customer_id = obj.get("customer")
            sess_id     = obj.get("id", "")
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("""
                        UPDATE users
                        SET vip_tier = $1,
                            vip_expires_at = NOW() + INTERVAL '30 days',
                            vip_boost_multiplier = $2,
                            stripe_customer_id = COALESCE($3, stripe_customer_id)
                        WHERE user_id = $4
                    """, tier, cfg['boost'], customer_id, user_id)

                    # Upsert vip_perks row
                    await conn.execute("""
                        INSERT INTO vip_perks (user_id, private_rooms_enabled, tournament_access)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (user_id) DO UPDATE
                        SET private_rooms_enabled = $2,
                            tournament_access      = $3,
                            updated_at             = NOW()
                    """, user_id,
                        tier in ('gold', 'platinum'),   # private rooms
                        tier == 'platinum')              # tournaments

                    # Idempotency: only grant tickets if this checkout session hasn't
                    # been processed before (Stripe can replay webhooks).
                    already = await conn.fetchval("""
                        SELECT 1 FROM ticket_transactions
                        WHERE source = 'subscription'
                          AND metadata->>'session_id' = $1
                    """, sess_id) if sess_id else None
                    if not already:
                        await grant_tickets(
                            user_id, cfg['daily_tickets'], 'subscription',
                            {'tier': tier, 'event': 'new_subscription',
                             'session_id': sess_id}, conn=conn
                        )
            _invalidate_vip_cache(user_id)
            logger.info(f"✅ VIP {tier} granted to user {user_id}")

        else:
            # Real-money ticket purchases were removed (tickets are earned
            # only, never bought) -- nothing in the app creates a mode=
            # "payment" checkout session anymore, so this should be
            # unreachable. Log rather than silently fulfilling anything in
            # case a stale/replayed session ever arrives.
            logger.warning(
                f"Stripe webhook received a non-subscription checkout for user "
                f"{user_id} (session={obj.get('id', '')}) -- ticket purchases are "
                f"disabled, no fulfillment performed."
            )

    # ── Subscription invoice paid — extend VIP ────────────────
    elif event_type == "invoice.paid":
        sub_id      = obj.get("subscription")
        customer_id = obj.get("customer")
        invoice_id  = obj.get("id", "")
        if not (sub_id and customer_id and invoice_id):
            return {"received": True}

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Lock user row and check idempotency together so concurrent
                # Stripe retries cannot both pass and double-extend VIP.
                row = await conn.fetchrow(
                    "SELECT user_id, vip_tier FROM users WHERE stripe_customer_id=$1 FOR UPDATE",
                    customer_id
                )
                if row:
                    already = await conn.fetchval("""
                        SELECT 1 FROM ticket_transactions
                        WHERE source='subscription'
                          AND metadata->>'invoice_id' = $1
                    """, invoice_id)
                    if not already:
                        cfg = VIP_TIERS.get(row['vip_tier'], {})
                        await conn.execute("""
                            UPDATE users
                            SET vip_expires_at = GREATEST(vip_expires_at, NOW()) + INTERVAL '30 days'
                            WHERE user_id = $1
                        """, row['user_id'])
                        # Award monthly daily tickets for renewal
                        daily = cfg.get('daily_tickets', 0)
                        if daily:
                            await grant_tickets(
                                row['user_id'], daily, 'subscription',
                                {'tier': row['vip_tier'], 'event': 'renewal',
                                 'invoice_id': invoice_id}, conn=conn
                            )
                        _invalidate_vip_cache(row['user_id'])
                        logger.info(f"✅ VIP extended for user {row['user_id']}")

    # ── Subscription cancelled / payment failed ───────────────
    elif event_type in ("customer.subscription.deleted", "invoice.payment_failed"):
        customer_id = obj.get("customer")
        if customer_id:
            pool = await get_db()
            async with pool.acquire() as conn:
                if event_type == "customer.subscription.deleted":
                    row = await conn.fetchrow(
                        "SELECT user_id FROM users WHERE stripe_customer_id=$1", customer_id
                    )
                    await conn.execute("""
                        UPDATE users SET vip_expires_at = NOW()
                        WHERE stripe_customer_id = $1
                    """, customer_id)
                    if row:
                        _invalidate_vip_cache(row['user_id'])
                else:
                    logger.warning(f"Payment failed for customer {customer_id}")

    return {"received": True}

# ============================================================
# PREMIUM CASE BATCH
# ============================================================

def _get_top_cases(n: int = 5):
    """Return the n most expensive cases from CASES dict."""
    return sorted(
        [(cid, c) for cid, c in CASES.items()],
        key=lambda x: float(x[1].get('price', 0)),
        reverse=True
    )[:n]

@router.post("/api/vip/premium-batch-open")
async def premium_batch_open(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()

    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)

            # Check and deduct ticket
            ok = await deduct_ticket(
                user_id, 'spend_case',
                {'action': 'premium_batch_open'}, conn=conn
            )
            if not ok:
                raise HTTPException(400, "Insufficient tickets")

            # Get top 5 cases
            top_cases = _get_top_cases(5)
            items = []
            total_cost = 0.0

            for case_id, case_data in top_cases:
                item = get_random_item(case_id)
                if not item:
                    continue
                price = float(case_data.get('price', 0))
                total_cost += price

                # Deduct case cost
                if not await deduct_balance(user_id, price, conn):
                    raise HTTPException(400, "Insufficient balance for case costs")

                # Store in inventory
                img_file = item.get('image_filename')
                img_url  = f"/static/images/skins/{img_file}" if img_file else None
                row = await conn.fetchrow("""
                    INSERT INTO inventory
                        (user_id, item_name, item_type, rarity, price, condition,
                         is_stattrak, status, case_id, float_value, image_url)
                    VALUES ($1,$2,'weapon',$3,$4,$5,$6,'kept',$7,$8,$9)
                    RETURNING id
                """, user_id, item['name'], item['rarity'], item['price'],
                    item['condition'], item['is_stattrak'], case_id,
                    item['float'], img_url)
                item['id']       = row['id']
                item['case_id']  = case_id
                item['case_name']= case_data.get('name', case_id)
                item['image_url']= img_url
                items.append(item)

            # Update stats
            await conn.execute(
                "UPDATE users SET total_opens = total_opens + $1, total_premium_opens = total_premium_opens + $2 WHERE user_id = $3",
                len(items), len(items), user_id
            )

    return {
        "success":    True,
        "items":      items,
        "total_cost": round(total_cost, 2),
        "cases_used": [c[1].get('name', c[0]) for c in top_cases],
    }

# ============================================================
# TICKET CASE
# ============================================================

# Drop rates for the Ticket Case — no Blues, skewed toward high rarity
_TICKET_CASE_RATES = [
    ('Purple', 0.42),
    ('Pink',   0.35),
    ('Red',    0.18),
    ('Gold',   0.05),
]

def _get_ticket_case_item() -> Optional[dict]:
    """Roll one item from the Ticket Case pool (Purple–Gold only)."""
    r = secure_random()
    cumulative = 0.0
    chosen_rarity = 'Purple'
    for rarity, chance in _TICKET_CASE_RATES:
        cumulative += chance
        if r <= cumulative:
            chosen_rarity = rarity
            break

    if chosen_rarity == 'Gold':
        pool = shared.GOLD_ITEMS_POOL or shared.ALL_ITEMS_BY_RARITY.get('Red', [])
    else:
        pool = shared.ALL_ITEMS_BY_RARITY.get(chosen_rarity, [])
    if not pool:
        pool = shared.ALL_ITEMS_BY_RARITY.get('Purple', [])
    if not pool:
        return None

    skin = secure_choice(pool)
    fmin = skin.get('float_min', 0.0)
    fmax = skin.get('float_max', 1.0)
    if fmax < fmin:
        fmin, fmax = fmax, fmin
    float_value = fmin + secure_random() * (fmax - fmin)

    condition   = get_skin_condition(float_value)
    # Real CS2 gloves never have StatTrak -- only weapons and knives do.
    is_stattrak = (not skin.get('is_glove')) and secure_random() < 0.1
    price       = calculate_item_value(chosen_rarity, condition, None, is_stattrak)

    full_name    = f"{skin['weapon_type']} | {skin['skin_name']}"
    name         = f"StatTrak™ {full_name}" if is_stattrak else full_name
    display_name = f"{RARITY_EMOJIS.get(chosen_rarity, '')} {name}"

    import os as _os
    skin_img       = skin.get('skin_image')
    image_filename = _os.path.basename(skin_img) if skin_img else None

    return {
        'name':          name,
        'display_name':  display_name,
        'rarity':        chosen_rarity,
        'rarity_emoji':  RARITY_EMOJIS.get(chosen_rarity, ''),
        'condition':     condition,
        'float':         float_value,
        'price':         price,
        'is_stattrak':   is_stattrak,
        'tier':          None,
        'image_filename': image_filename,
    }

@router.post("/api/vip/ticket-case-open")
async def ticket_case_open(request: Request):
    """Spend 1 ticket to open the Ticket Case (Purple–Gold only)."""
    user_id = await require_auth(request)
    pool = await get_db()

    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_user_exists(user_id, conn=conn)

            ok = await deduct_ticket(
                user_id, 'spend_case',
                {'action': 'ticket_case_open'}, conn=conn
            )
            if not ok:
                raise HTTPException(400, "Insufficient tickets")

            item = _get_ticket_case_item()
            if not item:
                raise HTTPException(500, "Failed to generate item")

            img_file = item.get('image_filename')
            img_url  = f"/static/images/skins/{img_file}" if img_file else None

            row = await conn.fetchrow("""
                INSERT INTO inventory
                    (user_id, item_name, item_type, rarity, price, condition,
                     is_stattrak, status, case_id, float_value, image_url)
                VALUES ($1,$2,'weapon',$3,$4,$5,$6,'kept','ticket_case',$7,$8)
                RETURNING id
            """, user_id, item['name'], item['rarity'], item['price'],
                item['condition'], item['is_stattrak'], item['float'], img_url)

            item['id']        = row['id']
            item['image_url'] = img_url

            if item['rarity'] == 'Gold':
                await conn.execute(
                    "UPDATE users SET total_golds = total_golds + 1 WHERE user_id=$1",
                    user_id
                )

            await conn.execute(
                "UPDATE users SET total_opens = total_opens + 1,"
                " total_premium_opens = total_premium_opens + 1 WHERE user_id=$1",
                user_id
            )

    return {"success": True, "item": item}

# ============================================================
# TICKET INFO ENDPOINTS
# ============================================================

@router.get("/api/vip/perks")
async def vip_perks_endpoint(request: Request):
    """Returns this user's vip_perks row."""
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM vip_perks WHERE user_id=$1", user_id
        )
    if not row:
        return {"private_rooms_enabled": False, "tournament_access": False,
                "profile_border": "none", "avatar_frame": "none"}
    return convert_decimals(dict(row))

@router.get("/api/tickets/balance")
async def ticket_balance_endpoint(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        tickets = await conn.fetchval(
            "SELECT tickets FROM users WHERE user_id=$1", user_id
        )
    return {"tickets": int(tickets or 0)}

@router.get("/api/tickets/history")
async def ticket_history(request: Request, limit: int = 20):
    user_id = await require_auth(request)
    limit = max(1, min(100, limit))  # Bug 168 fix: clamp to prevent table-scan DoS
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT amount, source, metadata, created_at
            FROM ticket_transactions
            WHERE user_id=$1
            ORDER BY created_at DESC LIMIT $2
        """, user_id, limit)
    return {"history": [convert_decimals(dict(r)) for r in rows]}
