import asyncio
from datetime import datetime, timezone
from typing import Optional
import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from shared import get_db, require_auth, logger, secure_random
from shared import check_rate_limit, RATE_WRITE
from shared import convert_decimals, get_skin_image_filename
from routes.premium import deduct_ticket, grant_tickets

router = APIRouter()

# ── Generalized game challenges (Session 4) ─────────────────────
# Ticket-tolled private-room challenges for 7 real PvP games, on top of
# the original plain ticket-coinflip pvp_challenges above. Each game
# contributes a create_private_room() hook (imported lazily per-dispatch
# below, matching server.py's convention for cross-route dependencies)
# that this generalized layer calls once every invitee has responded.
CHALLENGE_TICKET_TOLL = 2   # tickets each way: 2 to send, 2 to accept, per invitee

GAME_MIN_PLAYERS = {
    'dice_duel': 2, 'weapon_duel': 2, 'reaction_duel': 2, 'case_draft_duel': 2,
    'item_wager_duel': 2, 'item_trade_up_duel': 2, 'case_battles': 2,
    'ladder_race': 2, 'mines_race': 2,
    'sync_slots': 2, 'koth_ladder': 2, 'battle_royale_mines': 2, 'speed_case_race': 2,
    'live_blackjack': 2, 'live_roulette': 2, 'live_keno': 2,
}
GAME_MAX_PLAYERS = {
    'dice_duel': 2, 'weapon_duel': 2, 'reaction_duel': 2, 'case_draft_duel': 2,
    'item_wager_duel': 2, 'item_trade_up_duel': 2, 'case_battles': 4,
    'ladder_race': 2, 'mines_race': 2,
    'sync_slots': 2, 'koth_ladder': 2, 'battle_royale_mines': 2, 'speed_case_race': 2,
    'live_blackjack': 2, 'live_roulette': 2, 'live_keno': 2,
}


def _is_online(last_seen) -> bool:
    if not last_seen:
        return False
    ts = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() < 300


def _avatar(row) -> str:
    if (row.get("primary_provider") or "discord") == "google":
        return row.get("google_avatar_url") or row.get("avatar_url") or ""
    return row.get("avatar_url") or ""


async def init_friend_challenges_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS friend_challenges (
                id             SERIAL PRIMARY KEY,
                challenger_id  BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                game_type      TEXT NOT NULL CHECK (game_type IN (
                               'dice_duel','weapon_duel','reaction_duel','case_draft_duel',
                               'item_wager_duel','item_trade_up_duel','case_battles')),
                game_params    JSONB NOT NULL,
                game_room_id   INTEGER,
                status         TEXT DEFAULT 'pending' CHECK (status IN ('pending','resolving','started','cancelled','expired')),
                created_at     TIMESTAMPTZ DEFAULT NOW(),
                expires_at     TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '15 minutes')
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS friend_challenge_invitees (
                id             SERIAL PRIMARY KEY,
                challenge_id   INTEGER NOT NULL REFERENCES friend_challenges(id) ON DELETE CASCADE,
                user_id        BIGINT  NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                status         TEXT DEFAULT 'pending' CHECK (status IN ('pending','accepted','declined')),
                responded_at   TIMESTAMPTZ,
                UNIQUE(challenge_id, user_id)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_friend_challenges_status ON friend_challenges(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_friend_challenge_invitees_challenge ON friend_challenge_invitees(challenge_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_friend_challenge_invitees_user ON friend_challenge_invitees(user_id)")
    logger.info("✅ Friend challenge tables ready")


async def _create_game_room(game_type: str, participant_user_ids: list, game_params: dict) -> int:
    """Dispatches to the right game module's create_private_room(). Lazy
    imports (matching server.py's convention for cross-route dependencies)
    avoid any import-order issues at module load time."""
    if game_type == 'dice_duel':
        from routes.dice_duel import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['stake']))
    elif game_type == 'weapon_duel':
        from routes.weapon_duel import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['stake']))
    elif game_type == 'reaction_duel':
        from routes.reaction_duel import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['stake']))
    elif game_type == 'case_draft_duel':
        from routes.case_draft_duel import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['entry_fee']))
    elif game_type == 'item_wager_duel':
        from routes.item_wager_duel import create_private_room
        pairs = [(uid, int(game_params['inventory_ids'][str(uid)])) for uid in participant_user_ids]
        return await create_private_room(pairs)
    elif game_type == 'item_trade_up_duel':
        from routes.item_trade_up_duel import create_private_room
        pairs = [(uid, int(game_params['inventory_ids'][str(uid)])) for uid in participant_user_ids]
        return await create_private_room(pairs)
    elif game_type == 'case_battles':
        from routes.case_battles import create_private_room
        return await create_private_room(
            participant_user_ids, float(game_params['fee']),
            rounds=int(game_params.get('rounds', 3)),
            win_condition=game_params.get('win_condition', 'total_value'),
        )
    elif game_type == 'ladder_race':
        from routes.ladder_race import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['stake']))
    elif game_type == 'mines_race':
        from routes.mines_race import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['stake']))
    elif game_type == 'sync_slots':
        from routes.sync_slots import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['stake']))
    elif game_type == 'koth_ladder':
        from routes.koth_ladder import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['stake']))
    elif game_type == 'battle_royale_mines':
        from routes.battle_royale_mines import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['stake']))
    elif game_type == 'speed_case_race':
        from routes.speed_case_race import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['stake']))
    elif game_type == 'live_blackjack':
        from routes.live_blackjack import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['stake']))
    elif game_type == 'live_roulette':
        from routes.live_roulette import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['stake']))
    elif game_type == 'live_keno':
        from routes.live_keno import create_private_room
        return await create_private_room(participant_user_ids, float(game_params['stake']))
    raise HTTPException(400, f"Unsupported game_type: {game_type}")


