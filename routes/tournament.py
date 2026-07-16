import json, math, random, asyncio
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, Dict, List
from shared import get_db, require_auth, logger, ADMIN_USER_IDS, secure_shuffle, secure_random, add_balance

router = APIRouter(prefix="/api/tournaments", tags=["tournaments"])

# ============================================================
# SESSION 8: real-game bracket orchestration (non-coinflip modes)
# ============================================================
# Every one of these 5 games' create_private_room() unconditionally stakes
# real cash from both players' own balances -- there's no way to fund a
# match "from the prize pool" without touching that already-shipped logic,
# which this session deliberately avoids. So every orchestrated match, in
# every round, stakes a flat small nominal amount instead of scaling with
# the tournament's entry fee (locked-in user decision). Item Wager Duel and
# Item Trade-Up Duel are permanently excluded -- their create_private_room()
# requires real inventory items consumed on every call, with no nominal-
# match path, so forcing that every bracket round isn't offered at all.
ORCHESTRATED_MODES = ['dice_duel', 'weapon_duel', 'reaction_duel', 'case_draft_duel', 'case_battles']
MATCH_STAKE = 10.0
POLL_INTERVAL_SECS = 4
STUCK_MATCH_TIMEOUT_MINUTES = 15

# game_mode -> (table_name, winner_column). Used by _get_game_winner() to
# poll each game's own table directly rather than adding a callback hook
# into any of the 5 already-shipped game files. Table/column names come
# from this hardcoded dict, never from user input.
GAME_WINNER_TABLES = {
    'dice_duel':        ('dice_duels', 'winner_id'),
    'weapon_duel':       ('weapon_duels', 'winner_id'),
    'reaction_duel':     ('reaction_duels', 'winner_id'),
    'case_draft_duel':   ('case_draft_duels', 'winner_id'),
    'case_battles':      ('case_battles', 'winner_id'),
}

# In-memory registry of running per-tournament poll tasks -- rebuilt on
# server restart by recover_stale_tournament_polls(), same idea as every
# other game's recover_stale_* startup sweep in this codebase.
_tournament_poll_tasks: Dict[int, asyncio.Task] = {}

class CreateBody(BaseModel):
    name: str
    entry_fee: float = 0
    max_players: int = 16
    game_mode: str = "classic"

class SubmitBody(BaseModel):
    player1_score: float
    player2_score: float

