# ============================================================
# routes/item_house_jackpot.py
# CS2CaseBot | Item vs House Jackpot (solo/PvE)
#
# Stake a real inventory item against the house. Win chance is
# 0.5 * (1 - HOUSE_EDGE) so the house keeps its usual cut; win
# doubles the stake (item returned + a cash bonus equal to its
# value), loss consumes the item. Stateless per-request, like
# open_case -- no room/lock/WebSocket needed since there's only
# one player and one instantaneous resolve.
# ============================================================

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from shared import (
    logger, get_db, require_auth, ensure_user_exists,
    convert_decimals, secure_random, credit_win,
    check_rate_limit, RATE_WRITE, HOUSE_EDGE,
    relax_inventory_fk_to_set_null,
)

router = APIRouter(prefix="/api/games/jackpot/house", tags=["item-house-jackpot"])

MIN_STAKE_VALUE = 0.50
WIN_PROBABILITY = 0.5 * (1 - HOUSE_EDGE)


async def init_house_wager_table():
    pool = await get_db()
    async with pool.acquire() as conn:
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
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_item_house_wagers_user ON item_house_wagers(user_id)")
        await relax_inventory_fk_to_set_null(conn, 'item_house_wagers')
    logger.info("✅ Item House Jackpot table ready")


class WagerRequest(BaseModel):
    inventory_id: int


@router.post("/wager")
async def house_wager(req: WagerRequest, request: Request):
    await check_rate_limit(request, RATE_WRITE)
    user_id = await require_auth(request)
    await ensure_user_exists(user_id)

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Same atomic ownership+status+min-value guard as Item Jackpot's
            # join_pot -- a concurrent stake attempt on the same item can
            # never succeed twice.
            item = await conn.fetchrow("""
                UPDATE inventory SET status='staked'
                WHERE id=$1 AND user_id=$2 AND status='kept'
                  AND price >= $3 AND in_loadout = FALSE AND protected = FALSE
                RETURNING item_name, rarity, price, condition, is_stattrak, float_value, image_url
            """, req.inventory_id, user_id, MIN_STAKE_VALUE)
            if not item:
                raise HTTPException(
                    400,
                    f"Item not available to wager (must be a kept, unequipped, unprotected item worth at least ${MIN_STAKE_VALUE:.2f})"
                )

            value = float(item['price'])
            won = secure_random() < WIN_PROBABILITY
            payout = 0.0

            if won:
                await conn.execute(
                    "UPDATE inventory SET status='kept' WHERE id=$1",
                    req.inventory_id
                )
                payout = await credit_win(user_id, value, conn)
            else:
                # House keeps the item -- consumed, same status vocabulary sell_item uses.
                await conn.execute(
                    "UPDATE inventory SET status='sold' WHERE id=$1",
                    req.inventory_id
                )

            await conn.execute("""
                INSERT INTO item_house_wagers
                    (user_id, inventory_id, item_name, rarity, condition, is_stattrak,
                     float_value, image_url, value, won, payout_value)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """, user_id, req.inventory_id, item['item_name'], item['rarity'], item['condition'],
                item['is_stattrak'], item['float_value'], item['image_url'], value, won, payout)

    result = {
        "success": True,
        "won": won,
        "item": {
            'item_name': item['item_name'],
            'rarity': item['rarity'],
            'condition': item['condition'],
            'is_stattrak': item['is_stattrak'],
            'float_value': item['float_value'],
            'image_url': item['image_url'],
            'value': value,
        },
        "payout": payout,
    }
    return convert_decimals(result)


@router.get("/history")
async def house_wager_history(request: Request, limit: int = 20):
    user_id = await require_auth(request)
    limit = max(1, min(limit, 50))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT item_name, rarity, condition, value, won, payout_value, created_at
            FROM item_house_wagers
            WHERE user_id=$1
            ORDER BY created_at DESC
            LIMIT $2
        """, user_id, limit)
    result = {"history": [
        {
            'item_name': r['item_name'], 'rarity': r['rarity'], 'condition': r['condition'],
            'value': r['value'], 'won': r['won'], 'payout_value': r['payout_value'],
            'created_at': r['created_at'].isoformat() if r['created_at'] else None,
        }
        for r in rows
    ]}
    return convert_decimals(result)