async def _resolve_challenge_if_ready(challenge_id: int, conn):
    """Phase 1 -- safe to run inside the caller's existing transaction.
    Once every invitee has responded: if under minimum, refund everyone's
    ticket toll and cancel right here (same connection, no risk). If
    ready to start, atomically claim it (status='resolving', so a
    concurrent accept/decline can't also try to start it) and return a
    dict describing the pending start -- but does NOT create the game
    room itself. That needs a fresh connection/transaction, and doing it
    while still holding this transaction's row lock on the same users
    (from the ticket-toll deduction moments earlier in the same call)
    would deadlock against the game's own cash/ticket staking on those
    same rows. Returns None if there's nothing to do (still waiting, or
    already handled inline as a cancel). The caller must call
    _finish_challenge_start() with the returned dict AFTER this
    transaction commits."""
    ch = await conn.fetchrow("SELECT * FROM friend_challenges WHERE id=$1 FOR UPDATE", challenge_id)
    if not ch or ch['status'] != 'pending':
        return None
    invitees = await conn.fetch("SELECT * FROM friend_challenge_invitees WHERE challenge_id=$1", challenge_id)
    if any(iv['status'] == 'pending' for iv in invitees):
        return None   # still waiting on someone

    accepted_ids = [iv['user_id'] for iv in invitees if iv['status'] == 'accepted']
    participant_ids = [ch['challenger_id']] + accepted_ids
    min_players = GAME_MIN_PLAYERS.get(ch['game_type'], 2)

    if len(participant_ids) < min_players:
        await grant_tickets(ch['challenger_id'], CHALLENGE_TICKET_TOLL, 'friends_challenge_refund',
                             {'challenge_id': challenge_id}, conn)
        for uid in accepted_ids:
            await grant_tickets(uid, CHALLENGE_TICKET_TOLL, 'friends_challenge_refund',
                                 {'challenge_id': challenge_id}, conn)
        await conn.execute("UPDATE friend_challenges SET status='cancelled' WHERE id=$1", challenge_id)
        return None

    await conn.execute("UPDATE friend_challenges SET status='resolving' WHERE id=$1", challenge_id)
    return {'game_type': ch['game_type'], 'participant_ids': participant_ids, 'game_params': ch['game_params']}


async def _finish_challenge_start(challenge_id: int, pending: dict):
    """Phase 2 -- call AFTER the transaction that produced `pending` has
    committed, so its row locks are released before the game's own
    staking transaction needs those same rows. Creates the real game
    room on a fresh connection, then marks the challenge started. If
    room creation fails (e.g. a balance changed in the interim), refunds
    everyone's ticket toll and cancels instead of leaving the challenge
    stuck in 'resolving' forever."""
    pool = await get_db()
    try:
        room_id = await _create_game_room(pending['game_type'], pending['participant_ids'], pending['game_params'])
    except HTTPException:
        async with pool.acquire() as conn:
            async with conn.transaction():
                for uid in pending['participant_ids']:
                    await grant_tickets(uid, CHALLENGE_TICKET_TOLL, 'friends_challenge_refund',
                                         {'challenge_id': challenge_id}, conn)
                await conn.execute("UPDATE friend_challenges SET status='cancelled' WHERE id=$1", challenge_id)
        raise

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE friend_challenges SET status='started', game_room_id=$1 WHERE id=$2",
            room_id, challenge_id
        )


