from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from shared import get_db, require_auth

router = APIRouter()

MAX_LOADOUTS_PER_USER = 10


class LoadoutNameRequest(BaseModel):
    name: str


def _clean_name(raw: str) -> str:
    name = (raw or "").strip()[:40]
    if not name:
        raise HTTPException(400, "Name is required")
    return name


async def _recompute_in_loadout_flags(conn, user_id: int, active_loadout_id):
    """Keeps inventory.in_loadout in sync with membership in the given
    active loadout (or clears it entirely if active_loadout_id is None) --
    every other reader of that column (GET /api/loadout, the friend-loadout
    view, item-picker exclusions) only ever cares about the ACTIVE loadout."""
    await conn.execute("UPDATE inventory SET in_loadout=FALSE WHERE user_id=$1", user_id)
    if active_loadout_id is not None:
        await conn.execute("""
            UPDATE inventory SET in_loadout=TRUE
            WHERE user_id=$1 AND id IN (SELECT inventory_id FROM loadout_items WHERE loadout_id=$2)
        """, user_id, active_loadout_id)


@router.get("/api/loadouts")
async def list_loadouts(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT l.id, l.name, l.is_active, l.created_at,
                   COUNT(li.inventory_id) AS item_count
            FROM loadouts l
            LEFT JOIN loadout_items li ON li.loadout_id = l.id
            WHERE l.user_id = $1
            GROUP BY l.id
            ORDER BY l.created_at ASC
        """, user_id)
    return {"loadouts": [
        {
            "id": r["id"],
            "name": r["name"],
            "is_active": r["is_active"],
            "item_count": r["item_count"],
        } for r in rows
    ]}


@router.post("/api/loadouts")
async def create_loadout(body: LoadoutNameRequest, request: Request):
    user_id = await require_auth(request)
    name = _clean_name(body.name)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM loadouts WHERE user_id=$1", user_id
            )
            if count >= MAX_LOADOUTS_PER_USER:
                raise HTTPException(400, f"Maximum of {MAX_LOADOUTS_PER_USER} loadouts reached")
            make_active = count == 0
            loadout_id = await conn.fetchval(
                "INSERT INTO loadouts (user_id, name, is_active) VALUES ($1, $2, $3) RETURNING id",
                user_id, name, make_active
            )
            if make_active:
                await _recompute_in_loadout_flags(conn, user_id, loadout_id)
    return {"success": True, "id": loadout_id, "name": name, "is_active": make_active}


@router.patch("/api/loadouts/{loadout_id}")
async def rename_loadout(loadout_id: int, body: LoadoutNameRequest, request: Request):
    user_id = await require_auth(request)
    name = _clean_name(body.name)
    pool = await get_db()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE loadouts SET name=$1 WHERE id=$2 AND user_id=$3", name, loadout_id, user_id
        )
        if result == "UPDATE 0":
            raise HTTPException(404, "Loadout not found")
    return {"success": True, "name": name}


@router.delete("/api/loadouts/{loadout_id}")
async def delete_loadout(loadout_id: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, is_active FROM loadouts WHERE id=$1 AND user_id=$2 FOR UPDATE",
                loadout_id, user_id
            )
            if not row:
                raise HTTPException(404, "Loadout not found")
            was_active = row["is_active"]
            await conn.execute("DELETE FROM loadouts WHERE id=$1", loadout_id)  # cascades loadout_items
            if was_active:
                next_row = await conn.fetchrow(
                    "SELECT id FROM loadouts WHERE user_id=$1 ORDER BY created_at ASC LIMIT 1",
                    user_id
                )
                if next_row:
                    await conn.execute(
                        "UPDATE loadouts SET is_active=TRUE WHERE id=$1", next_row["id"]
                    )
                    await _recompute_in_loadout_flags(conn, user_id, next_row["id"])
                else:
                    await _recompute_in_loadout_flags(conn, user_id, None)
    return {"success": True}


@router.post("/api/loadouts/{loadout_id}/activate")
async def activate_loadout(loadout_id: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id FROM loadouts WHERE id=$1 AND user_id=$2 FOR UPDATE",
                loadout_id, user_id
            )
            if not row:
                raise HTTPException(404, "Loadout not found")
            await conn.execute("UPDATE loadouts SET is_active=FALSE WHERE user_id=$1", user_id)
            await conn.execute("UPDATE loadouts SET is_active=TRUE WHERE id=$1", loadout_id)
            await _recompute_in_loadout_flags(conn, user_id, loadout_id)
    return {"success": True}


@router.post("/api/loadouts/{loadout_id}/items/{item_id}")
async def add_item_to_loadout(loadout_id: int, item_id: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            loadout = await conn.fetchrow(
                "SELECT id, is_active FROM loadouts WHERE id=$1 AND user_id=$2 FOR UPDATE",
                loadout_id, user_id
            )
            if not loadout:
                raise HTTPException(404, "Loadout not found")
            item = await conn.fetchrow(
                "SELECT id FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept'",
                item_id, user_id
            )
            if not item:
                raise HTTPException(404, "Item not found")
            await conn.execute(
                "INSERT INTO loadout_items (loadout_id, inventory_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                loadout_id, item_id
            )
            if loadout["is_active"]:
                await conn.execute("UPDATE inventory SET in_loadout=TRUE WHERE id=$1", item_id)
    return {"success": True}


@router.delete("/api/loadouts/{loadout_id}/items/{item_id}")
async def remove_item_from_loadout(loadout_id: int, item_id: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            loadout = await conn.fetchrow(
                "SELECT id, is_active FROM loadouts WHERE id=$1 AND user_id=$2 FOR UPDATE",
                loadout_id, user_id
            )
            if not loadout:
                raise HTTPException(404, "Loadout not found")
            await conn.execute(
                "DELETE FROM loadout_items WHERE loadout_id=$1 AND inventory_id=$2",
                loadout_id, item_id
            )
            if loadout["is_active"]:
                await conn.execute(
                    "UPDATE inventory SET in_loadout=FALSE WHERE id=$1 AND user_id=$2",
                    item_id, user_id
                )
    return {"success": True}
