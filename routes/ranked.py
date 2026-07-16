# ============================================================
# routes/ranked.py
# CS2CaseBot | Ranked Mode
#
# Competitive matchmaking layered on top of the 7 PvP games that
# already resolve to a single, unambiguous winner_id: Dice Duel,
# Weapon Duel, Reaction Duel, Case Draft Duel, Case Battles (the 5
# games Session 8's tournament orchestration already knows how to
# poll for a winner) plus Ladder Race and Mines Race (Session 9's
# two race games, which also resolve to one winner). The other 9
# multiplayer games don't have a clean single-winner outcome today
# and are deliberately out of scope.
#
# Reuses three subsystems already proven elsewhere in this codebase
# rather than re-deriving them:
#   - routes/friends.py's _create_game_room() dispatcher stakes and
#     launches a private room for any of these games.
#   - The "poll the game's own table for a winner_id" pattern from
#     routes/tournament.py's _get_game_winner()/GAME_WINNER_TABLES
#     (kept as an independent copy here, RANKED_GAME_TABLES, to
#     avoid coupling ranked mode's correctness to tournament.py).
#   - The background-poll-task-per-active-match convention from
#     routes/tournament.py's _ensure_poll_task()/_poll_tournament().
#
# Rating is a single GLOBAL Elo number per user (not per-game) --
# the player pool is small enough that fragmenting it into 7
# separate ladders would hurt match quality more than a single
# shared rating helps game-specific nuance.
# ============================================================

import asyncio
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from shared import (
    logger, get_db, require_auth, ensure_user_exists, add_balance,
)

router = APIRouter(prefix="/api/ranked", tags=["ranked"])

STARTING_RATING = 1000
K_FACTOR = 32
MATCH_STAKE = 10.0
POLL_INTERVAL_SECS = 4
STUCK_MATCH_TIMEOUT_MINUTES = 15

# Matchmaking queue tuning: start requiring opponents within +-100 rating,
# widen by 50 every 10s waited, and after a 60s hard cap pair whoever's
# waited longest with whoever's closest regardless of gap -- guarantees a
# match actually happens even with a small concurrent player pool.
INITIAL_RATING_GAP = 100
GAP_WIDEN_PER_SECS = 10
GAP_WIDEN_AMOUNT = 50
QUEUE_HARD_CAP_SECS = 60

RANKED_GAME_TABLES = {
    'dice_duel':        ('dice_duels', 'winner_id'),
    'weapon_duel':       ('weapon_duels', 'winner_id'),
    'reaction_duel':     ('reaction_duels', 'winner_id'),
    'case_draft_duel':   ('case_draft_duels', 'winner_id'),
    'case_battles':      ('case_battles', 'winner_id'),
    'ladder_race':       ('ladder_race_rounds', 'winner_id'),
    'mines_race':        ('mines_race_rounds', 'winner_id'),
}

TIER_THRESHOLDS = [
    (1800, 'Diamond'), (1500, 'Platinum'), (1200, 'Gold'), (900, 'Silver'), (0, 'Bronze'),
]


def rating_tier(rating: int) -> str:
    for threshold, name in TIER_THRESHOLDS:
        if rating >= threshold:
            return name
    return 'Bronze'


def elo_delta(my_rating: int, opp_rating: int, won: bool) -> int:
    """Standard Elo: expected score from the logistic curve, K=32."""
    expected = 1 / (1 + 10 ** ((opp_rating - my_rating) / 400))
    actual = 1.0 if won else 0.0
    return round(K_FACTOR * (actual - expected))


def _ranked_game_params(game_type: str) -> dict:
    """Per-game create_private_room() parameter shape, mirroring
    routes/friends.py's _create_game_room() dispatcher expectations --
    kept as an independent function rather than importing
    tournament.py's _game_params_for_mode() for the same decoupling
    reason as RANKED_GAME_TABLES above."""
    if game_type == 'case_draft_duel':
        return {'entry_fee': MATCH_STAKE}
    if game_type == 'case_battles':
        return {'fee': MATCH_STAKE, 'rounds': 3, 'win_condition': 'total_value'}
    return {'stake': MATCH_STAKE}


# ============================================================
# TABLE SETUP
# ============================================================