async def init_tournament_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tournaments (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                entry_fee NUMERIC(12,2) DEFAULT 0,
                max_players INT DEFAULT 16,
                game_mode TEXT DEFAULT 'classic',
                prize_pool NUMERIC(12,2) DEFAULT 0,
                created_by INT REFERENCES users(user_id),
                created_at TIMESTAMP DEFAULT NOW(),
                started_at TIMESTAMP,
                ended_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tournament_participants (
                id SERIAL PRIMARY KEY,
                tournament_id INT REFERENCES tournaments(id) ON DELETE CASCADE,
                user_id INT REFERENCES users(user_id),
                seed INT,
                current_score NUMERIC(12,2) DEFAULT 0,
                eliminated BOOLEAN DEFAULT FALSE,
                placement INT,
                joined_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(tournament_id, user_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tournament_matches (
                id SERIAL PRIMARY KEY,
                tournament_id INT REFERENCES tournaments(id) ON DELETE CASCADE,
                round INT NOT NULL,
                match_index INT NOT NULL,
                player1_id INT REFERENCES users(user_id),
                player2_id INT REFERENCES users(user_id),
                player1_score NUMERIC(12,2),
                player2_score NUMERIC(12,2),
                winner_id INT REFERENCES users(user_id),
                loser_id INT REFERENCES users(user_id),
                status TEXT DEFAULT 'pending',
                played_at TIMESTAMP,
                UNIQUE(tournament_id, round, match_index)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id INT REFERENCES users(user_id),
                amount NUMERIC(12,2) NOT NULL,
                type TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Session 8: link a match to the real game room orchestrating it
        # (dice_duels.id, case_battles.id, etc. depending on the
        # tournament's game_mode) and track when that room was created so
        # the poll task can detect a stuck match past STUCK_MATCH_TIMEOUT_MINUTES.
        await conn.execute("ALTER TABLE tournament_matches ADD COLUMN IF NOT EXISTS game_room_id INTEGER")
        await conn.execute("ALTER TABLE tournament_matches ADD COLUMN IF NOT EXISTS started_at TIMESTAMP")
        logger.info("tournament tables ready")


async def recover_stale_tournament_polls():
    """Server-restart recovery: the in-memory _tournament_poll_tasks dict is
    gone after a restart, so any orchestrated tournament with matches still
    'in_progress' would otherwise never advance again. Re-spawn a poll task
    for each such tournament -- mirrors every other game's recover_stale_*
    startup sweep, just re-arming a poller instead of refunding a stake
    (nothing needs refunding here; the real game rooms already have their
    own recovery sweeps for any money stuck inside them)."""
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT t.id, t.game_mode FROM tournaments t
            JOIN tournament_matches tm ON tm.tournament_id = t.id
            WHERE t.status = 'active' AND tm.status = 'in_progress'
        """)
    for row in rows:
        _ensure_poll_task(row['id'], row['game_mode'])
    if rows:
        logger.info(f"🏆 Re-armed {len(rows)} orchestrated tournament poll task(s) after restart")

@router.post("")
async def create_tournament(body: CreateBody, request: Request):
    user_id = await require_auth(request)
    # classic/speed/precision were never wired up (no score-submission UI
    # ever existed for them -- confirmed dead weight during audit) and stay
    # dead. Session 8 adds 5 real orchestrated game modes alongside the
    # original coinflip -- Item Wager Duel/Item Trade-Up Duel are
    # permanently excluded (real-item-consuming, no nominal-match path).
    if body.game_mode not in ("coinflip", *ORCHESTRATED_MODES):
        raise HTTPException(400, "Unsupported bracket game mode")
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO tournaments (name, entry_fee, max_players, game_mode, created_by)
            VALUES ($1,$2,$3,$4,$5)
            RETURNING id, name, status, entry_fee, max_players, game_mode, prize_pool, created_at
        """, body.name, body.entry_fee, body.max_players, body.game_mode, user_id)
        return dict(row)

@router.get("")
async def list_tournaments():
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT t.*,
                   (SELECT COUNT(*) FROM tournament_participants WHERE tournament_id=t.id) AS player_count
            FROM tournaments t
            ORDER BY t.created_at DESC
            LIMIT 50
        """)
        return [dict(r) for r in rows]

@router.get("/{tid}")
async def get_tournament(tid: int):
    pool = await get_db()
    async with pool.acquire() as conn:
        t = await conn.fetchrow("SELECT * FROM tournaments WHERE id=$1", tid)
        if not t:
            raise HTTPException(404, "Tournament not found")
        players = await conn.fetch("""
            SELECT u.username, u.avatar, tp.*
            FROM tournament_participants tp
            JOIN users u ON u.user_id=tp.user_id
            WHERE tp.tournament_id=$1
            ORDER BY tp.seed NULLS LAST, tp.current_score DESC
        """, tid)
        return {**dict(t), "players": [dict(p) for p in players]}

@router.get("/{tid}/bracket")
async def get_bracket(tid: int):
    pool = await get_db()
    async with pool.acquire() as conn:
        matches = await conn.fetch("""
            SELECT tm.*,
                   COALESCE(u1.username, 'TBD') AS player1_name,
                   COALESCE(u2.username, 'TBD') AS player2_name,
                   COALESCE(w.username, '')     AS winner_name
            FROM tournament_matches tm
            LEFT JOIN users u1 ON u1.user_id=tm.player1_id
            LEFT JOIN users u2 ON u2.user_id=tm.player2_id
            LEFT JOIN users w  ON w.user_id=tm.winner_id
            WHERE tm.tournament_id=$1
            ORDER BY tm.round, tm.match_index
        """, tid)
        return [dict(m) for m in matches]

@router.post("/{tid}/join")
async def join_tournament(tid: int, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        # Wrapped in an explicit transaction (unlike the rest of this file)
        # since this path can now also trigger _start_bracket() -- join +
        # auto-start must commit or roll back together.
        async with conn.transaction():
            t = await conn.fetchrow("SELECT * FROM tournaments WHERE id=$1 FOR UPDATE", tid)
            if not t:
                raise HTTPException(404, "Tournament not found")
            if t["status"] != "open":
                raise HTTPException(400, "Tournament is not open")

            count = await conn.fetchval("SELECT COUNT(*) FROM tournament_participants WHERE tournament_id=$1", tid)
            if count >= t["max_players"]:
                raise HTTPException(400, "Tournament is full")

            if t["entry_fee"] > 0:
                bal = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1 FOR UPDATE", user_id)
                if (bal or 0) < t["entry_fee"]:
                    raise HTTPException(400, "Insufficient balance")
                await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id=$2", t["entry_fee"], user_id)
                await conn.execute("UPDATE tournaments SET prize_pool = prize_pool + $1 WHERE id=$2", t["entry_fee"], tid)
                await conn.execute("""
                    INSERT INTO transactions (user_id, amount, type, description)
                    VALUES ($1, $2, 'tournament_entry', $3)
                """, user_id, -t["entry_fee"], f"Entry fee for {t['name']}")

            existing = await conn.fetchval("SELECT id FROM tournament_participants WHERE tournament_id=$1 AND user_id=$2", tid, user_id)
            if existing:
                raise HTTPException(400, "Already joined")

            await conn.execute("""
                INSERT INTO tournament_participants (tournament_id, user_id)
                VALUES ($1, $2)
            """, tid, user_id)

            new_count = count + 1
            auto_started = False
            bracket_result = None
            if t["game_mode"] in ("coinflip", *ORCHESTRATED_MODES) and new_count >= t["max_players"]:
                bracket_result = await _start_bracket(tid, conn)
                auto_started = True

        # _launch_orchestrated_matches() opens its OWN connection/transaction
        # per match (via _create_game_room() -> each game's create_private_room())
        # -- calling it while still inside the transaction above (which just
        # held a FOR UPDATE lock on this joining user's row) risks the exact
        # cross-connection deadlock discovered in Session 4's Friends
        # challenge system. Must run only AFTER that transaction has
        # committed and released its locks.
        if bracket_result and bracket_result.get('game_mode') in ORCHESTRATED_MODES:
            await _launch_orchestrated_matches(
                tid, bracket_result['game_mode'], bracket_result['round1_match_ids']
            )

        return {"ok": True, "player_count": new_count, "auto_started": auto_started}

@router.post("/{tid}/leave")
async def leave_tournament(tid: int, request: Request):
    """Self-service creation means players need a way out if a bracket
    never fills -- mirrors Item Jackpot's 'backout only in waiting phase'
    pattern. Only valid while the tournament hasn't started yet."""
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        t = await conn.fetchrow("SELECT * FROM tournaments WHERE id=$1 FOR UPDATE", tid)
        if not t:
            raise HTTPException(404, "Tournament not found")
        if t["status"] != "open":
            raise HTTPException(400, "Tournament has already started -- your entry is locked in")

        deleted = await conn.fetchval(
            "DELETE FROM tournament_participants WHERE tournament_id=$1 AND user_id=$2 RETURNING id",
            tid, user_id
        )
        if not deleted:
            raise HTTPException(400, "You're not in this tournament")

        if t["entry_fee"] > 0:
            await add_balance(user_id, float(t["entry_fee"]), conn)
            await conn.execute("UPDATE tournaments SET prize_pool = prize_pool - $1 WHERE id=$2", t["entry_fee"], tid)
            await conn.execute("""
                INSERT INTO transactions (user_id, amount, type, description)
                VALUES ($1, $2, 'tournament_refund', $3)
            """, user_id, float(t["entry_fee"]), f"Left tournament - {tid}")

        return {"ok": True}

@router.post("/{tid}/cancel")
async def cancel_tournament(tid: int, request: Request):
    """Admin moderation path for a stuck or abusive tournament -- no
    equivalent existed before this session's audit. Refunds every
    participant's entry fee, same idiom as leave_tournament."""
    user_id = await require_auth(request)
    if user_id not in ADMIN_USER_IDS:
        raise HTTPException(403, "Admin only")
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            t = await conn.fetchrow("SELECT * FROM tournaments WHERE id=$1 FOR UPDATE", tid)
            if not t:
                raise HTTPException(404, "Tournament not found")
            if t["status"] not in ("open", "active"):
                raise HTTPException(400, "Tournament cannot be cancelled from its current state")

            if t["entry_fee"] > 0:
                participants = await conn.fetch(
                    "SELECT user_id FROM tournament_participants WHERE tournament_id=$1", tid
                )
                for p in participants:
                    await add_balance(p["user_id"], float(t["entry_fee"]), conn)
                    await conn.execute("""
                        INSERT INTO transactions (user_id, amount, type, description)
                        VALUES ($1, $2, 'tournament_refund', $3)
                    """, p["user_id"], float(t["entry_fee"]), f"Tournament cancelled - {tid}")

            await conn.execute(
                "UPDATE tournaments SET status='cancelled', ended_at=NOW() WHERE id=$1", tid
            )
        return {"ok": True}

async def _start_bracket(tid: int, conn):
    """Shared bracket-seeding logic for both the admin manual /start
    trigger (non-coinflip modes) and the coinflip auto-start-on-full-queue
    path in join_tournament(). Caller must already hold a FOR UPDATE lock
    on the tournaments row (or be inside the same transaction as one)."""
    t = await conn.fetchrow("SELECT * FROM tournaments WHERE id=$1", tid)
    if not t or t["status"] != "open":
        raise HTTPException(400, "Tournament cannot be started")

    rows = await conn.fetch("SELECT user_id FROM tournament_participants WHERE tournament_id=$1", tid)
    players = [dict(r) for r in rows]
    players = secure_shuffle(players)
    n = len(players)
    if n < 2:
        raise HTTPException(400, "Need at least 2 players")

    total_rounds = math.ceil(math.log2(n))
    bracket_size = 2 ** total_rounds

    seeded = [p["user_id"] for p in players]
    while len(seeded) < bracket_size:
        seeded.append(None)

    for i, p in enumerate(players):
        await conn.execute("UPDATE tournament_participants SET seed=$1 WHERE tournament_id=$2 AND user_id=$3", i+1, tid, p["user_id"])

    match_idx = 0
    round1_match_ids = []
    for i in range(0, bracket_size, 2):
        p1 = seeded[i]
        p2 = seeded[i + 1]
        status = "pending"
        winner = None
        loser = None
        if p1 is None and p2 is not None:
            winner, loser = p2, None
            status = "completed"
        elif p2 is None and p1 is not None:
            winner, loser = p1, None
            status = "completed"
        elif p1 is None and p2 is None:
            match_idx += 1
            continue

        row = await conn.fetchrow("""
            INSERT INTO tournament_matches (tournament_id, round, match_index, player1_id, player2_id, winner_id, loser_id, status)
            VALUES ($1, 1, $2, $3, $4, $5, $6, $7)
            RETURNING id
        """, tid, match_idx, p1, p2, winner, loser, status)
        round1_match_ids.append(row["id"])
        match_idx += 1

    await conn.execute("UPDATE tournaments SET status='active', started_at=NOW() WHERE id=$1", tid)

    for rnd in range(2, total_rounds + 1):
        matches_in_round = bracket_size // (2 ** rnd)
        for mi in range(matches_in_round):
            await conn.execute("""
                INSERT INTO tournament_matches (tournament_id, round, match_index, player1_id, player2_id, status)
                VALUES ($1, $2, $3, NULL, NULL, 'pending')
            """, tid, rnd, mi)

    if t["game_mode"] == "coinflip":
        # max_players is always a power of 2 (4/8/16) and auto-start only
        # fires at exactly max_players participants, so n == bracket_size
        # always -- no bye slots, every round-1 match already has both
        # players. Resolving all of them cascades through every later
        # round too via _resolve_coinflip_match's recursion.
        for match_id in round1_match_ids:
            await _resolve_coinflip_match(tid, match_id, conn)
        return {"ok": True, "rounds": total_rounds, "players": n, "game_mode": "coinflip"}

    if t["game_mode"] in ORCHESTRATED_MODES:
        # Unlike coinflip, real-game matches take genuine wall-clock time to
        # resolve (a duel/battle actually has to be played) -- these can't
        # cascade-resolve synchronously here. The caller launches game rooms
        # for round1_match_ids AFTER this function's transaction commits
        # (see join_tournament()'s deadlock-avoidance comment), then the
        # background poll task takes over advancing the bracket as each
        # match's real game resolves.
        return {
            "ok": True, "rounds": total_rounds, "players": n,
            "game_mode": t["game_mode"], "round1_match_ids": round1_match_ids,
        }

    return {"ok": True, "rounds": total_rounds, "players": n, "game_mode": t["game_mode"]}

@router.post("/{tid}/start")
async def start_tournament(tid: int, request: Request):
    user_id = await require_auth(request)
    if user_id not in ADMIN_USER_IDS:
        raise HTTPException(403, "Admin only")
    pool = await get_db()
    async with pool.acquire() as conn:
        result = await _start_bracket(tid, conn)
    if result.get('game_mode') in ORCHESTRATED_MODES:
        await _launch_orchestrated_matches(tid, result['game_mode'], result['round1_match_ids'])
    return result

@router.post("/{tid}/matches/{match_id}/submit")
async def submit_match(tid: int, match_id: int, body: SubmitBody, request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        m = await conn.fetchrow("SELECT * FROM tournament_matches WHERE id=$1 AND tournament_id=$2 FOR UPDATE", match_id, tid)
        if not m:
            raise HTTPException(404, "Match not found")
        if m["status"] == "completed":
            raise HTTPException(400, "Match already completed")
        if m["player1_id"] != user_id and m["player2_id"] != user_id:
            raise HTTPException(403, "You are not a participant in this match")

        if body.player1_score < 0 or body.player2_score < 0:
            raise HTTPException(400, "Scores cannot be negative")
        if body.player1_score > 100 or body.player2_score > 100:
            raise HTTPException(400, "Score exceeds maximum (100)")
        if body.player1_score == body.player2_score:
            raise HTTPException(400, "Tie scores not allowed — rematch required")

        winner = m["player1_id"] if body.player1_score > body.player2_score else m["player2_id"]
        loser  = m["player2_id"] if body.player1_score > body.player2_score else m["player1_id"]

        await conn.execute("""
            UPDATE tournament_matches
            SET player1_score=$1, player2_score=$2, winner_id=$3, loser_id=$4, status='completed', played_at=NOW()
            WHERE id=$5
        """, body.player1_score, body.player2_score, winner, loser, match_id)

        await conn.execute("UPDATE tournament_participants SET current_score = current_score + $1 WHERE tournament_id=$2 AND user_id=$3",
                           body.player1_score, tid, m["player1_id"])
        await conn.execute("UPDATE tournament_participants SET current_score = current_score + $1 WHERE tournament_id=$2 AND user_id=$3",
                           body.player2_score, tid, m["player2_id"])

        await _advance_match(tid, winner, m["round"], m["match_index"], conn)

        return {"ok": True, "winner_id": winner}

async def _advance_match(tid: int, winner_id: int, round_num: int, match_index: int, conn):
    """Shared tail of match resolution: advance the winner into the next
    round's slot, and if that was the final round, mark the tournament
    completed and pay out prizes. Returns the populated next_match row (or
    None if this was the final), so callers needing cascade behavior
    (coinflip auto-resolve) can inspect whether it's now ready to resolve."""
    next_round = round_num + 1
    next_match_idx = match_index // 2
    next_match = await conn.fetchrow("""
        SELECT id, player1_id, player2_id FROM tournament_matches
        WHERE tournament_id=$1 AND round=$2 AND match_index=$3
        FOR UPDATE
    """, tid, next_round, next_match_idx)

    if next_match:
        if match_index % 2 == 0:
            await conn.execute("UPDATE tournament_matches SET player1_id=$1 WHERE id=$2", winner_id, next_match["id"])
        else:
            await conn.execute("UPDATE tournament_matches SET player2_id=$1 WHERE id=$2", winner_id, next_match["id"])

    if next_round > 1:
        remaining = await conn.fetchval("""
            SELECT COUNT(*) FROM tournament_matches
            WHERE tournament_id=$1 AND round=$2 AND status='pending'
        """, tid, next_round)
        if remaining == 0:
            final = await conn.fetchrow("""
                SELECT winner_id FROM tournament_matches
                WHERE tournament_id=$1 AND round=$2 AND match_index=0
            """, tid, next_round - 1)
            if final:
                await conn.execute("UPDATE tournaments SET status='completed', ended_at=NOW() WHERE id=$1", tid)
                t = await conn.fetchrow("SELECT prize_pool FROM tournaments WHERE id=$1", tid)
                if t and t["prize_pool"] > 0:
                    prize = float(t["prize_pool"]) * 0.7
                    await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", prize, final["winner_id"])
                    await conn.execute("""
                        INSERT INTO transactions (user_id, amount, type, description)
                        VALUES ($1, $2, 'tournament_prize', $3)
                    """, final["winner_id"], prize, f"1st place prize - {tid}")
                    await _distribute_runner_up_prizes(tid, t["prize_pool"], final["winner_id"], conn)

    return next_match

async def _resolve_coinflip_match(tid: int, match_id: int, conn):
    """Instantly flip and cascade-advance a coinflip match. Recurses into
    the next round whenever advancing this match fills both slots there, so
    a full bracket resolves in one synchronous pass from _start_bracket."""
    m = await conn.fetchrow("SELECT * FROM tournament_matches WHERE id=$1 AND tournament_id=$2 FOR UPDATE", match_id, tid)
    if not m or m["status"] == "completed" or m["player1_id"] is None or m["player2_id"] is None:
        return
    p1, p2 = m["player1_id"], m["player2_id"]
    winner = p1 if secure_random() < 0.5 else p2
    loser = p2 if winner == p1 else p1

    await conn.execute("""
        UPDATE tournament_matches SET winner_id=$1, loser_id=$2, status='completed', played_at=NOW()
        WHERE id=$3
    """, winner, loser, match_id)
    await conn.execute(
        "UPDATE tournament_participants SET current_score = current_score + 1 WHERE tournament_id=$1 AND user_id=$2",
        tid, winner
    )

    next_match = await _advance_match(tid, winner, m["round"], m["match_index"], conn)

    if next_match:
        refreshed = await conn.fetchrow("SELECT * FROM tournament_matches WHERE id=$1", next_match["id"])
        if refreshed["status"] == "pending" and refreshed["player1_id"] is not None and refreshed["player2_id"] is not None:
            await _resolve_coinflip_match(tid, refreshed["id"], conn)


# ============================================================
# SESSION 8: orchestrated (real-game) match resolution
# ============================================================

def _game_params_for_mode(game_mode: str) -> dict:
    """Per-game create_private_room() parameter shape, matching exactly
    what routes/friends.py's _create_game_room() dispatcher expects --
    every match, every round, stakes the same flat MATCH_STAKE regardless
    of the tournament's own entry_fee (locked-in decision)."""
    if game_mode == 'case_draft_duel':
        return {'entry_fee': MATCH_STAKE}
    if game_mode == 'case_battles':
        return {'fee': MATCH_STAKE, 'rounds': 3, 'win_condition': 'total_value'}
    return {'stake': MATCH_STAKE}  # dice_duel, weapon_duel, reaction_duel


async def _get_game_winner(game_mode: str, room_id: int, conn) -> Optional[int]:
    """Poll the real game's own table for a populated winner_id, rather than
    adding a callback hook into any of the 5 already-shipped game files.
    Table/column names come from the hardcoded GAME_WINNER_TABLES dict,
    never from user input."""
    table, col = GAME_WINNER_TABLES[game_mode]
    row = await conn.fetchrow(f"SELECT {col} FROM {table} WHERE id=$1", room_id)
    return row[col] if row else None


async def _settle_orchestrated_match(tid: int, match_id: int, winner_id: int, conn):
    """Same tail as _resolve_coinflip_match (winner recorded, participant
    score bumped, _advance_match() called) but for a match whose winner was
    decided by a real game or a stuck-match coinflip fallback, not the
    bracket's own coinflip mode. Does NOT recurse -- the caller decides
    whether the next round's match is ready and, if so, launches ITS game
    room (orchestrated brackets can't cascade-resolve synchronously the way
    coinflip does, since a real game takes genuine wall-clock time)."""
    m = await conn.fetchrow("SELECT * FROM tournament_matches WHERE id=$1 AND tournament_id=$2 FOR UPDATE", match_id, tid)
    if not m or m["status"] == "completed":
        return None
    p1, p2 = m["player1_id"], m["player2_id"]
    loser_id = p2 if winner_id == p1 else p1

    await conn.execute("""
        UPDATE tournament_matches SET winner_id=$1, loser_id=$2, status='completed', played_at=NOW()
        WHERE id=$3
    """, winner_id, loser_id, match_id)
    await conn.execute(
        "UPDATE tournament_participants SET current_score = current_score + 1 WHERE tournament_id=$1 AND user_id=$2",
        tid, winner_id
    )

    return await _advance_match(tid, winner_id, m["round"], m["match_index"], conn)


async def _launch_orchestrated_matches(tid: int, game_mode: str, match_ids: list):
    """Creates a real game room for each given match (via routes/friends.py's
    already-tested _create_game_room() dispatcher) and records it on the
    match row. Must only be called AFTER any transaction that was holding a
    lock on either player's user row has committed -- create_private_room()
    opens its own connection/transaction to stake each player, so calling
    this from inside such a transaction risks the exact cross-connection
    deadlock discovered in Session 4's Friends challenge system."""
    from routes.friends import _create_game_room
    pool = await get_db()
    game_params = _game_params_for_mode(game_mode)

    for match_id in match_ids:
        async with pool.acquire() as conn:
            m = await conn.fetchrow("SELECT * FROM tournament_matches WHERE id=$1", match_id)
        if not m or m["status"] != "pending" or m["player1_id"] is None or m["player2_id"] is None:
            continue  # bye match, already launched, or already settled

        next_match = None
        try:
            room_id = await _create_game_room(game_mode, [m["player1_id"], m["player2_id"]], game_params)
        except Exception as e:
            # A player couldn't cover the flat match stake (or the room
            # failed to create for any other reason) -- fall back to a
            # coinflip for just this one match rather than stalling the
            # bracket on it forever.
            logger.warning(f"Tournament {tid} match {match_id}: {game_mode} room failed ({e}), coinflip fallback")
            winner_id = m["player1_id"] if secure_random() < 0.5 else m["player2_id"]
            async with pool.acquire() as conn:
                async with conn.transaction():
                    next_match = await _settle_orchestrated_match(tid, match_id, winner_id, conn)
        else:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE tournament_matches SET game_room_id=$1, started_at=NOW(), status='in_progress' WHERE id=$2",
                    room_id, match_id
                )

        if next_match:
            async with pool.acquire() as conn:
                refreshed = await conn.fetchrow("SELECT * FROM tournament_matches WHERE id=$1", next_match["id"])
            if refreshed and refreshed["status"] == "pending" and refreshed["player1_id"] is not None and refreshed["player2_id"] is not None:
                await _launch_orchestrated_matches(tid, game_mode, [refreshed["id"]])

    _ensure_poll_task(tid, game_mode)


def _ensure_poll_task(tid: int, game_mode: str):
    existing = _tournament_poll_tasks.get(tid)
    if existing and not existing.done():
        return
    _tournament_poll_tasks[tid] = asyncio.create_task(_poll_tournament(tid, game_mode))


async def _poll_tournament(tid: int, game_mode: str):
    """Background task, one per active orchestrated tournament: every
    POLL_INTERVAL_SECS, checks each 'in_progress' match's real game room for
    a resolved winner and advances the bracket exactly like coinflip does
    (same _advance_match() tail, so prize distribution and next-round
    seeding are identical, zero duplicated logic). Falls back to a coinflip
    for any single match stuck past STUCK_MATCH_TIMEOUT_MINUTES. Exits once
    the tournament is no longer 'active' (completed or cancelled)."""
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECS)

            pool = await get_db()
            async with pool.acquire() as conn:
                t = await conn.fetchrow("SELECT status FROM tournaments WHERE id=$1", tid)
            if not t or t["status"] != "active":
                return

            async with pool.acquire() as conn:
                in_progress = await conn.fetch(
                    "SELECT * FROM tournament_matches WHERE tournament_id=$1 AND status='in_progress'", tid
                )

            for m in in_progress:
                async with pool.acquire() as conn:
                    winner_id = await _get_game_winner(game_mode, m["game_room_id"], conn)

                if winner_id is None:
                    async with pool.acquire() as conn:
                        stale = await conn.fetchval(
                            "SELECT started_at < NOW() - ($1 * INTERVAL '1 minute') FROM tournament_matches WHERE id=$2",
                            STUCK_MATCH_TIMEOUT_MINUTES, m["id"]
                        )
                    if not stale:
                        continue
                    logger.warning(f"Tournament {tid} match {m['id']} stuck past {STUCK_MATCH_TIMEOUT_MINUTES}min, coinflip fallback")
                    winner_id = m["player1_id"] if secure_random() < 0.5 else m["player2_id"]

                async with pool.acquire() as conn:
                    async with conn.transaction():
                        next_match = await _settle_orchestrated_match(tid, m["id"], winner_id, conn)

                if next_match:
                    async with pool.acquire() as conn:
                        refreshed = await conn.fetchrow("SELECT * FROM tournament_matches WHERE id=$1", next_match["id"])
                    if refreshed and refreshed["status"] == "pending" and refreshed["player1_id"] is not None and refreshed["player2_id"] is not None:
                        await _launch_orchestrated_matches(tid, game_mode, [refreshed["id"]])
    finally:
        _tournament_poll_tasks.pop(tid, None)


async def _distribute_runner_up_prizes(tid, prize_pool, winner_id, conn):
    players = await conn.fetch("""
        SELECT user_id FROM tournament_participants
        WHERE tournament_id=$1 AND user_id!=$2
        ORDER BY current_score DESC
        LIMIT 3
    """, tid, winner_id)
    remaining = float(prize_pool) * 0.3
    shares = [0.15, 0.10, 0.05]
    for i, p in enumerate(players):
        share = remaining * shares[i] / 0.3
        if share > 0:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id=$2", share, p["user_id"])
            await conn.execute("""
                INSERT INTO transactions (user_id, amount, type, description)
                VALUES ($1, $2, 'tournament_prize', $3)
            """, p["user_id"], share, f"Tournament prize - {tid}")