# ── Background: refund + expire stale PvP challenges ───────────
# Challenges hold the challenger's tickets until accepted/declined/expired.
# Nothing else ever settled a challenge the challenged player ignored, so
# those tickets used to be lost forever. This sweeps them back.

async def expire_pvp_challenges_loop():
    while True:
        await asyncio.sleep(60)
        try:
            pool = await get_db()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    stale = await conn.fetch("""
                        SELECT id, challenger_id, bet_tickets FROM pvp_challenges
                        WHERE status='pending' AND expires_at <= NOW()
                        FOR UPDATE
                    """)
                    for ch in stale:
                        await conn.execute(
                            "UPDATE users SET tickets = tickets + $1 WHERE user_id=$2",
                            ch["bet_tickets"], ch["challenger_id"],
                        )
                        await conn.execute(
                            "UPDATE pvp_challenges SET status='expired' WHERE id=$1", ch["id"],
                        )
                    if stale:
                        logger.info(f"Refunded {len(stale)} expired PvP challenge(s)")
        except Exception as e:
            logger.warning(f"expire_pvp_challenges_loop failed: {e}")

        try:
            await _expire_friend_challenges()
        except Exception as e:
            logger.warning(f"expire_friend_challenges failed: {e}")


async def _expire_friend_challenges():
    """Sibling sweep to the ticket-coinflip one above, same 60s cadence
    (called from the same loop) -- refunds the ticket toll to the
    challenger and to any invitee who already accepted, for any
    friend_challenges row that timed out without every invitee
    responding."""
    pool = await get_db()
    async with pool.acquire() as conn:
        candidates = await conn.fetch(
            "SELECT id FROM friend_challenges WHERE status='pending' AND expires_at <= NOW()"
        )
        count = 0
        for row in candidates:
            cid = row['id']
            async with conn.transaction():
                ch = await conn.fetchrow("SELECT * FROM friend_challenges WHERE id=$1 FOR UPDATE", cid)
                if not ch or ch['status'] != 'pending':
                    continue
                invitees = await conn.fetch(
                    "SELECT * FROM friend_challenge_invitees WHERE challenge_id=$1", cid
                )
                await grant_tickets(ch['challenger_id'], CHALLENGE_TICKET_TOLL, 'friends_challenge_expired_refund',
                                     {'challenge_id': cid}, conn)
                for iv in invitees:
                    if iv['status'] == 'accepted':
                        await grant_tickets(iv['user_id'], CHALLENGE_TICKET_TOLL, 'friends_challenge_expired_refund',
                                             {'challenge_id': cid}, conn)
                await conn.execute("UPDATE friend_challenges SET status='expired' WHERE id=$1", cid)
                count += 1
        if count:
            logger.info(f"Expired {count} friend challenge(s), refunded tickets")


# ── List friends ───────────────────────────────────────────────

@router.get("/api/friends")
async def get_friends(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.user_id, u.username, u.avatar_url, u.google_avatar_url,
                   u.primary_provider, u.last_seen, u.level, f.id AS friendship_id
            FROM friendships f
            JOIN users u ON u.user_id = CASE
                WHEN f.requester_id = $1 THEN f.addressee_id ELSE f.requester_id END
            WHERE (f.requester_id = $1 OR f.addressee_id = $1) AND f.status = 'accepted'
            ORDER BY u.username
        """, user_id)
        return {"friends": [{
            "user_id":       str(r["user_id"]),
            "username":      r["username"],
            "avatar_url":    _avatar(r),
            "online":        _is_online(r["last_seen"]),
            "level":         int(r["level"] or 1),
            "friendship_id": r["friendship_id"],
        } for r in rows]}


# ── Friend requests (in / out) ─────────────────────────────────

@router.get("/api/friends/requests")
async def get_friend_requests(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT f.id, f.requester_id, f.addressee_id,
                   u.username, u.avatar_url, u.google_avatar_url, u.primary_provider
            FROM friendships f
            JOIN users u ON u.user_id = CASE
                WHEN f.requester_id = $1 THEN f.addressee_id ELSE f.requester_id END
            WHERE (f.requester_id = $1 OR f.addressee_id = $1) AND f.status = 'pending'
            ORDER BY f.created_at DESC
        """, user_id)
        incoming, outgoing = [], []
        for r in rows:
            entry = {
                "id":        r["id"],
                "user_id":   str(r["requester_id"] if r["addressee_id"] == user_id else r["addressee_id"]),
                "username":  r["username"],
                "avatar_url": _avatar(r),
            }
            (incoming if r["addressee_id"] == user_id else outgoing).append(entry)
        return {"incoming": incoming, "outgoing": outgoing}


