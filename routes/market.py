# ============================================================
# routes/market.py
# CS2CaseBot | P2P Marketplace — list, browse, buy, cancel
# ============================================================

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
from asyncpg.exceptions import UniqueViolationError

from shared import get_db, get_user_id_from_session, convert_decimals, logger, check_rate_limit, RATE_MARKET

router = APIRouter(prefix="/api/market", tags=["market"])

HOUSE_FEE = 0.05  # 5% taken from sale price


class ListingRequest(BaseModel):
    item_id: int
    price: float


# ============================================================
# TABLE INIT
# ============================================================

async def init_market_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS market_listings (
                id          SERIAL PRIMARY KEY,
                seller_id   BIGINT  REFERENCES users(user_id)  ON DELETE CASCADE,
                item_id     INTEGER REFERENCES inventory(id)   ON DELETE CASCADE,
                item_name   TEXT    NOT NULL,
                rarity      TEXT,
                condition   TEXT,
                is_stattrak BOOLEAN DEFAULT FALSE,
                float_value DECIMAL(10,4),
                image_url   TEXT,
                price       DECIMAL(15,2) NOT NULL,
                created_at  TIMESTAMP DEFAULT NOW(),
                status      TEXT DEFAULT 'active'
                            CHECK (status IN ('active','sold','cancelled'))
            )
        """)
        try:
            await conn.execute("ALTER TABLE market_listings ADD COLUMN IF NOT EXISTS image_url TEXT")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE market_listings ADD COLUMN IF NOT EXISTS applied_stickers JSONB DEFAULT '[]'::jsonb")
        except Exception:
            pass
        for sql in [
            "CREATE INDEX IF NOT EXISTS idx_market_active ON market_listings(created_at DESC) WHERE status='active'",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_market_item ON market_listings(item_id) WHERE status='active'",
        ]:
            try:
                await conn.execute(sql)
            except Exception:
                pass


# ============================================================
# ENDPOINTS
# ============================================================

@router.get("/listings")
async def get_listings(
    rarity: Optional[str] = None,
    search: Optional[str] = None,
    sort: str = "newest",
    limit: int = 40,
    offset: int = 0,
):
    """Browse active market listings."""
    limit  = max(1, min(limit, 100))
    offset = max(0, offset)

    order = {
        "newest":     "ml.created_at DESC",
        "price_asc":  "ml.price ASC",
        "price_desc": "ml.price DESC",
    }.get(sort, "ml.created_at DESC")

    pool = await get_db()
    async with pool.acquire() as conn:
        filters = "ml.status = 'active'"
        args: list = []
        if rarity:
            args.append(rarity)
            filters += f" AND ml.rarity = ${len(args)}"
        if search:
            args.append(f"%{search}%")
            filters += f" AND ml.item_name ILIKE ${len(args)}"

        args += [limit, offset]
        rows = await conn.fetch(f"""
            SELECT ml.id, ml.seller_id, ml.item_name, ml.rarity, ml.condition,
                   ml.is_stattrak, ml.float_value, ml.price, ml.created_at,
                   ml.image_url, ml.applied_stickers, u.username AS seller_name
            FROM market_listings ml
            JOIN users u ON ml.seller_id = u.user_id
            WHERE {filters}
            ORDER BY {order}
            LIMIT ${len(args)-1} OFFSET ${len(args)}
        """, *args)

        total = await conn.fetchval(f"""
            SELECT COUNT(*) FROM market_listings ml WHERE {filters}
        """, *args[:-2])

    return {
        "listings": [convert_decimals(dict(r)) for r in rows],
        "total":    total,
        "limit":    limit,
        "offset":   offset,
    }


@router.post("/list")
async def list_item(request: Request, req: ListingRequest):
    """List an inventory item for sale."""
    await check_rate_limit(request, RATE_MARKET)
    user_id = await get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")

    price = round(req.price, 2)
    if price < 0.01:
        raise HTTPException(400, "Minimum listing price is $0.01")
    if price > 500_000:
        raise HTTPException(400, "Maximum listing price is $500,000")

    pool = await get_db()
    async with pool.acquire() as conn:
        # Verify item belongs to user and is not already listed
        item = await conn.fetchrow("""
            SELECT id, item_name, rarity, condition, is_stattrak, float_value, status, item_type, image_url, protected, applied_stickers
            FROM inventory
            WHERE id = $1 AND user_id = $2
        """, req.item_id, user_id)

        if not item:
            raise HTTPException(404, "Item not found")
        if item['status'] != 'kept':
            raise HTTPException(400, "Item is not available for listing")
        if item['protected']:
            raise HTTPException(400, "This item is protected — unprotect it first to list it")
        # Check not already listed
        existing = await conn.fetchval(
            "SELECT 1 FROM market_listings WHERE item_id = $1 AND status = 'active'",
            req.item_id
        )
        if existing:
            raise HTTPException(400, "Item is already listed")

        try:
            async with conn.transaction():
                # Mark item as listed in inventory
                await conn.execute(
                    "UPDATE inventory SET status = 'listed' WHERE id = $1",
                    req.item_id
                )
                listing_id = await conn.fetchval("""
                    INSERT INTO market_listings
                        (seller_id, item_id, item_name, rarity, condition,
                         is_stattrak, float_value, price, image_url, applied_stickers)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    RETURNING id
                """, user_id, req.item_id, item['item_name'], item['rarity'],
                    item['condition'], item['is_stattrak'], item['float_value'], price,
                    item['image_url'], item['applied_stickers'])
        except UniqueViolationError:
            raise HTTPException(400, "Item is already listed")

    return {"success": True, "listing_id": listing_id, "price": price}


@router.post("/buy/{listing_id}")
async def buy_listing(request: Request, listing_id: int):
    """Purchase a market listing."""
    await check_rate_limit(request, RATE_MARKET)
    user_id = await get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")

    pool = await get_db()
    async with pool.acquire() as conn:
        listing = await conn.fetchrow("""
            SELECT ml.id, ml.seller_id, ml.item_id, ml.item_name,
                   ml.rarity, ml.condition, ml.is_stattrak, ml.float_value,
                   ml.price, ml.status
            FROM market_listings ml
            WHERE ml.id = $1
        """, listing_id)

        if not listing:
            raise HTTPException(404, "Listing not found")
        if listing['status'] != 'active':
            raise HTTPException(400, "This listing is no longer available")
        if listing['seller_id'] == user_id:
            raise HTTPException(400, "You cannot buy your own listing")

        price      = float(listing['price'])
        seller_cut = round(price * (1 - HOUSE_FEE), 2)

        async with conn.transaction():
            # Lock & re-check listing is still active (race condition guard) before touching balances
            still_active = await conn.fetchval(
                "SELECT 1 FROM market_listings WHERE id = $1 AND status = 'active' FOR UPDATE",
                listing_id
            )
            if not still_active:
                raise HTTPException(400, "This listing was just purchased by someone else")

            # Check & deduct buyer balance
            deducted = await conn.fetchval("""
                UPDATE users SET balance = balance - $1
                WHERE user_id = $2 AND balance >= $1
                RETURNING user_id
            """, price, user_id)
            if not deducted:
                raise HTTPException(400, "Insufficient balance")

            # Pay seller
            await conn.execute(
                "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                seller_cut, listing['seller_id']
            )

            # Transfer item to buyer -- stickers travel WITH the item now that
            # market listings visibly show the sticker'd look; in_loadout is
            # still reset since a showcase preference doesn't carry to a new owner.
            await conn.execute(
                "UPDATE inventory SET user_id = $1, status = 'kept', in_loadout = FALSE WHERE id = $2",
                user_id, listing['item_id']
            )

            # Mark listing sold
            await conn.execute(
                "UPDATE market_listings SET status = 'sold' WHERE id = $1",
                listing_id
            )

    return {
        "success":     True,
        "item_name":   listing['item_name'],
        "price":       price,
        "seller_cut":  seller_cut,
        "house_fee":   round(price * HOUSE_FEE, 2),
    }


@router.delete("/cancel/{listing_id}")
async def cancel_listing(request: Request, listing_id: int):
    """Cancel your own listing and return item to inventory."""
    user_id = await get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")

    pool = await get_db()
    async with pool.acquire() as conn:
        listing = await conn.fetchrow(
            "SELECT seller_id, item_id, status FROM market_listings WHERE id = $1",
            listing_id
        )
        if not listing:
            raise HTTPException(404, "Listing not found")
        if listing['seller_id'] != user_id:
            raise HTTPException(403, "Not your listing")
        if listing['status'] != 'active':
            raise HTTPException(400, "Listing is not active")

        async with conn.transaction():
            await conn.execute(
                "UPDATE market_listings SET status = 'cancelled' WHERE id = $1",
                listing_id
            )
            await conn.execute(
                "UPDATE inventory SET status = 'kept' WHERE id = $1",
                listing['item_id']
            )

    return {"success": True}


@router.get("/my-listings")
async def my_listings(request: Request):
    """Return the authenticated user's listings (all statuses)."""
    user_id = await get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")

    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, item_name, rarity, condition, is_stattrak,
                   float_value, price, status, created_at
            FROM market_listings
            WHERE seller_id = $1
            ORDER BY created_at DESC
            LIMIT 50
        """, user_id)

    return [convert_decimals(dict(r)) for r in rows]
