# ============================================================
# routes/site.py
# CS2CaseBot | Public site-wide notices
#
# Read-side companions to the admin-only endpoints in routes/admin.py:
# announcements, giveaways (with entry), and active fire sales.
# ============================================================

from datetime import datetime

from fastapi import APIRouter, Request, HTTPException

import asyncpg

from shared import logger, get_db, get_user_id_from_session, convert_decimals

router = APIRouter(prefix="/api", tags=["site"])

# ============================================================
# ANNOUNCEMENTS
# ============================================================

@router.get("/announcements")
async def public_announcements():
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, message, type, created_at
            FROM announcements ORDER BY created_at DESC LIMIT 5
        """)
    return {"announcements": [convert_decimals(dict(r)) for r in rows]}

# ============================================================
# FIRE SALES
# ============================================================

@router.get("/fire-sales")
async def public_fire_sales():
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name, discount_percent, case_type, expires_at
            FROM fire_sales WHERE expires_at > NOW()
            ORDER BY created_at DESC
        """)
    return {"fire_sales": [convert_decimals(dict(r)) for r in rows]}

# ============================================================
# GIVEAWAYS
# ============================================================

@router.get("/giveaways")
async def public_giveaways(request: Request):
    user_id = await get_user_id_from_session(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT g.id, g.prize_amount, g.winner_count, g.end_time,
                   g.required_level, g.required_opens,
                   COUNT(e.id) AS entries_count
            FROM giveaways g
            LEFT JOIN giveaway_entries e ON e.giveaway_id = g.id
            WHERE g.status = 'active' AND g.end_time > NOW()
            GROUP BY g.id
            ORDER BY g.end_time ASC
        """)
        entered_ids = set()
        if user_id and rows:
            gids = [r["id"] for r in rows]
            entered_rows = await conn.fetch(
                "SELECT giveaway_id FROM giveaway_entries "
                "WHERE user_id=$1 AND giveaway_id = ANY($2::int[])",
                user_id, gids
            )
            entered_ids = {r["giveaway_id"] for r in entered_rows}

    giveaways = []
    for r in rows:
        d = convert_decimals(dict(r))
        d["entries_count"] = int(d["entries_count"])
        d["entered"] = r["id"] in entered_ids
        giveaways.append(d)
    return {"giveaways": giveaways}

@router.post("/giveaways/{giveaway_id}/enter")
async def enter_giveaway(giveaway_id: int, request: Request):
    user_id = await get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            g = await conn.fetchrow(
                "SELECT * FROM giveaways WHERE id=$1 FOR UPDATE", giveaway_id
            )
            if not g:
                raise HTTPException(404, "Giveaway not found")
            if g["status"] != "active" or g["end_time"] <= datetime.utcnow():
                raise HTTPException(400, "This giveaway has ended")

            user = await conn.fetchrow(
                "SELECT level, total_opens FROM users WHERE user_id=$1", user_id
            )
            if not user:
                raise HTTPException(404, "User not found")
            if (g["required_level"] or 0) > (user["level"] or 0):
                raise HTTPException(400, f"Requires level {g['required_level']}")
            if (g["required_opens"] or 0) > (user["total_opens"] or 0):
                raise HTTPException(400, f"Requires {g['required_opens']} case opens")

            try:
                await conn.execute(
                    "INSERT INTO giveaway_entries (giveaway_id, user_id) VALUES ($1,$2)",
                    giveaway_id, user_id
                )
            except asyncpg.exceptions.UniqueViolationError:
                raise HTTPException(400, "You've already entered this giveaway")

    return {"success": True}