# ── Pending PvP challenges directed at me ─────────────────────

@router.get("/api/friends/challenges")
async def get_challenges(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT c.id, c.challenger_id, c.bet_tickets,
                   u.username, u.avatar_url, u.google_avatar_url, u.primary_provider
            FROM pvp_challenges c
            JOIN users u ON u.user_id = c.challenger_id
            WHERE c.challenged_id = $1 AND c.status = 'pending' AND c.expires_at > NOW()
            ORDER BY c.created_at DESC
        """, user_id)
        ticket_challenges = [{
            "id":                r["id"],
            "challenger_id":     str(r["challenger_id"]),
            "challenger_name":   r["username"],
            "challenger_avatar": _avatar(r),
            "bet_tickets":       r["bet_tickets"],
        } for r in rows]

        # New: game-specific challenges I sent or was invited to (Session 4)
        fc_rows = await conn.fetch("""
            SELECT fc.id, fc.challenger_id, fc.game_type, fc.game_params, fc.game_room_id, fc.status,
                   u.username AS challenger_name, u.avatar_url, u.google_avatar_url, u.primary_provider
            FROM friend_challenges fc
            JOIN users u ON u.user_id = fc.challenger_id
            WHERE fc.status='pending' AND fc.expires_at > NOW()
              AND (fc.challenger_id=$1 OR EXISTS (
                  SELECT 1 FROM friend_challenge_invitees fci WHERE fci.challenge_id=fc.id AND fci.user_id=$1
              ))
            ORDER BY fc.created_at DESC
        """, user_id)
        duel_challenges = []
        for r in fc_rows:
            invitees = await conn.fetch("""
                SELECT fci.user_id, fci.status, u.username
                FROM friend_challenge_invitees fci JOIN users u ON u.user_id = fci.user_id
                WHERE fci.challenge_id=$1
            """, r["id"])
            duel_challenges.append({
                "id":                r["id"],
                "challenger_id":     str(r["challenger_id"]),
                "challenger_name":   r["challenger_name"],
                "challenger_avatar": _avatar(r),
                "game_type":         r["game_type"],
                "game_params":       r["game_params"],
                "is_challenger":     r["challenger_id"] == user_id,
                "invitees": [
                    {"user_id": str(iv["user_id"]), "username": iv["username"], "status": iv["status"]}
                    for iv in invitees
                ],
            })

        return {"challenges": ticket_challenges, "duel_challenges": duel_challenges}


# ── Search users by username (autocomplete when adding) ───────

@router.get("/api/friends/search")
async def search_users(request: Request, q: str = ""):
    user_id = await require_auth(request)
    q = q.strip()
    if len(q) < 2:
        return {"results": []}
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.user_id, u.username, u.avatar_url, u.google_avatar_url, u.primary_provider,
                   f.status,
                   (f.requester_id = $1) AS requested_by_me
            FROM users u
            LEFT JOIN friendships f
                ON (f.requester_id = $1 AND f.addressee_id = u.user_id)
                OR (f.requester_id = u.user_id AND f.addressee_id = $1)
            WHERE u.username ILIKE $2 AND u.user_id != $1
            ORDER BY u.username
            LIMIT 8
        """, user_id, f"%{q}%")
        results = []
        for r in rows:
            if r["status"] == "accepted":
                rel = "friends"
            elif r["status"] == "pending":
                rel = "outgoing" if r["requested_by_me"] else "incoming"
            else:
                rel = "none"
            results.append({
                "user_id":     str(r["user_id"]),
                "username":    r["username"],
                "avatar_url":  _avatar(r),
                "relationship": rel,
            })
        return {"results": results}


# ── Send friend request (by username or numeric ID) ───────────

