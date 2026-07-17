import json
import secrets
from fastapi import APIRouter, HTTPException, Request
from shared import get_db, require_auth, logger, SKINS_DATA, ITEM_ID_TO_DISPLAY_NAME
from shared import secure_randint, secure_choice, secure_random, require_game_enabled
from shared import get_user_id_from_session

router = APIRouter()

# Payout tiers — (threshold, tickets_won)
REACTION_TIERS = [(150, 8), (200, 5), (300, 3), (450, 2), (600, 1)]
AIM_TIERS      = [(20, 6), (18, 4), (15, 3), (10, 2), (5, 1)]
MEMORY_TIERS   = [(8, 6), (6, 4), (4, 3), (2, 1)]
FLOAT_TIERS    = [(0.01, 8), (0.03, 5), (0.05, 3), (0.10, 1)]
WIRE_COLORS    = ["red", "blue", "green", "yellow", "white"]

# 'asc' = lower score is better (reaction time, float diff), 'desc' = higher is
# better (hits, correct-in-sequence). Bomb Defuse has no entry here -- its
# score is always 1.0/0.0 (win/lose), a leaderboard-by-score concept doesn't
# fit a pass/fail game.
LEADERBOARD_DIRECTION = {"reaction": "asc", "aim": "desc", "float": "asc", "memory": "desc"}


async def _start_game(user_id: int, game_type: str, game_data: dict, conn) -> str:
    tix = await conn.fetchval("SELECT tickets FROM users WHERE user_id=$1 FOR UPDATE", user_id)
    if (tix or 0) < 1:
        raise HTTPException(400, "Not enough tickets")
    token = secrets.token_urlsafe(32)
    await conn.execute("UPDATE users SET tickets = tickets - 1 WHERE user_id=$1", user_id)
    await conn.execute("""
        INSERT INTO ticket_games (user_id, game_type, session_token, game_data)
        VALUES ($1, $2, $3, $4)
    """, user_id, game_type, token, json.dumps(game_data))
    await conn.execute("""
        INSERT INTO ticket_transactions (user_id, amount, source, metadata)
        VALUES ($1, -1, 'ticket_game', $2)
    """, user_id, json.dumps({"game": game_type}))
    return token


async def _get_active_game(user_id: int, token: str, game_type: str, expiry_min: int, conn):
    row = await conn.fetchrow("""
        SELECT id, game_data FROM ticket_games
        WHERE session_token=$1 AND user_id=$2 AND game_type=$3 AND status='active'
          AND started_at > NOW() - ($4 * INTERVAL '1 minute')
        FOR UPDATE
    """, token, user_id, game_type, expiry_min)
    if not row:
        raise HTTPException(400, "Invalid or expired game session")
    return row


async def _complete_game(user_id: int, game_type: str, token: str, tickets_won: int, score: float, conn, direction: str = None) -> bool:
    """Records the completed round, pays out tickets, and -- if `direction` is
    given ('asc'/'desc', see LEADERBOARD_DIRECTION) -- reports whether this
    score is a new personal best for this user+game_type (checked against
    prior 'completed' rows only, so the in-flight row being written here
    can't count against itself)."""
    is_new_best = False
    if direction:
        order = "ASC" if direction == "asc" else "DESC"
        prev_best = await conn.fetchval(f"""
            SELECT score FROM ticket_games
            WHERE user_id=$1 AND game_type=$2 AND status='completed'
            ORDER BY score {order} LIMIT 1
        """, user_id, game_type)
        if prev_best is None:
            is_new_best = True
        elif direction == "asc":
            is_new_best = score < float(prev_best)
        else:
            is_new_best = score > float(prev_best)

    await conn.execute("""
        UPDATE ticket_games
        SET status='completed', completed_at=NOW(), score=$1, tickets_won=$2
        WHERE session_token=$3
    """, score, tickets_won, token)
    if tickets_won > 0:
        await conn.execute("UPDATE users SET tickets = tickets + $1 WHERE user_id=$2", tickets_won, user_id)
        await conn.execute("""
            INSERT INTO ticket_transactions (user_id, amount, source, metadata)
            VALUES ($1, $2, 'ticket_game_win', $3)
        """, user_id, tickets_won, json.dumps({"score": score}))
    return is_new_best