async def init_ranked_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ranked_ratings (
                user_id       BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                rating        INTEGER DEFAULT 1000,
                wins          INTEGER DEFAULT 0,
                losses        INTEGER DEFAULT 0,
                games_played  INTEGER DEFAULT 0,
                updated_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ranked_matches (
                id                    SERIAL PRIMARY KEY,
                game_type             TEXT NOT NULL,
                player1_id            BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                player2_id            BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                winner_id             BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
                game_room_id          INTEGER,
                player1_rating_before INTEGER,
                player2_rating_before INTEGER,
                rating_change         INTEGER,
                status                TEXT DEFAULT 'in_progress'
                                      CHECK (status IN ('in_progress','completed','cancelled')),
                created_at            TIMESTAMP DEFAULT NOW(),
                started_at            TIMESTAMP,
                resolved_at           TIMESTAMP
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ranked_ratings_rating ON ranked_ratings(rating)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ranked_matches_status ON ranked_matches(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ranked_matches_player1 ON ranked_matches(player1_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ranked_matches_player2 ON ranked_matches(player2_id)")
    logger.info("✅ Ranked mode tables ready")


async def _ensure_rating_row(user_id: int, conn) -> int:
    row = await conn.fetchrow("SELECT rating FROM ranked_ratings WHERE user_id=$1", user_id)
    if row:
        return row['rating']
    await conn.execute(
        "INSERT INTO ranked_ratings (user_id, rating) VALUES ($1,$2) ON CONFLICT (user_id) DO NOTHING",
        user_id, STARTING_RATING
    )
    return STARTING_RATING


# ============================================================
# QUEUE
# ============================================================

class QueueEntry:
    __slots__ = ('user_id', 'rating', 'joined_at')

    def __init__(self, user_id: int, rating: int):
        self.user_id = user_id
        self.rating = rating
        self.joined_at = time.time()


_queues: Dict[str, List[QueueEntry]] = {g: [] for g in RANKED_GAME_TABLES}
_queue_lock = asyncio.Lock()
_matchmaking_tasks: Dict[str, asyncio.Task] = {}
_match_poll_tasks: Dict[int, asyncio.Task] = {}


def _ensure_matchmaking_task(game_type: str):
    existing = _matchmaking_tasks.get(game_type)
    if existing and not existing.done():
        return
    _matchmaking_tasks[game_type] = asyncio.create_task(_run_matchmaking(game_type))


async def _run_matchmaking(game_type: str):
    """Runs continuously while players are queued for this game_type,
    pairing the closest-rated two once they're within an acceptable
    (and time-widening) rating gap. Exits once the queue empties."""
    try:
        while True:
            await asyncio.sleep(1.0)
            async with _queue_lock:
                q = _queues[game_type]
                if len(q) < 2:
                    if not q:
                        return
                    continue
                q.sort(key=lambda e: e.rating)
                now = time.time()
                paired = None
                for i in range(len(q) - 1):
                    a, b = q[i], q[i + 1]
                    waited = max(now - a.joined_at, now - b.joined_at)
                    gap = INITIAL_RATING_GAP + (waited // GAP_WIDEN_PER_SECS) * GAP_WIDEN_AMOUNT
                    if abs(a.rating - b.rating) <= gap or waited >= QUEUE_HARD_CAP_SECS:
                        paired = (a, b)
                        break
                if not paired:
                    continue
                a, b = paired
                q.remove(a)
                q.remove(b)
            await _start_ranked_match(game_type, a.user_id, b.user_id)
    except Exception as e:
        logger.error(f"Ranked matchmaking task for {game_type} crashed: {e}")


async def _start_ranked_match(game_type: str, uid1: int, uid2: int):
    from routes.friends import _create_game_room

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            r1 = await _ensure_rating_row(uid1, conn)
            r2 = await _ensure_rating_row(uid2, conn)
            match_row = await conn.fetchrow("""
                INSERT INTO ranked_matches
                    (game_type, player1_id, player2_id, player1_rating_before, player2_rating_before, status)
                VALUES ($1,$2,$3,$4,$5,'in_progress')
                RETURNING id
            """, game_type, uid1, uid2, r1, r2)
            match_id = match_row['id']

    try:
        room_id = await _create_game_room(game_type, [uid1, uid2], _ranked_game_params(game_type))
    except Exception as e:
        logger.warning(f"Ranked match {match_id} failed to start a real room ({e}); cancelling with no stake taken")
        async with pool.acquire() as conn:
            await conn.execute("UPDATE ranked_matches SET status='cancelled' WHERE id=$1", match_id)
        return

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE ranked_matches SET game_room_id=$1, started_at=NOW() WHERE id=$2
        """, room_id, match_id)
    _ensure_match_poll_task(match_id, game_type)


def _ensure_match_poll_task(match_id: int, game_type: str):
    existing = _match_poll_tasks.get(match_id)
    if existing and not existing.done():
        return
    _match_poll_tasks[match_id] = asyncio.create_task(_poll_match(match_id, game_type))


async def _get_game_winner(game_type: str, room_id: int, conn) -> Optional[int]:
    table, col = RANKED_GAME_TABLES[game_type]
    row = await conn.fetchrow(f"SELECT {col} FROM {table} WHERE id=$1", room_id)
    return row[col] if row else None


async def _poll_match(match_id: int, game_type: str):
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECS)

            pool = await get_db()
            async with pool.acquire() as conn:
                match = await conn.fetchrow("SELECT * FROM ranked_matches WHERE id=$1", match_id)
                if not match or match['status'] != 'in_progress':
                    return

                winner_id = await _get_game_winner(game_type, match['game_room_id'], conn)

                if winner_id is None:
                    stale = await conn.fetchval(
                        "SELECT started_at < NOW() - ($1 * INTERVAL '1 minute') FROM ranked_matches WHERE id=$2",
                        STUCK_MATCH_TIMEOUT_MINUTES, match_id
                    )
                    if stale:
                        # No-contest cancel + refund, not a coinflip -- there's no
                        # "someone must advance" pressure like a tournament bracket,
                        # so refunding is simpler and fairer than forcing a result.
                        async with conn.transaction():
                            await add_balance(match['player1_id'], MATCH_STAKE, conn)
                            await add_balance(match['player2_id'], MATCH_STAKE, conn)
                            await conn.execute(
                                "UPDATE ranked_matches SET status='cancelled', resolved_at=NOW() WHERE id=$1",
                                match_id
                            )
                        logger.info(f"🏳️ Ranked match {match_id} ({game_type}) stuck >{STUCK_MATCH_TIMEOUT_MINUTES}m, cancelled + refunded")
                        return
                    continue

                loser_id = match['player2_id'] if winner_id == match['player1_id'] else match['player1_id']
                r1_before = match['player1_rating_before']
                r2_before = match['player2_rating_before']
                winner_before = r1_before if winner_id == match['player1_id'] else r2_before
                loser_before = r2_before if winner_id == match['player1_id'] else r1_before

                delta = elo_delta(winner_before, loser_before, won=True)
                delta = max(delta, 1)   # a win always gains at least 1 point

                async with conn.transaction():
                    await conn.execute("""
                        UPDATE ranked_ratings SET rating = rating + $2, wins = wins + 1,
                            games_played = games_played + 1, updated_at = NOW()
                        WHERE user_id = $1
                    """, winner_id, delta)
                    await conn.execute("""
                        UPDATE ranked_ratings SET rating = GREATEST(0, rating - $2), losses = losses + 1,
                            games_played = games_played + 1, updated_at = NOW()
                        WHERE user_id = $1
                    """, loser_id, delta)
                    await conn.execute("""
                        UPDATE ranked_matches
                        SET status='completed', winner_id=$1, rating_change=$2, resolved_at=NOW()
                        WHERE id=$3
                    """, winner_id, delta, match_id)
                logger.info(f"🏆 Ranked match {match_id} ({game_type}) resolved: winner={winner_id} Δ={delta}")
                return
    finally:
        _match_poll_tasks.pop(match_id, None)


async def recover_stale_ranked_matches():
    """Startup crash-recovery: refund any match that never got a real
    game room (matchmaking state is lost on restart, so it can never
    resolve)."""
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch(
                "SELECT id, player1_id, player2_id FROM ranked_matches WHERE status='in_progress' AND game_room_id IS NULL FOR UPDATE"
            )
            for row in stale:
                await add_balance(row['player1_id'], MATCH_STAKE, conn)
                await add_balance(row['player2_id'], MATCH_STAKE, conn)
                await conn.execute(
                    "UPDATE ranked_matches SET status='cancelled', resolved_at=NOW() WHERE id=$1", row['id']
                )
            if stale:
                logger.info(f"🏆 Recovered {len(stale)} stale ranked match(es) with no room, refunded both stakes")


async def recover_stale_ranked_polls():
    """Re-arm poll tasks for matches that already have a real game room
    but whose in-memory poll task was lost on restart."""
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, game_type FROM ranked_matches WHERE status='in_progress' AND game_room_id IS NOT NULL"
        )
    for row in rows:
        _ensure_match_poll_task(row['id'], row['game_type'])
    if rows:
        logger.info(f"🏆 Re-armed {len(rows)} ranked match poll task(s) after restart")


# ============================================================
# REST ROUTES
# ============================================================

class QueueJoinRequest(BaseModel):
    game_type: str


@router.post("/queue/join")
async def queue_join(req: QueueJoinRequest, request: Request):
    user_id = await require_auth(request)
    await ensure_user_exists(user_id)

    if req.game_type not in RANKED_GAME_TABLES:
        raise HTTPException(400, f"'{req.game_type}' is not a ranked-eligible game")

    pool = await get_db()
    async with pool.acquire() as conn:
        rating = await _ensure_rating_row(user_id, conn)

    async with _queue_lock:
        for game_type, q in _queues.items():
            if any(e.user_id == user_id for e in q):
                raise HTTPException(400, "You're already queued for a ranked match")
        _queues[req.game_type].append(QueueEntry(user_id, rating))

    _ensure_matchmaking_task(req.game_type)
    return {"success": True, "game_type": req.game_type, "rating": rating}


@router.post("/queue/leave")
async def queue_leave(request: Request):
    user_id = await require_auth(request)
    removed = False
    async with _queue_lock:
        for q in _queues.values():
            before = len(q)
            q[:] = [e for e in q if e.user_id != user_id]
            if len(q) != before:
                removed = True
    return {"success": True, "removed": removed}


@router.get("/queue/status")
async def queue_status(request: Request):
    user_id = await require_auth(request)
    async with _queue_lock:
        for game_type, q in _queues.items():
            for e in q:
                if e.user_id == user_id:
                    return {"queued": True, "game_type": game_type, "waited_secs": round(time.time() - e.joined_at, 1)}

    pool = await get_db()
    async with pool.acquire() as conn:
        match = await conn.fetchrow("""
            SELECT * FROM ranked_matches
            WHERE (player1_id=$1 OR player2_id=$1) AND status='in_progress'
            ORDER BY created_at DESC LIMIT 1
        """, user_id)
    if match:
        return {
            "queued": False, "matched": True, "game_type": match['game_type'],
            "game_room_id": match['game_room_id'],
        }
    return {"queued": False, "matched": False}


@router.get("/me")
async def ranked_me(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rating = await _ensure_rating_row(user_id, conn)
        row = await conn.fetchrow("SELECT * FROM ranked_ratings WHERE user_id=$1", user_id)
        rank_position = await conn.fetchval(
            "SELECT COUNT(*) + 1 FROM ranked_ratings WHERE rating > $1", row['rating']
        )
    return {
        "rating": row['rating'], "tier": rating_tier(row['rating']),
        "wins": row['wins'], "losses": row['losses'], "games_played": row['games_played'],
        "rank_position": rank_position,
    }


@router.get("/leaderboard")
async def ranked_leaderboard(limit: int = 25):
    limit = max(1, min(limit, 100))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT r.user_id, u.username, r.rating, r.wins, r.losses, r.games_played
            FROM ranked_ratings r
            JOIN users u ON u.user_id = r.user_id
            WHERE r.games_played > 0
            ORDER BY r.rating DESC
            LIMIT $1
        """, limit)
    return {"users": [
        {**dict(r), "tier": rating_tier(r['rating'])} for r in rows
    ]}


@router.get("/history")
async def ranked_history(request: Request, limit: int = 20):
    user_id = await require_auth(request)
    limit = max(1, min(limit, 50))
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT m.*, u1.username AS p1_username, u2.username AS p2_username
            FROM ranked_matches m
            LEFT JOIN users u1 ON u1.user_id = m.player1_id
            LEFT JOIN users u2 ON u2.user_id = m.player2_id
            WHERE (m.player1_id=$1 OR m.player2_id=$1) AND m.status != 'in_progress'
            ORDER BY m.created_at DESC
            LIMIT $2
        """, user_id, limit)
    return {"matches": [
        {
            'match_id': r['id'], 'game_type': r['game_type'], 'status': r['status'],
            'opponent': r['p2_username'] if r['player1_id'] == user_id else r['p1_username'],
            'won': r['winner_id'] == user_id if r['winner_id'] else None,
            'rating_change': r['rating_change'],
            'resolved_at': r['resolved_at'].isoformat() if r['resolved_at'] else None,
        }
        for r in rows
    ]}