@router.post("/api/friends/request")
async def send_friend_request(request: Request):
    user_id = await require_auth(request)
    await check_rate_limit(request, RATE_WRITE)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    query = str(body.get("username_or_id", "")).strip()
    if not query:
        raise HTTPException(400, "username_or_id required")

    pool = await get_db()
    async with pool.acquire() as conn:
        target = None
        if query.isdigit():
            target = await conn.fetchrow("SELECT user_id FROM users WHERE user_id=$1", int(query))
        if not target:
            target = await conn.fetchrow(
                "SELECT user_id FROM users WHERE lower(username)=lower($1)", query
            )
        if not target:
            raise HTTPException(404, "User not found")
        target_id = target["user_id"]
        if target_id == user_id:
            raise HTTPException(400, "Cannot add yourself")

        # The friendships table's UNIQUE constraint is on (requester_id,
        # addressee_id) -- directional, not symmetric -- so it does NOT
        # prevent two rows existing for the same pair in opposite
        # directions (A->B pending AND B->A pending both insert cleanly).
        # An advisory transaction lock keyed by the sorted pair serializes
        # concurrent add-friend calls between the same two users, closing
        # that race regardless of which direction each call comes from
        # (previously only a literal simultaneous INSERT conflict was
        # handled, which this constraint shape can't actually produce).
        async with conn.transaction():
            lo, hi = (user_id, target_id) if user_id < target_id else (target_id, user_id)
            # pg_advisory_xact_lock(int4, int4) can't take a raw Discord
            # snowflake directly (64-bit, overflows int4) -- hash each ID
            # down to a 32-bit key first. A rare hash collision would only
            # cause two unrelated pairs to briefly wait on each other's
            # lock, not a correctness issue.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1::text), hashtext($2::text))", str(lo), str(hi)
            )

            existing = await conn.fetchrow("""
                SELECT id, status, requester_id FROM friendships
                WHERE (requester_id=$1 AND addressee_id=$2) OR (requester_id=$2 AND addressee_id=$1)
            """, user_id, target_id)
            if existing:
                if existing["status"] == "accepted":
                    raise HTTPException(400, "Already friends")
                if existing["requester_id"] == target_id:
                    # They already sent us a request — adding them back accepts it,
                    # instead of erroring and leaving the friendship stuck pending forever.
                    await conn.execute(
                        "UPDATE friendships SET status='accepted', updated_at=NOW() WHERE id=$1",
                        existing["id"],
                    )
                    username = await conn.fetchval("SELECT username FROM users WHERE user_id=$1", target_id)
                    return {"success": True, "message": f"You are now friends with {username}!"}
                raise HTTPException(400, "Request already pending")

            await conn.execute("""
                INSERT INTO friendships (requester_id, addressee_id, status)
                VALUES ($1, $2, 'pending')
            """, user_id, target_id)
        username = await conn.fetchval("SELECT username FROM users WHERE user_id=$1", target_id)
        return {"success": True, "message": f"Friend request sent to {username}"}


# ── Accept / decline friend request ───────────────────────────

@router.post("/api/friends/accept/{request_id}")
async def accept_friend_request(request_id: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM friendships WHERE id=$1 AND addressee_id=$2 AND status='pending'",
            request_id, user_id,
        )
        if not row:
            raise HTTPException(404, "Request not found")
        await conn.execute(
            "UPDATE friendships SET status='accepted', updated_at=NOW() WHERE id=$1", request_id
        )
        return {"success": True}


@router.post("/api/friends/decline/{request_id}")
async def decline_friend_request(request_id: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM friendships WHERE id=$1 AND (requester_id=$2 OR addressee_id=$2)",
            request_id, user_id,
        )
        return {"success": True}


# ── Unfriend ───────────────────────────────────────────────────