# ── Reaction Time ──────────────────────────────────────────────

@router.post("/api/ticket-games/reaction/start")
async def reaction_start(request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("reaction")
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            token = await _start_game(user_id, "reaction", {}, conn)
    return {"token": token}


@router.post("/api/ticket-games/reaction/submit")
async def reaction_submit(request: Request):
    user_id = await require_auth(request)
    body = await request.json()
    token = str(body.get("token", ""))
    ms = float(body.get("ms", 9999))
    if ms < 100:   # anti-cheat floor — impossible to react faster
        ms = 9999
    tickets_won = next((r for t, r in REACTION_TIERS if ms < t), 0)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _get_active_game(user_id, token, "reaction", 5, conn)
            is_new_best = await _complete_game(user_id, "reaction", token, tickets_won, ms, conn, "asc")
    return {"tickets_won": tickets_won, "ms": round(ms), "is_new_best": is_new_best}


# ── Aim Trainer ────────────────────────────────────────────────

@router.post("/api/ticket-games/aim/start")
async def aim_start(request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("aim-trainer")
    targets = [{"x": secure_randint(5, 92), "y": secure_randint(5, 85), "r": secure_randint(18, 40)} for _ in range(20)]
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            token = await _start_game(user_id, "aim", {"targets": targets}, conn)
    return {"token": token, "targets": targets}


@router.post("/api/ticket-games/aim/submit")
async def aim_submit(request: Request):
    user_id = await require_auth(request)
    body = await request.json()
    token = str(body.get("token", ""))
    hits = min(20, max(0, int(body.get("hits", 0))))
    tickets_won = next((r for t, r in AIM_TIERS if hits >= t), 0)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _get_active_game(user_id, token, "aim", 10, conn)
            is_new_best = await _complete_game(user_id, "aim", token, tickets_won, float(hits), conn, "desc")
    return {"tickets_won": tickets_won, "hits": hits, "is_new_best": is_new_best}


# ── Bomb Defuse ────────────────────────────────────────────────

@router.post("/api/ticket-games/bomb/start")
async def bomb_start(request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("bomb-defuse")
    safe = secure_choice(WIRE_COLORS)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            token = await _start_game(user_id, "bomb", {"safe_wire": safe}, conn)
    return {"token": token, "wires": WIRE_COLORS}


@router.post("/api/ticket-games/bomb/submit")
async def bomb_submit(request: Request):
    user_id = await require_auth(request)
    body = await request.json()
    token = str(body.get("token", ""))
    chosen = str(body.get("wire", ""))
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _get_active_game(user_id, token, "bomb", 3, conn)
            safe = json.loads(row["game_data"])["safe_wire"]
            won = chosen == safe
            await _complete_game(user_id, "bomb", token, 3 if won else 0, 1.0 if won else 0.0, conn)
    return {"tickets_won": 3 if won else 0, "won": won, "safe_wire": safe, "chose": chosen}


# ── Float Guesser ──────────────────────────────────────────────

@router.post("/api/ticket-games/float/start")
async def float_start(request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("float-guesser")
    skin = secure_choice(SKINS_DATA)
    fmin = float(skin.get("floatTop", 0.06))
    fmax = float(skin.get("floatBottom", 0.80))
    if fmin >= fmax:
        fmin, fmax = 0.06, 0.80
    actual = round(fmin + secure_random() * (fmax - fmin), 4)
    weapon = ITEM_ID_TO_DISPLAY_NAME.get(skin.get("itemId", ""), skin.get("weaponType", "Unknown"))
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            token = await _start_game(user_id, "float", {"actual": actual, "fmin": fmin, "fmax": fmax}, conn)
    return {
        "token":      token,
        "skin_name":  f"{weapon} | {skin.get('name', 'Unknown')}",
        "skin_image": skin.get("skinImage", ""),
        "float_min":  fmin,
        "float_max":  fmax,
    }


@router.post("/api/ticket-games/float/submit")
async def float_submit(request: Request):
    user_id = await require_auth(request)
    body = await request.json()
    token = str(body.get("token", ""))
    guess = float(body.get("guess", 0))
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _get_active_game(user_id, token, "float", 5, conn)
            gd = json.loads(row["game_data"])
            actual = float(gd["actual"])
            diff = abs(guess - actual)
            tickets_won = next((r for t, r in FLOAT_TIERS if diff <= t), 0)
            is_new_best = await _complete_game(user_id, "float", token, tickets_won, diff, conn, "asc")
    return {"tickets_won": tickets_won, "actual": actual, "guess": guess, "diff": round(diff, 4), "is_new_best": is_new_best}


# ── Memory Sequence ────────────────────────────────────────────

@router.post("/api/ticket-games/memory/start")
async def memory_start(request: Request):
    user_id = await require_auth(request)
    await require_game_enabled("memory-sequence")
    sequence = [secure_randint(0, 15) for _ in range(10)]
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            token = await _start_game(user_id, "memory", {"sequence": sequence}, conn)
    return {"token": token, "sequence": sequence}   # client displays this; server validates submission


@router.post("/api/ticket-games/memory/submit")
async def memory_submit(request: Request):
    user_id = await require_auth(request)
    body = await request.json()
    token = str(body.get("token", ""))
    answered = list(body.get("sequence", []))
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _get_active_game(user_id, token, "memory", 10, conn)
            seq = json.loads(row["game_data"])["sequence"]
            correct = 0
            for i, val in enumerate(answered):
                if i >= len(seq) or int(val) != seq[i]:
                    break
                correct += 1
            tickets_won = next((r for t, r in MEMORY_TIERS if correct >= t), 0)
            is_new_best = await _complete_game(user_id, "memory", token, tickets_won, float(correct), conn, "desc")
    return {"tickets_won": tickets_won, "correct": correct, "sequence": seq, "total": len(seq), "is_new_best": is_new_best}


# ── Leaderboards (reaction / aim / float / memory only -- bomb is pass/fail) ──

@router.get("/api/ticket-games/{game_type}/leaderboard")
async def arcade_leaderboard(game_type: str, request: Request):
    # Public board -- viewing doesn't require a session, matching every other
    # leaderboard tab on static/leaderboard.html. A session (if present) is
    # only used to fill in "your_best"; logged-out visitors just get None.
    user_id = await get_user_id_from_session(request)
    direction = LEADERBOARD_DIRECTION.get(game_type)
    if not direction:
        raise HTTPException(404, "No leaderboard for this game")
    order = "ASC" if direction == "asc" else "DESC"
    pool = await get_db()
    async with pool.acquire() as conn:
        # DISTINCT ON (user_id) with a matching ORDER BY picks each user's
        # single best row for this game_type in one pass.
        bests = await conn.fetch(f"""
            SELECT DISTINCT ON (t.user_id) t.user_id, t.score, u.username
            FROM ticket_games t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.game_type=$1 AND t.status='completed'
            ORDER BY t.user_id, t.score {order}
        """, game_type)
        ranked = sorted(bests, key=lambda r: r["score"], reverse=(direction == "desc"))[:10]
        my_best = None
        if user_id:
            my_best = await conn.fetchval(f"""
                SELECT score FROM ticket_games
                WHERE user_id=$1 AND game_type=$2 AND status='completed'
                ORDER BY score {order} LIMIT 1
            """, user_id, game_type)
    return {
        "leaderboard": [
            {"user_id": str(r["user_id"]), "username": r["username"], "score": float(r["score"])}
            for r in ranked
        ],
        "your_best": float(my_best) if my_best is not None else None,
    }