@router.delete("/api/friends/{friend_id}")
async def remove_friend(friend_id: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM friendships
            WHERE ((requester_id=$1 AND addressee_id=$2) OR (requester_id=$2 AND addressee_id=$1))
              AND status='accepted'
        """, user_id, friend_id)
        return {"success": True}


# ── View a friend's public profile ────────────────────────────

@router.get("/api/friends/{friend_id}/profile")
async def friend_public_profile(friend_id: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        ok = await conn.fetchrow("""
            SELECT id FROM friendships
            WHERE ((requester_id=$1 AND addressee_id=$2) OR (requester_id=$2 AND addressee_id=$1))
              AND status='accepted'
        """, user_id, friend_id)
        if not ok:
            raise HTTPException(403, "Not friends with this user")

        u = await conn.fetchrow("""
            SELECT u.user_id, u.username, u.avatar_url, u.google_avatar_url,
                   u.primary_provider, u.level, u.prestige, u.last_seen,
                   (SELECT COUNT(*) FROM inventory WHERE user_id=u.user_id AND status != 'sold') AS item_count
            FROM users u WHERE u.user_id=$1
        """, friend_id)
        if not u:
            raise HTTPException(404, "User not found")

        drops = await conn.fetch("""
            SELECT item_name, rarity, condition, float_value FROM inventory
            WHERE user_id=$1 AND status != 'sold'
            ORDER BY acquired_at DESC NULLS LAST LIMIT 5
        """, friend_id)

        return {
            "user_id":      str(u["user_id"]),
            "username":     u["username"],
            "avatar_url":   _avatar(u),
            "level":        int(u["level"] or 1),
            "prestige":     int(u["prestige"] or 0),
            "item_count":   int(u["item_count"] or 0),
            "online":       _is_online(u["last_seen"]),
            "recent_drops": [{
                "name": d["item_name"], "rarity": d["rarity"],
                "condition": d["condition"],
                "float_value": float(d["float_value"]) if d["float_value"] is not None else None,
            } for d in drops],
        }


@router.get("/api/friends/{friend_id}/loadout")
async def friend_loadout(friend_id: int, request: Request):
    """Read-only view of a friend's showcased loadout -- reuses the exact
    same accepted-friendship gate as friend_public_profile above, and the
    same item-enrichment logic as the current user's own GET /api/loadout
    (server.py) so the frontend can reuse its existing item-card renderer."""
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        ok = await conn.fetchrow("""
            SELECT id FROM friendships
            WHERE ((requester_id=$1 AND addressee_id=$2) OR (requester_id=$2 AND addressee_id=$1))
              AND status='accepted'
        """, user_id, friend_id)
        if not ok:
            raise HTTPException(403, "Not friends with this user")

        rows = await conn.fetch(
            "SELECT * FROM inventory WHERE user_id=$1 AND in_loadout=TRUE AND status='kept' ORDER BY created_at DESC",
            friend_id
        )

    import json as _json, re as _re

    def _enrich(r):
        d = convert_decimals(dict(r))
        raw = d.get("item_name", "")
        clean = _re.sub(r'^[\U0001F300-\U0001FFFF\U00002600-\U000027BF\U0000FE00-\U0000FEFF\s🟦🟪🟥🟨🟩⬛⬜🟫🔥⭐💫👑✨]+', '', raw).strip()
        d["display_name"] = clean or raw
        if not d.get("image_url"):
            filename = get_skin_image_filename(clean or raw)
            if filename:
                d["image_url"] = f"/static/images/skins/{filename}"
        raw_st = d.get("applied_stickers")
        d["applied_stickers"] = _json.loads(raw_st) if isinstance(raw_st, str) else (raw_st or [])
        return d

    return {"items": [_enrich(r) for r in rows]}


# ── Send PvP ticket challenge ──────────────────────────────────

@router.post("/api/friends/{friend_id}/challenge")
async def challenge_friend(friend_id: int, request: Request):
    user_id = await require_auth(request)
    await check_rate_limit(request, RATE_WRITE)
    body = await request.json()
    bet = max(1, min(10, int(body.get("bet_tickets", 1))))

    pool = await get_db()
    async with pool.acquire() as conn:
        ok = await conn.fetchrow("""
            SELECT id FROM friendships
            WHERE ((requester_id=$1 AND addressee_id=$2) OR (requester_id=$2 AND addressee_id=$1))
              AND status='accepted'
        """, user_id, friend_id)
        if not ok:
            raise HTTPException(403, "Not friends with this user")

        async with conn.transaction():
            tix = await conn.fetchval("SELECT tickets FROM users WHERE user_id=$1 FOR UPDATE", user_id)
            if (tix or 0) < bet:
                raise HTTPException(400, "Not enough tickets")
            dup = await conn.fetchrow("""
                SELECT id FROM pvp_challenges
                WHERE challenger_id=$1 AND challenged_id=$2
                  AND status='pending' AND expires_at > NOW()
            """, user_id, friend_id)
            if dup:
                raise HTTPException(400, "You already have a pending challenge to this player")

            await conn.execute("UPDATE users SET tickets = tickets - $1 WHERE user_id=$2", bet, user_id)
            cid = await conn.fetchval("""
                INSERT INTO pvp_challenges (challenger_id, challenged_id, bet_tickets)
                VALUES ($1, $2, $3) RETURNING id
            """, user_id, friend_id, bet)
        return {"success": True, "challenge_id": cid}


# ── Accept / decline PvP challenge ────────────────────────────

@router.post("/api/friends/challenges/{challenge_id}/accept")
async def accept_challenge(challenge_id: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            ch = await conn.fetchrow("""
                SELECT * FROM pvp_challenges
                WHERE id=$1 AND challenged_id=$2 AND status='pending' AND expires_at > NOW()
                FOR UPDATE
            """, challenge_id, user_id)
            if not ch:
                raise HTTPException(404, "Challenge not found or expired")
            bet = ch["bet_tickets"]
            tix = await conn.fetchval("SELECT tickets FROM users WHERE user_id=$1 FOR UPDATE", user_id)
            if (tix or 0) < bet:
                raise HTTPException(400, "Not enough tickets")

            await conn.execute("UPDATE users SET tickets = tickets - $1 WHERE user_id=$2", bet, user_id)
            winner_id = ch["challenger_id"] if secure_random() < 0.5 else user_id
            await conn.execute("UPDATE users SET tickets = tickets + $1 WHERE user_id=$2", bet * 2, winner_id)
            await conn.execute("""
                UPDATE pvp_challenges SET status='completed', winner_id=$1, completed_at=NOW()
                WHERE id=$2
            """, winner_id, challenge_id)

        return {
            "success":     True,
            "winner_id":   str(winner_id),
            "you_won":     winner_id == user_id,
            "tickets_won": bet * 2 if winner_id == user_id else 0,
        }


@router.post("/api/friends/challenges/{challenge_id}/decline")
async def decline_challenge(challenge_id: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        ch = await conn.fetchrow(
            "SELECT * FROM pvp_challenges WHERE id=$1 AND challenged_id=$2 AND status='pending'",
            challenge_id, user_id,
        )
        if not ch:
            raise HTTPException(404, "Challenge not found")
        async with conn.transaction():
            await conn.execute(
                "UPDATE users SET tickets = tickets + $1 WHERE user_id=$2",
                ch["bet_tickets"], ch["challenger_id"],
            )
            await conn.execute("UPDATE pvp_challenges SET status='declined' WHERE id=$1", challenge_id)
        return {"success": True}


# ── Generalized game challenges (Session 4) ─────────────────────

class GameChallengeRequest(BaseModel):
    game_type: str
    invited_user_ids: list
    game_params: dict = {}


@router.post("/api/friends/challenge")
async def send_game_challenge(body: GameChallengeRequest, request: Request):
    user_id = await require_auth(request)
    await check_rate_limit(request, RATE_WRITE)

    if body.game_type not in GAME_MIN_PLAYERS:
        raise HTTPException(400, f"Unsupported game_type: {body.game_type}")

    invited = list(dict.fromkeys(int(u) for u in body.invited_user_ids))
    if not invited:
        raise HTTPException(400, "Must invite at least 1 friend")
    if user_id in invited:
        raise HTTPException(400, "Cannot invite yourself")
    max_invitees = GAME_MAX_PLAYERS[body.game_type] - 1
    if len(invited) > max_invitees:
        raise HTTPException(400, f"{body.game_type} supports at most {max_invitees} invitee(s)")

    pool = await get_db()
    async with pool.acquire() as conn:
        for fid in invited:
            ok = await conn.fetchrow("""
                SELECT id FROM friendships
                WHERE ((requester_id=$1 AND addressee_id=$2) OR (requester_id=$2 AND addressee_id=$1))
                  AND status='accepted'
            """, user_id, fid)
            if not ok:
                raise HTTPException(403, f"Not friends with user {fid}")

        async with conn.transaction():
            for _ in range(CHALLENGE_TICKET_TOLL):
                if not await deduct_ticket(user_id, 'friends_challenge_send', {'game_type': body.game_type}, conn):
                    raise HTTPException(400, "Not enough tickets")

            cid = await conn.fetchval("""
                INSERT INTO friend_challenges (challenger_id, game_type, game_params)
                VALUES ($1, $2, $3) RETURNING id
            """, user_id, body.game_type, body.game_params)
            for fid in invited:
                await conn.execute(
                    "INSERT INTO friend_challenge_invitees (challenge_id, user_id) VALUES ($1, $2)",
                    cid, fid
                )
        return {"success": True, "challenge_id": cid}


class AcceptChallengeBody(BaseModel):
    inventory_id: Optional[int] = None   # required only for item-staked game_types (item_wager_duel, item_trade_up_duel)


@router.post("/api/friends/challenge/{challenge_id}/accept")
async def accept_game_challenge(challenge_id: int, body: AcceptChallengeBody, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    pending_start = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            iv = await conn.fetchrow("""
                SELECT fci.id, fc.status AS challenge_status, fc.expires_at, fc.game_type
                FROM friend_challenge_invitees fci
                JOIN friend_challenges fc ON fc.id = fci.challenge_id
                WHERE fci.challenge_id=$1 AND fci.user_id=$2 AND fci.status='pending'
                FOR UPDATE OF fci
            """, challenge_id, user_id)
            if not iv:
                raise HTTPException(404, "Challenge invite not found")
            if iv["challenge_status"] != "pending" or iv["expires_at"] <= datetime.now(timezone.utc):
                raise HTTPException(400, "This challenge is no longer active")

            for _ in range(CHALLENGE_TICKET_TOLL):
                if not await deduct_ticket(user_id, 'friends_challenge_accept', {'challenge_id': challenge_id}, conn):
                    raise HTTPException(400, "Not enough tickets")

            await conn.execute(
                "UPDATE friend_challenge_invitees SET status='accepted', responded_at=NOW() WHERE id=$1",
                iv["id"]
            )

            # Item-staked games: the challenger could only pick THEIR OWN
            # item at send time -- the invited friend's item is unknown
            # until they accept, since a challenger can't see a friend's
            # inventory. Merge it into game_params here, keyed by this
            # accepter's user_id, so _create_game_room finds it later.
            if iv["game_type"] in ('item_wager_duel', 'item_trade_up_duel'):
                if body.inventory_id is None:
                    raise HTTPException(400, "Select an item to stake before accepting")
                ch_row = await conn.fetchrow("SELECT game_params FROM friend_challenges WHERE id=$1", challenge_id)
                params = dict(ch_row["game_params"] or {})
                inv_ids = dict(params.get("inventory_ids", {}))
                inv_ids[str(user_id)] = body.inventory_id
                params["inventory_ids"] = inv_ids
                await conn.execute(
                    "UPDATE friend_challenges SET game_params=$1 WHERE id=$2", params, challenge_id
                )
            # Phase 1 only -- does not create the game room. See
            # _resolve_challenge_if_ready's docstring for why: doing so
            # inside this same transaction (which still holds a lock on
            # this user's row from the ticket-toll deduction above) would
            # deadlock against the game's own cash/ticket staking on that
            # same row once we release control to it.
            pending_start = await _resolve_challenge_if_ready(challenge_id, conn)
        # Transaction committed here -- its row locks are released before
        # phase 2 (which touches the same user rows) runs.

    if pending_start:
        await _finish_challenge_start(challenge_id, pending_start)

    async with pool.acquire() as conn:
        result = await conn.fetchrow(
            "SELECT status, game_type, game_room_id, game_params FROM friend_challenges WHERE id=$1", challenge_id
        )
    return {
        "success":      True,
        "status":       result["status"],
        "game_type":    result["game_type"],
        "game_room_id": result["game_room_id"],
        "game_params":  result["game_params"],
    }


@router.post("/api/friends/challenge/{challenge_id}/decline")
async def decline_game_challenge(challenge_id: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    pending_start = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            iv = await conn.fetchrow("""
                SELECT * FROM friend_challenge_invitees
                WHERE challenge_id=$1 AND user_id=$2 AND status='pending'
                FOR UPDATE
            """, challenge_id, user_id)
            if not iv:
                raise HTTPException(404, "Challenge invite not found")
            await conn.execute(
                "UPDATE friend_challenge_invitees SET status='declined', responded_at=NOW() WHERE id=$1",
                iv["id"]
            )
            # An early decline can still leave enough accepted players to
            # start (e.g. a 4-player Case Battles challenge with 3
            # invitees) -- same phase-1/phase-2 split as accept, for the
            # same deadlock reason.
            pending_start = await _resolve_challenge_if_ready(challenge_id, conn)

    if pending_start:
        await _finish_challenge_start(challenge_id, pending_start)

    return {"success": True}
