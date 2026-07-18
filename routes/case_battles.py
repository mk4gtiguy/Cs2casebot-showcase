# ============================================================
# routes/case_battles.py
# CS2CaseBot | Case Battles — PvP + PvE + Matchmaking
#
# KEY FIX: Server is the ONLY clock.
#   - Server broadcasts {type:"round_tick", seconds_remaining:N} every second
#   - Client ONLY displays what server sends — no client-side countdown
#   - Dead WS connections caught by try/except in broadcast, never .closed
# ============================================================

import asyncio
from datetime import datetime
from typing import Dict, Set, Optional, List, Any

from fastapi import (
    APIRouter, WebSocket, WebSocketDisconnect,
    Depends, HTTPException, Request, Body
)
from pydantic import BaseModel

import shared
from shared import (
    CASES, RARITY_EMOJIS, get_db, logger,
    generate_skin_float, get_skin_condition, calculate_item_value,
    DROP_RATES, get_random_item, get_user_id_from_session,
    require_admin, ADMIN_USER_IDS, BOT_IDS, BOT_NAMES,
    convert_decimals, broadcast_to_set,
    secure_random, secure_randint, secure_choice, secure_shuffle, log_game,
)

# ─── Router ─────────────────────────────────────────────────
router = APIRouter(prefix="/api/battles", tags=["battles"])

# ─── Round settings ─────────────────────────────────────────
ROUND_DURATION_SECONDS = 15   # how long each player has to open
BETWEEN_ROUND_SECONDS  = 3    # pause between rounds

# Terminal cases (offer-based buy/skip a sequence of priced items -- not a
# single random roll) and souvenir packages (can't drop Golds, novelty
# items) aren't valid case-battle cases -- mirrors the same exclusion
# static/battle-setup.html applies to its case picker. Enforced here too
# since case_id comes straight off the client's websocket message.
BATTLE_EXCLUDED_CASE_IDS = {
    cid for cid, c in CASES.items()
    if cid == 'dead_hand_terminal' or c.get('category') == 'souvenir'
}
BATTLE_ELIGIBLE_CASE_IDS = [cid for cid in CASES.keys() if cid not in BATTLE_EXCLUDED_CASE_IDS]

# ============================================================
# PYDANTIC MODELS
# ============================================================

class BattleSettingsUpdate(BaseModel):
    enabled: bool
    fee_tiers: List[float]
    round_options: List[int]

class QueueRequest(BaseModel):
    fee: float
    rounds: int = 3
    win_condition: str = "total_value"
    player_count: int = 2   # 2, 3, 4 for FFA; 4, 6, 8 for team (team_size>1)
    team_size: int = 1      # 1=FFA, 2/3/4 for team battles
    inventory_id: Optional[int] = None   # stake an item instead of paying the cash fee

class PvERequest(BaseModel):
    fee: float
    rounds: int = 3
    win_condition: str = "total_value"
    difficulty: str = "normal"
    inventory_id: Optional[int] = None   # stake an item instead of paying the cash fee

# ============================================================
# BATTLE MANAGER
# ============================================================

class BattleManager:
    def __init__(self):
        # (fee, player_count, team_size) → asyncio.Queue of waiting players
        self.queues:         Dict[tuple, asyncio.Queue] = {}
        # user_ids currently waiting in any queue (prevents self-match / double-deduction)
        self.queued_users:   Set[int]                   = set()
        # battle_id → room state dict
        self.rooms:          Dict[int, Dict]            = {}
        # battle_id → set of active WebSockets
        self.ws_connections: Dict[int, Set[WebSocket]]  = {}
        # battle_id → running tick task
        self.tick_tasks:     Dict[int, asyncio.Task]    = {}
        # user_id → matchmaking WebSocket
        self.matchmaking_ws: Dict[int, WebSocket]       = {}

        self.matchmaking_task: Optional[asyncio.Task] = None
        self._shutdown = False

    # ── Queue helpers ──────────────────────────────────────
    def get_queue(self, fee: float, player_count: int = 2, team_size: int = 1) -> asyncio.Queue:
        key = (fee, player_count, team_size)
        if key not in self.queues:
            self.queues[key] = asyncio.Queue()
        return self.queues[key]

    # ── WS registry ───────────────────────────────────────
    def add_ws(self, battle_id: int, ws: WebSocket):
        self.ws_connections.setdefault(battle_id, set()).add(ws)

    def remove_ws(self, battle_id: int, ws: WebSocket):
        self.ws_connections.get(battle_id, set()).discard(ws)

    # ── Broadcast ─────────────────────────────────────────
    async def broadcast(self, battle_id: int, message: dict):
        """
        Send JSON to every WS in the battle room.
        Dead sockets are removed via try/except — never trust .closed.
        """
        ws_set = self.ws_connections.get(battle_id, set())
        dead = await broadcast_to_set(ws_set, message)
        ws_set -= dead

    # ── Matchmaking loop ──────────────────────────────────
    async def start_matchmaking(self):
        while not self._shutdown:
            await asyncio.sleep(2)
            try:
                pool = await get_db()
                async with pool.acquire() as conn:
                    # Iterate over all active queues keyed by (fee, player_count, team_size)
                    for key, queue in list(self.queues.items()):
                        fee, player_count, team_size = key
                        if queue.qsize() < player_count:
                            continue
                        players = [await queue.get() for _ in range(player_count)]
                        for p in players:
                            self.queued_users.discard(p['user_id'])
                        battle_id = await self._create_pvp_battle(
                            conn, players, float(fee)
                        )
                        if battle_id:
                            for p in players:
                                await self._notify_match_found(p['ws'], battle_id, p)
                        else:
                            for p in players:
                                try:
                                    await p['ws'].send_json({
                                        'type':    'match_failed',
                                        'message': 'Match could not be created — '
                                                   'please re-join the queue.',
                                    })
                                except Exception:
                                    pass
            except Exception as e:
                logger.error(f"Matchmaking loop error: {e}")

    async def _create_pvp_battle(self, conn, players: list, fee: float) -> Optional[int]:
        """Create a PvP battle for 2–8 players (FFA or team modes)."""
        try:
            rounds        = players[0].get('rounds', 3)
            win_condition = players[0].get('win_condition', 'total_value')
            player_count  = len(players)
            team_size     = int(players[0].get('team_size', 1))

            async with conn.transaction():
                # user_id -> (staked_inventory_id, staked_value), for players
                # who staked an item instead of paying the cash fee.
                staked: Dict[int, tuple] = {}
                for player in players:
                    inv_id = player.get('inventory_id')
                    if inv_id:
                        # Same atomic ownership+status+min-value guard the
                        # item-wager games use -- a concurrent stake attempt
                        # on the same item elsewhere can never succeed twice.
                        item = await conn.fetchrow("""
                            UPDATE inventory SET status='staked'
                            WHERE id=$1 AND user_id=$2 AND status='kept' AND protected = FALSE AND price >= $3
                            RETURNING price
                        """, inv_id, player['user_id'], fee)
                        if not item:
                            raise ValueError(f"Staked item no longer available for user {player['user_id']}")
                        staked[player['user_id']] = (inv_id, float(item['price']))
                    else:
                        deducted = await conn.fetchval("""
                            UPDATE users SET balance = balance - $1
                            WHERE user_id = $2 AND balance >= $1
                            RETURNING user_id
                        """, fee, player['user_id'])
                        if not deducted:
                            raise ValueError(f"Insufficient balance for user {player['user_id']}")

                battle_id = await conn.fetchval("""
                    INSERT INTO case_battles
                        (battle_type, status, entry_fee, total_rounds,
                         win_condition, player_count, team_size)
                    VALUES ('pvp', 'waiting', $1, $2, $3, $4, $5)
                    RETURNING id
                """, fee, rounds, win_condition, player_count, team_size)

                for i, player in enumerate(players):
                    team_id = (i // team_size) + 1  # 1-indexed team
                    stake_inv_id, stake_value = staked.get(player['user_id'], (None, None))
                    await conn.execute("""
                        INSERT INTO case_battle_participants
                            (battle_id, user_id, team_id, staked_inventory_id, staked_value)
                        VALUES ($1, $2, $3, $4, $5)
                    """, battle_id, player['user_id'], team_id, stake_inv_id, stake_value)

            self.rooms[battle_id] = {
                'status':       'waiting',
                'battle_type':  'pvp',
                'players': {
                    p['user_id']: {'ws': p['ws'], 'ready': False,
                                   'team_id': (i // team_size) + 1}
                    for i, p in enumerate(players)
                },
                'round':        0,
                'total_rounds': rounds,
                'win_condition': win_condition,
                'team_size':    team_size,
                'difficulty':   None,
            }
            self.ws_connections[battle_id] = {p['ws'] for p in players}
            return battle_id
        except Exception as e:
            logger.error(f"Create PvP battle error: {e}")
            return None

    async def _notify_match_found(self, ws: WebSocket, battle_id: int, player: dict):
        try:
            await ws.send_json({
                'type':      'match_found',
                'battle_id': battle_id,
                'fee':       player.get('fee'),
                'rounds':    player.get('rounds', 3),
                'win':       player.get('win_condition', 'total_value'),
            })
        except Exception:
            pass

    # ── Start battle ──────────────────────────────────────
    async def start_battle(self, battle_id: int, conn):
        # Guard against concurrent WebSocket connections both seeing 'waiting'
        # and both trying to start the battle. Only one UPDATE can win.
        updated = await conn.fetchval("""
            UPDATE case_battles
            SET status = 'active', started_at = NOW()
            WHERE id = $1 AND status = 'waiting'
            RETURNING id
        """, battle_id)
        if not updated:
            return  # Already started by a concurrent connection

        room = self.rooms.get(battle_id)
        if room:
            room['status'] = 'active'

        await self.broadcast(battle_id, {
            'type':         'battle_started',
            'total_rounds': room['total_rounds'] if room else 0,
        })

        # Kick off round 1 tick loop
        self._start_tick_loop(battle_id, 1)

    # ── Tick loop (THE clock — replaces client timer) ─────
    def _start_tick_loop(self, battle_id: int, round_num: int):
        """Cancel any existing tick loop and start a new one for round_num."""
        self._cancel_tick(battle_id)
        task = asyncio.create_task(
            self._tick_loop(battle_id, round_num)
        )
        self.tick_tasks[battle_id] = task

    def _cancel_tick(self, battle_id: int):
        task = self.tick_tasks.pop(battle_id, None)
        if task and not task.done():
            task.cancel()

    async def _tick_loop(self, battle_id: int, round_num: int):
        """
        Broadcast a tick every second for ROUND_DURATION_SECONDS.
        If time expires, auto-open for any player who hasn't opened yet.
        """
        try:
            # Announce new round
            await self.broadcast(battle_id, {
                'type':             'round_start',
                'round':            round_num,
                'seconds_total':    ROUND_DURATION_SECONDS,
                'seconds_remaining': ROUND_DURATION_SECONDS,
            })

            for seconds_left in range(ROUND_DURATION_SECONDS, 0, -1):
                await asyncio.sleep(1)
                await self.broadcast(battle_id, {
                    'type':             'round_tick',
                    'round':            round_num,
                    'seconds_remaining': seconds_left - 1,
                })

            # Time's up — force any missing opens
            pool = await get_db()
            async with pool.acquire() as conn:
                battle = await conn.fetchrow(
                    "SELECT status FROM case_battles WHERE id = $1", battle_id
                )
                if not battle or battle['status'] != 'active':
                    return

                missed = await conn.fetch("""
                    SELECT cp.id, cp.user_id, cp.is_bot, cp.bot_difficulty
                    FROM case_battle_participants cp
                    LEFT JOIN case_battle_rounds cr
                        ON cp.id = cr.participant_id AND cr.round_number = $1
                    WHERE cp.battle_id = $2 AND cr.id IS NULL
                """, round_num, battle_id)

                for p in missed:
                    if p['is_bot']:
                        await self._bot_open(
                            battle_id, p['id'], round_num,
                            p['bot_difficulty'] or 'normal', conn
                        )
                    else:
                        # Player missed — record zero and advance their round
                        # counter so they are not stuck replaying the same round.
                        async with conn.transaction():
                            await conn.execute("""
                                INSERT INTO case_battle_rounds
                                    (battle_id, participant_id, round_number,
                                     item_name, rarity, value)
                                VALUES ($1, $2, $3, 'Missed Round', 'Blue', 0)
                                ON CONFLICT (participant_id, round_number) DO NOTHING
                            """, battle_id, p['id'], round_num)
                            await conn.execute("""
                                UPDATE case_battle_participants
                                SET current_round = $1
                                WHERE id = $2 AND current_round < $1
                            """, round_num, p['id'])
                        user_row = await conn.fetchrow(
                            "SELECT username FROM users WHERE user_id = $1", p['user_id']
                        )
                        await self.broadcast(battle_id, {
                            'type':     'round_missed',
                            'user_id':  p['user_id'],
                            'username': user_row['username'] if user_row else 'Unknown',
                            'round':    round_num,
                        })

                await self._check_and_advance(battle_id, round_num, conn)

        except asyncio.CancelledError:
            pass  # Round completed early — tick cancelled intentionally
        except Exception as e:
            logger.error(f"Tick loop error battle {battle_id} round {round_num}: {e}")

    # ── Bot open ──────────────────────────────────────────
    async def _bot_open(
        self, battle_id: int, participant_id: int,
        round_num: int, difficulty: str, conn
    ):
        item = self._get_bot_item(difficulty)
        await conn.execute("""
            INSERT INTO case_battle_rounds
                (battle_id, participant_id, round_number,
                 item_name, rarity, value, is_stattrak, float_value)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (participant_id, round_number) DO NOTHING
        """, battle_id, participant_id, round_num,
            item['name'], item['rarity'], item['price'],
            item.get('is_stattrak', False), item.get('float', 0.0))

        await conn.execute("""
            UPDATE case_battle_participants
            SET total_value     = total_value + $1,
                gold_count      = gold_count + CASE WHEN $2='Gold' THEN 1 ELSE 0 END,
                best_item_value = GREATEST(best_item_value, $1),
                current_round   = $3
            WHERE id = $4
        """, item['price'], item['rarity'], round_num, participant_id)

        await self.broadcast(battle_id, {
            'type':     'round_update',
            'user_id':  -1,
            'username': f'🤖 Bot ({difficulty})',
            'round':    round_num,
            'item':     item,
            'value':    float(item['price']),
        })

    def _get_bot_item(self, difficulty: str) -> dict:
        """Roll a case item with difficulty-adjusted drop rates."""
        case_id = secure_choice(BATTLE_ELIGIBLE_CASE_IDS)
        rates = DROP_RATES.copy()

        if difficulty == 'hard':
            rates['Blue']   = max(0,   rates['Blue']   - 15)
            rates['Purple'] = min(100, rates['Purple'] + 8)
            rates['Pink']   = min(100, rates['Pink']   + 4)
            rates['Red']    = min(100, rates['Red']    + 2)
            rates['Gold']   = min(100, rates['Gold']   + 1)
        elif difficulty == 'expert':
            rates['Blue']   = max(0,   rates['Blue']   - 30)
            rates['Purple'] = min(100, rates['Purple'] + 15)
            rates['Pink']   = min(100, rates['Pink']   + 8)
            rates['Red']    = min(100, rates['Red']    + 4)
            rates['Gold']   = min(100, rates['Gold']   + 3)

        total = sum(rates.values())
        rates = {k: (v / total) * 100 for k, v in rates.items()}

        case = CASES.get(case_id, {})
        rand = secure_random() * 100
        cum  = 0.0
        for rarity, chance in rates.items():
            cum += chance
            if rand <= cum:
                possible = [i for i in case.get('items', []) if i['rarity'] == rarity]
                if possible:
                    tmpl     = secure_choice(possible)
                    is_st    = secure_random() < 0.1
                    fv       = generate_skin_float()
                    cond     = get_skin_condition(fv)
                    tier     = tmpl.get('tier')
                    price    = calculate_item_value(rarity, cond, tier, is_st)
                    name     = f"{'StatTrak™ ' if is_st else ''}{tmpl['name']}"
                    return {
                        'name':         name,
                        'display_name': f"{RARITY_EMOJIS.get(rarity,'')} {name}",
                        'rarity':       rarity,
                        'price':        price,
                        'float':        fv,
                        'condition':    cond,
                        'is_stattrak':  is_st,
                    }
        # Fallback
        item = get_random_item(case_id)
        return item or {'name': 'Mystery Item', 'rarity': 'Blue', 'price': 0.25,
                        'is_stattrak': False, 'float': 0.0, 'condition': 'Field-Tested',
                        'display_name': '🟦 Mystery Item'}

    # ── Check round completion & advance ──────────────────
    async def _check_and_advance(self, battle_id: int, round_num: int, conn):
        rows = await conn.fetch("""
            SELECT cp.id, cr.id AS round_row
            FROM case_battle_participants cp
            LEFT JOIN case_battle_rounds cr
                ON cp.id = cr.participant_id AND cr.round_number = $1
            WHERE cp.battle_id = $2
        """, round_num, battle_id)

        if not all(r['round_row'] is not None for r in rows):
            return  # Not everyone has opened yet

        # Everyone opened — cancel any remaining tick
        self._cancel_tick(battle_id)

        battle = await conn.fetchrow(
            "SELECT total_rounds FROM case_battles WHERE id = $1", battle_id
        )
        if round_num >= battle['total_rounds']:
            await self._finish_battle(battle_id, conn)
        else:
            # Brief inter-round pause, then next tick loop
            await asyncio.sleep(BETWEEN_ROUND_SECONDS)
            still_active = await conn.fetchval(
                "SELECT status FROM case_battles WHERE id = $1", battle_id
            )
            if still_active == 'active':
                self._start_tick_loop(battle_id, round_num + 1)

    # ── Finish battle ─────────────────────────────────────
    async def _transfer_staked_items(self, conn, battle_id: int, winner_user_id: int):
        """Pay out every item-staked entry fee in this battle on top of the
        cash pool -- same atomic transfer idiom the item-wager games use.
        Team battles credit every staked item to the single team
        representative in `winner`, same simplification the cash prize split
        doesn't need but items do (an item can't be split across members).
        If a bot won (PvE loss), staked items are lost to the house instead
        of transferred, same semantics as an Item vs House Jackpot loss --
        bots can't own inventory rows."""
        rows = await conn.fetch(
            "SELECT staked_inventory_id FROM case_battle_participants WHERE battle_id=$1 AND staked_inventory_id IS NOT NULL",
            battle_id
        )
        item_ids = [r['staked_inventory_id'] for r in rows]
        if not item_ids:
            return
        if winner_user_id > 0:
            await conn.execute(
                "UPDATE inventory SET user_id=$1, status='kept' WHERE id = ANY($2::int[]) AND status='staked'",
                winner_user_id, item_ids
            )
        else:
            await conn.execute(
                "UPDATE inventory SET status='sold' WHERE id = ANY($1::int[]) AND status='staked'",
                item_ids
            )

    async def _finish_battle(self, battle_id: int, conn):
        participants = await conn.fetch("""
            SELECT user_id, total_value, gold_count, best_item_value, is_bot, team_id
            FROM case_battle_participants
            WHERE battle_id = $1
        """, battle_id)

        battle = await conn.fetchrow(
            "SELECT win_condition, entry_fee, battle_type, team_size FROM case_battles WHERE id = $1",
            battle_id
        )
        win_condition = battle['win_condition']
        team_size     = int(battle['team_size'] or 1)

        col_map = {'best_item': 'best_item_value'}
        primary_col = col_map.get(win_condition, win_condition)

        entry_fee    = float(battle['entry_fee'])
        # Only real players contribute to the pool (bots don't pay)
        real_participants = [p for p in participants if not p.get('is_bot')]
        total_pool   = round(entry_fee * max(1, len(real_participants)) * 0.95, 2)

        if team_size > 1:
            # ── Team battle: group by team_id, highest team score wins ──
            teams: Dict[int, Any] = {}
            for p in participants:
                tid = p['team_id'] or 1
                if tid not in teams:
                    teams[tid] = {'score': 0.0, 'members': []}
                teams[tid]['score'] += float(p[primary_col])
                teams[tid]['members'].append(p)

            winning_team_id  = max(teams, key=lambda t: teams[t]['score'])
            winning_team     = teams[winning_team_id]
            prize_per_member = round(total_pool / team_size, 2)
            winner           = winning_team['members'][0]

            async with conn.transaction():
                updated = await conn.fetchval("""
                    UPDATE case_battles SET status='completed', ended_at=NOW(), winner_id=$1
                    WHERE id=$2 AND status='active' RETURNING id
                """, winner['user_id'], battle_id)
                if not updated:
                    return
                for member in winning_team['members']:
                    if member['user_id'] > 0:
                        await conn.execute(
                            "UPDATE users SET balance=balance+$1 WHERE user_id=$2",
                            prize_per_member, member['user_id']
                        )
                await self._transfer_staked_items(conn, battle_id, winner['user_id'])
                winning_uids = {m['user_id'] for m in winning_team['members']}
                for p in real_participants:
                    await log_game(conn, p['user_id'], 'case_battles', entry_fee,
                                   prize_per_member if p['user_id'] in winning_uids else 0.0,
                                   {'battle_id': battle_id, 'team_size': team_size})
            prize = prize_per_member
        else:
            # ── FFA / 1v1: individual highest score wins ──
            def sort_key(p):
                primary   = float(p[primary_col])
                secondary = float(p['gold_count'] if win_condition != 'gold_count' else p['total_value'])
                return (primary, secondary)

            sorted_p = sorted(participants, key=sort_key, reverse=True)
            winner   = sorted_p[0]
            prize    = total_pool

            async with conn.transaction():
                updated = await conn.fetchval("""
                    UPDATE case_battles
                    SET status = 'completed', ended_at = NOW(), winner_id = $1
                    WHERE id = $2 AND status = 'active'
                    RETURNING id
                """, winner['user_id'], battle_id)
                if not updated:
                    return
                if winner['user_id'] > 0:
                    await conn.execute(
                        "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                        prize, winner['user_id']
                    )
                await self._transfer_staked_items(conn, battle_id, winner['user_id'])
                for p in real_participants:
                    await log_game(conn, p['user_id'], 'case_battles', entry_fee,
                                   prize if p['user_id'] == winner['user_id'] else 0.0,
                                   {'battle_id': battle_id})

        winner_row = await conn.fetchrow(
            "SELECT username FROM users WHERE user_id = $1", winner['user_id']
        )

        scores = []
        for p in participants:
            user_row = await conn.fetchrow(
                "SELECT username FROM users WHERE user_id = $1", p['user_id']
            )
            scores.append({
                'user_id':         p['user_id'],
                'username':        user_row['username'] if user_row else f'User {p["user_id"]}',
                'total_value':     float(p['total_value']),
                'gold_count':      int(p['gold_count']),
                'best_item_value': float(p['best_item_value']),
                'is_bot':          p['is_bot'],
                'team_id':         p['team_id'],
            })

        await self.broadcast(battle_id, {
            'type':            'battle_complete',
            'winner_id':       winner['user_id'],
            'winner_username': winner_row['username'] if winner_row else 'Unknown',
            'prize':           prize,
            'team_size':       team_size,
            'scores':          scores,
            'battle_type':     battle['battle_type'],
        })

        # Clean up room after 2 minutes
        async def _cleanup():
            await asyncio.sleep(120)
            self.rooms.pop(battle_id, None)
            self.ws_connections.pop(battle_id, None)
        asyncio.create_task(_cleanup())

    def shutdown(self):
        self._shutdown = True
        for task in self.tick_tasks.values():
            task.cancel()
        if self.matchmaking_task:
            self.matchmaking_task.cancel()


# ── Singleton ────────────────────────────────────────────────
battle_manager = BattleManager()


def start_matchmaking():
    if battle_manager.matchmaking_task is None or battle_manager.matchmaking_task.done():
        battle_manager.matchmaking_task = asyncio.create_task(
            battle_manager.start_matchmaking()
        )
        logger.info("⚔️  Battle matchmaking loop started")


def shutdown_matchmaking():
    battle_manager.shutdown()
    logger.info("⚔️  Battle matchmaking shutdown")


async def create_private_room(participant_user_ids: list, fee: float,
                                rounds: int = 3, win_condition: str = 'total_value') -> int:
    """Programmatic room creation for the Friends challenge system
    (routes/friends.py) -- calls _create_pvp_battle() directly with a
    pre-chosen player list, bypassing the queue/matchmaking loop entirely
    (that function already supports this, confirmed in Session 3's
    research; no schema change needed here unlike the 6 duel games).
    Scoped to FFA cash entry only (team_size=1, 2-4 players) -- team-mode
    friend challenges (who's on whose team) would need real additional
    UI and are out of scope this session. `ws=None` per player is safe:
    broadcast_to_set() already treats any dead/invalid connection as
    something to clean up on the next broadcast (shared.py:1929), and no
    player has a live battle-room WS yet until they load the battle page."""
    if not (2 <= len(participant_user_ids) <= 4):
        raise HTTPException(400, "Case Battles friend challenges support 2-4 players (FFA only)")

    players = [
        {'user_id': uid, 'ws': None, 'rounds': rounds, 'win_condition': win_condition, 'team_size': 1}
        for uid in participant_user_ids
    ]
    pool = await get_db()
    async with pool.acquire() as conn:
        battle_id = await battle_manager._create_pvp_battle(conn, players, fee)
    if not battle_id:
        raise HTTPException(400, "Could not create battle -- a participant may have insufficient balance")
    return battle_id


# ============================================================
# BOT AUTO-OPEN  (runs after 2s delay in a background task)
# ============================================================

async def _schedule_bot_open(
    battle_id: int, participant_id: int,
    round_num: int, difficulty: str
):
    """Trigger bot open 2 seconds after user opens, don't wait for tick timeout."""
    await asyncio.sleep(2)
    pool = await get_db()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("""
            SELECT 1 FROM case_battle_rounds
            WHERE participant_id = $1 AND round_number = $2
        """, participant_id, round_num)
        if exists:
            return  # Bot already opened (e.g. from tick timeout)

        battle = await conn.fetchrow(
            "SELECT status FROM case_battles WHERE id = $1", battle_id
        )
        if not battle or battle['status'] != 'active':
            return

        await battle_manager._bot_open(
            battle_id, participant_id, round_num, difficulty, conn
        )
        await battle_manager._check_and_advance(battle_id, round_num, conn)


# ============================================================
# WEBSOCKETS
# ============================================================

@router.websocket("/matchmaking")
async def ws_matchmaking(websocket: WebSocket):
    await websocket.accept()

    token = websocket.cookies.get("session_token")
    session = shared.get_session(token) if token else None
    if not session:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    user_id = session["user_id"]
    battle_manager.matchmaking_ws[user_id] = websocket
    logger.info(f"Matchmaking WS connected: user {user_id}")

    try:
        while True:
            await websocket.receive_text()   # keep-alive; messages not needed
    except WebSocketDisconnect:
        pass
    finally:
        battle_manager.matchmaking_ws.pop(user_id, None)
        logger.info(f"Matchmaking WS disconnected: user {user_id}")


@router.websocket("/ws/{battle_id}")
async def ws_battle(websocket: WebSocket, battle_id: int):
    await websocket.accept()

    token = websocket.cookies.get("session_token")
    session = shared.get_session(token) if token else None
    if not session:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    user_id = session["user_id"]
    pool    = await get_db()

    async with pool.acquire() as conn:
        participant = await conn.fetchrow("""
            SELECT id FROM case_battle_participants
            WHERE battle_id = $1 AND user_id = $2
        """, battle_id, user_id)

        battle = await conn.fetchrow("""
            SELECT status, win_condition, total_rounds, battle_type
            FROM case_battles WHERE id = $1
        """, battle_id)

    if not battle:
        await websocket.close(code=1008, reason="Battle not found")
        return

    is_participant = participant is not None
    battle_manager.add_ws(battle_id, websocket)

    if is_participant:
        room = battle_manager.rooms.get(battle_id)
        if room and user_id in room.get('players', {}):
            room['players'][user_id]['ws'] = websocket

        # Start battle on first participant connect
        if battle['status'] == 'waiting':
            async with pool.acquire() as conn:
                await battle_manager.start_battle(battle_id, conn)

    # Send full state snapshot immediately
    await _send_state(websocket, battle_id, user_id)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get('type')

            if msg_type == 'open_case' and is_participant:
                case_id = data.get('case_id')
                await _handle_open(battle_id, user_id, case_id)

            elif msg_type == 'reaction':
                rxn = data.get('reaction', '')
                if rxn in ('🔥', '😱', '🍀', '💀', '👀', '💎'):
                    await battle_manager.broadcast(battle_id, {
                        'type':     'reaction',
                        'user_id':  user_id,
                        'reaction': rxn,
                    })

            elif msg_type == 'ping':
                try:
                    await websocket.send_json({'type': 'pong'})
                except Exception:
                    break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Battle WS error: {e}")
    finally:
        battle_manager.remove_ws(battle_id, websocket)
        await battle_manager.broadcast(battle_id, {
            'type':    'player_left',
            'user_id': user_id,
        })


# ── State snapshot ───────────────────────────────────────────
async def _send_state(ws: WebSocket, battle_id: int, user_id: int):
    pool = await get_db()
    async with pool.acquire() as conn:
        battle = await conn.fetchrow("""
            SELECT status, total_rounds, win_condition, battle_type,
                   player_count, team_size
            FROM case_battles WHERE id = $1
        """, battle_id)

        participants = await conn.fetch("""
            SELECT cp.user_id, cp.total_value, cp.gold_count,
                   cp.best_item_value, cp.is_bot, cp.current_round,
                   cp.team_id, u.username
            FROM case_battle_participants cp
            LEFT JOIN users u ON cp.user_id = u.user_id
            WHERE cp.battle_id = $1
        """, battle_id)

        rounds = await conn.fetch("""
            SELECT participant_id, round_number, item_name, rarity, value, is_stattrak, float_value
            FROM case_battle_rounds
            WHERE battle_id = $1
            ORDER BY round_number
        """, battle_id)

    try:
        await ws.send_json({
            'type':          'state',
            'battle_id':     battle_id,
            'your_user_id':  user_id,
            'status':        battle['status'] if battle else 'unknown',
            'total_rounds':  battle['total_rounds'] if battle else 0,
            'win_condition': battle['win_condition'] if battle else 'total_value',
            'battle_type':   battle['battle_type'] if battle else 'pve',
            'player_count':  battle['player_count'] if battle else 2,
            'team_size':     battle['team_size'] if battle else 1,
            'participants':  convert_decimals([dict(p) for p in participants]),
            'rounds':        convert_decimals([dict(r) for r in rounds]),
        })
    except Exception:
        pass


# ── Handle user case open ────────────────────────────────────
async def _handle_open(battle_id: int, user_id: int, case_id: Optional[str]):
    pool = await get_db()
    async with pool.acquire() as conn:
        # FOR UPDATE inside a transaction prevents concurrent opens from both
        # reading the same current_round and doubling total_value.
        async with conn.transaction():
            part = await conn.fetchrow("""
                SELECT id, current_round
                FROM case_battle_participants
                WHERE battle_id = $1 AND user_id = $2
                FOR UPDATE
            """, battle_id, user_id)
            if not part:
                return

            current_round = part['current_round'] + 1

            battle = await conn.fetchrow(
                "SELECT total_rounds FROM case_battles WHERE id = $1", battle_id
            )
            if current_round > battle['total_rounds']:
                return

            # Idempotency guard
            exists = await conn.fetchval("""
                SELECT 1 FROM case_battle_rounds
                WHERE participant_id = $1 AND round_number = $2
            """, part['id'], current_round)
            if exists:
                return

            if not case_id or case_id not in CASES or case_id in BATTLE_EXCLUDED_CASE_IDS:
                case_id = secure_choice(BATTLE_ELIGIBLE_CASE_IDS)

            item = get_random_item(case_id)
            if not item:
                return

            # Record round
            await conn.execute("""
                INSERT INTO case_battle_rounds
                    (battle_id, participant_id, round_number,
                     item_name, rarity, value, is_stattrak, float_value)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (participant_id, round_number) DO NOTHING
            """, battle_id, part['id'], current_round,
                item['name'], item['rarity'], item['price'],
                item.get('is_stattrak', False), item.get('float', 0.0))

            # Update participant totals
            await conn.execute("""
                UPDATE case_battle_participants
                SET total_value     = total_value + $1,
                    gold_count      = gold_count + CASE WHEN $2='Gold' THEN 1 ELSE 0 END,
                    best_item_value = GREATEST(best_item_value, $1),
                    current_round   = $3
                WHERE id = $4
            """, item['price'], item['rarity'], current_round, part['id'])

            # Add item to user's inventory
            await conn.execute("""
                INSERT INTO inventory
                    (user_id, item_name, item_type, rarity, price,
                     condition, is_stattrak, status, float_value)
                VALUES ($1,$2,'weapon',$3,$4,$5,$6,'kept',$7)
            """, user_id, item['name'], item['rarity'], item['price'],
                item.get('condition', 'Field-Tested'),
                item.get('is_stattrak', False), item.get('float', 0.0))

            if user_id > 0:
                await conn.execute("""
                    UPDATE users SET total_opens = total_opens + 1,
                        total_golds = total_golds + $2
                    WHERE user_id = $1
                """, user_id, 1 if item['rarity'] == 'Gold' else 0)
        # Transaction committed — lock released; broadcast outside the transaction

        user_row = await conn.fetchrow(
            "SELECT username FROM users WHERE user_id = $1", user_id
        )
        await battle_manager.broadcast(battle_id, {
            'type':     'round_update',
            'user_id':  user_id,
            'username': user_row['username'] if user_row else 'Unknown',
            'round':    current_round,
            'item':     item,
            'value':    float(item['price']),
        })

        # Schedule opens for any bot opponents in this battle
        bot_opponents = await conn.fetch("""
            SELECT id, bot_difficulty
            FROM case_battle_participants
            WHERE battle_id = $1 AND user_id != $2 AND is_bot = TRUE
        """, battle_id, user_id)
        for bot in bot_opponents:
            task = asyncio.create_task(_schedule_bot_open(
                battle_id, bot['id'], current_round,
                bot['bot_difficulty'] or 'normal'
            ))
            task.add_done_callback(lambda t: logger.error(f"bot_open failed: {t.exception()}") if t.exception() else None)

        await battle_manager._check_and_advance(battle_id, current_round, conn)


# ============================================================
# HTTP ENDPOINTS
# ============================================================

@router.get("/settings")
async def get_battle_settings(request: Request):
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT enabled, fee_tiers, round_options FROM battle_settings LIMIT 1"
        )
        if not row:
            return {"enabled": True, "fee_tiers": [1000], "round_options": [3, 5, 7]}
        return {
            "enabled":       row['enabled'],
            "fee_tiers":     list(row['fee_tiers']),
            "round_options": list(row['round_options']),
        }


_VALID_MODES = {
    (2, 1), (3, 1), (4, 1),   # FFA: 1v1, 1v1v1, 1v1v1v1
    (4, 2), (6, 3), (8, 4),   # Team: 2v2, 3v3, 4v4
}

@router.post("/queue")
async def join_queue(request: Request, req: QueueRequest):
    user_id = await get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")

    _VALID_WIN_CONDITIONS = {'total_value', 'gold_count', 'best_item'}
    if req.win_condition not in _VALID_WIN_CONDITIONS:
        raise HTTPException(400, f"win_condition must be one of {sorted(_VALID_WIN_CONDITIONS)}")

    player_count = int(req.player_count)
    team_size    = int(req.team_size)
    if (player_count, team_size) not in _VALID_MODES:
        raise HTTPException(400, f"Invalid mode. Valid (player_count, team_size): {sorted(_VALID_MODES)}")

    rounds = max(1, min(req.rounds, 10))

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            settings = await conn.fetchrow(
                "SELECT enabled, fee_tiers FROM battle_settings LIMIT 1"
            )
            if not settings or not settings['enabled']:
                raise HTTPException(400, "Battles are currently disabled")
            valid_fees = {float(f) for f in (settings['fee_tiers'] or [])}
            if req.fee not in valid_fees:
                raise HTTPException(400, f"Fee must be one of: {sorted(valid_fees)}")
            if req.fee > 750_000:
                raise HTTPException(400, "Maximum battle fee is $750,000")
            if req.inventory_id is not None:
                # Eligibility check only -- the actual atomic stake happens at
                # match time in _create_pvp_battle, mirroring how cash isn't
                # deducted until a match forms either. No FOR UPDATE lock
                # here since nothing is mutated yet.
                item_check = await conn.fetchrow(
                    "SELECT price FROM inventory WHERE id=$1 AND user_id=$2 AND status='kept'",
                    req.inventory_id, user_id
                )
                if not item_check or float(item_check['price']) < req.fee:
                    raise HTTPException(400, f"Item not eligible (must be a kept item worth at least ${req.fee:.2f})")
            else:
                user = await conn.fetchrow(
                    "SELECT balance FROM users WHERE user_id = $1 FOR UPDATE",
                    user_id
                )
                if not user or float(user['balance']) < req.fee:
                    raise HTTPException(400, "Insufficient balance")

    mm_ws = battle_manager.matchmaking_ws.get(user_id)
    if not mm_ws:
        raise HTTPException(400, "Matchmaking WebSocket not connected — please refresh")

    if user_id in battle_manager.queued_users:
        raise HTTPException(400, "Already in matchmaking queue — please wait")

    mode_label = {
        (2,1):'1v1',(3,1):'1v1v1',(4,1):'1v1v1v1',
        (4,2):'2v2',(6,3):'3v3',(8,4):'4v4',
    }.get((player_count, team_size), f'{player_count}p')

    battle_manager.queued_users.add(user_id)
    queue = battle_manager.get_queue(req.fee, player_count, team_size)
    await queue.put({
        'user_id':       user_id,
        'ws':            mm_ws,
        'fee':           req.fee,
        'rounds':        rounds,
        'win_condition': req.win_condition,
        'player_count':  player_count,
        'team_size':     team_size,
        'inventory_id':  req.inventory_id,
    })
    return {
        "success":  True,
        "message":  f"In {mode_label} queue — waiting for {player_count - 1} more player(s)",
        "mode":     mode_label,
    }


@router.post("/pve/start")
async def start_pve(request: Request, req: PvERequest):
    user_id = await get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")

    _VALID_WIN_CONDITIONS = {'total_value', 'gold_count', 'best_item'}
    if req.win_condition not in _VALID_WIN_CONDITIONS:
        raise HTTPException(400, f"win_condition must be one of {sorted(_VALID_WIN_CONDITIONS)}")
    rounds = max(1, min(req.rounds, 10))

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if req.fee <= 0:
                raise HTTPException(400, "Fee must be positive")
            if req.fee > 750_000:
                raise HTTPException(400, "Maximum battle fee is $750,000")

            stake_inv_id, stake_value = None, None
            if req.inventory_id is not None:
                item = await conn.fetchrow("""
                    UPDATE inventory SET status='staked'
                    WHERE id=$1 AND user_id=$2 AND status='kept' AND protected = FALSE AND price >= $3
                    RETURNING price
                """, req.inventory_id, user_id, req.fee)
                if not item:
                    raise HTTPException(400, f"Item not available to stake (must be a kept, unprotected item worth at least ${req.fee:.2f})")
                stake_inv_id, stake_value = req.inventory_id, float(item['price'])
            else:
                # Atomic deduct with balance guard to prevent negative balance under
                # concurrent requests (no separate SELECT needed).
                deducted = await conn.fetchval(
                    "UPDATE users SET balance = balance - $1 WHERE user_id = $2 AND balance >= $1 RETURNING user_id",
                    req.fee, user_id
                )
                if not deducted:
                    raise HTTPException(400, "Insufficient balance")

            battle_id = await conn.fetchval("""
                INSERT INTO case_battles
                    (battle_type, status, entry_fee, total_rounds, win_condition)
                VALUES ('pve', 'waiting', $1, $2, $3)
                RETURNING id
            """, req.fee, rounds, req.win_condition)

            await conn.execute("""
                INSERT INTO case_battle_participants (battle_id, user_id, staked_inventory_id, staked_value)
                VALUES ($1, $2, $3, $4)
            """, battle_id, user_id, stake_inv_id, stake_value)

            bot_id = BOT_IDS.get(req.difficulty, -1)
            await conn.execute("""
                INSERT INTO case_battle_participants
                    (battle_id, user_id, is_bot, bot_difficulty)
                VALUES ($1, $2, TRUE, $3)
            """, battle_id, bot_id, req.difficulty)

            battle_manager.rooms[battle_id] = {
                'status':       'waiting',
                'battle_type':  'pve',
                'difficulty':   req.difficulty,
                'players': {
                    user_id: {'ws': None, 'ready': False},
                    bot_id:  {'ws': None, 'ready': False,
                              'bot': True, 'difficulty': req.difficulty},
                },
                'round':        0,
                'total_rounds': req.rounds,
                'win_condition': req.win_condition,
            }
            battle_manager.ws_connections[battle_id] = set()

    return {
        "success":    True,
        "battle_id":  battle_id,
        "fee":        req.fee,
        "rounds":     req.rounds,
        "win":        req.win_condition,
        "difficulty": req.difficulty,
    }


@router.get("/history")
async def battle_history(request: Request):
    user_id = await get_user_id_from_session(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT b.id, b.battle_type, b.entry_fee, b.win_condition,
                   b.ended_at, b.winner_id,
                   EXISTS (
                       SELECT 1 FROM case_battle_participants
                       WHERE battle_id = b.id AND user_id = $1
                   ) AS participated
            FROM case_battles b
            WHERE b.status = 'completed' AND b.winner_id IS NOT NULL
            ORDER BY b.ended_at DESC
            LIMIT 50
        """, user_id)
    return [convert_decimals(dict(r)) for r in rows]


@router.get("/active")
async def active_battles(request: Request):
    """Return currently active/waiting battles for the lobby view."""
    pool = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT b.id, b.battle_type, b.entry_fee, b.total_rounds,
                   b.win_condition, b.status, b.created_at,
                   b.player_count, b.team_size,
                   COUNT(cp.id) AS joined_count
            FROM case_battles b
            LEFT JOIN case_battle_participants cp ON cp.battle_id = b.id
            WHERE b.status IN ('waiting','active')
            GROUP BY b.id
            ORDER BY b.created_at DESC
            LIMIT 20
        """)
    return [convert_decimals(dict(r)) for r in rows]


# ── Admin ────────────────────────────────────────────────────
@router.get("/admin/settings")
async def admin_get_settings(request: Request, _=Depends(require_admin)):
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT enabled, fee_tiers, round_options FROM battle_settings LIMIT 1"
        )
        if not row:
            return {"enabled": True, "fee_tiers": [1000], "round_options": [3, 5, 7]}
        return {
            "enabled":       row['enabled'],
            "fee_tiers":     list(row['fee_tiers']),
            "round_options": list(row['round_options']),
        }

@router.post("/admin/settings")
async def admin_update_settings(
    request: Request,
    update: BattleSettingsUpdate,
    _=Depends(require_admin)
):
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE battle_settings
            SET enabled = $1, fee_tiers = $2, round_options = $3, updated_at = NOW()
            WHERE id = (SELECT id FROM battle_settings LIMIT 1)
        """, update.enabled, update.fee_tiers, update.round_options)
    return {"success": True}

@router.post("/admin/toggle")
async def admin_toggle(request: Request, _=Depends(require_admin)):
    pool = await get_db()
    async with pool.acquire() as conn:
        current = await conn.fetchval(
            "SELECT enabled FROM battle_settings LIMIT 1"
        )
        new_val = not current
        await conn.execute(
            "UPDATE battle_settings SET enabled = $1 WHERE id = (SELECT id FROM battle_settings LIMIT 1)",
            new_val
        )
    return {"enabled": new_val}


# ============================================================
# DB TABLE INIT  (called from server.py lifespan)
# ============================================================

async def init_battle_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS battle_settings (
                id           SERIAL PRIMARY KEY,
                enabled      BOOLEAN DEFAULT TRUE,
                fee_tiers    DECIMAL(10,2)[] DEFAULT '{1000}',
                round_options INTEGER[]      DEFAULT '{3,5,7}',
                bot_difficulties TEXT[]      DEFAULT '{normal,hard,expert}',
                updated_at   TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            INSERT INTO battle_settings (enabled, fee_tiers, round_options)
            SELECT TRUE, '{1000}', '{3,5,7}'
            WHERE NOT EXISTS (SELECT 1 FROM battle_settings)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS case_battles (
                id            SERIAL PRIMARY KEY,
                battle_type   TEXT NOT NULL CHECK (battle_type IN ('pvp','pve')),
                status        TEXT DEFAULT 'waiting'
                              CHECK (status IN ('waiting','active','completed','cancelled')),
                entry_fee     DECIMAL(10,2) NOT NULL,
                total_rounds  INTEGER NOT NULL,
                win_condition TEXT DEFAULT 'total_value'
                              CHECK (win_condition IN ('total_value','gold_count','best_item')),
                created_at    TIMESTAMP DEFAULT NOW(),
                started_at    TIMESTAMP,
                ended_at      TIMESTAMP,
                winner_id     BIGINT REFERENCES users(user_id) ON DELETE SET NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS case_battle_participants (
                id              SERIAL PRIMARY KEY,
                battle_id       INTEGER REFERENCES case_battles(id) ON DELETE CASCADE,
                user_id         BIGINT  REFERENCES users(user_id)   ON DELETE CASCADE,
                is_bot          BOOLEAN DEFAULT FALSE,
                bot_difficulty  TEXT,
                total_value     DECIMAL(10,2) DEFAULT 0,
                gold_count      INTEGER DEFAULT 0,
                best_item_value DECIMAL(10,2) DEFAULT 0,
                current_round   INTEGER DEFAULT 0,
                joined_at       TIMESTAMP DEFAULT NOW(),
                UNIQUE(battle_id, user_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS case_battle_rounds (
                id             SERIAL PRIMARY KEY,
                battle_id      INTEGER REFERENCES case_battles(id) ON DELETE CASCADE,
                participant_id INTEGER REFERENCES case_battle_participants(id) ON DELETE CASCADE,
                round_number   INTEGER NOT NULL,
                item_name      TEXT NOT NULL,
                rarity         TEXT,
                value          DECIMAL(10,2),
                is_stattrak    BOOLEAN DEFAULT FALSE,
                float_value    DECIMAL(10,4),
                opened_at      TIMESTAMP DEFAULT NOW(),
                UNIQUE(participant_id, round_number)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS battle_reactions (
                id         SERIAL PRIMARY KEY,
                battle_id  INTEGER REFERENCES case_battles(id) ON DELETE CASCADE,
                user_id    BIGINT  REFERENCES users(user_id)   ON DELETE CASCADE,
                reaction   TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Migrations for multi-player columns
        for sql in [
            "ALTER TABLE case_battles ADD COLUMN IF NOT EXISTS player_count INTEGER DEFAULT 2",
            "ALTER TABLE case_battles ADD COLUMN IF NOT EXISTS team_size INTEGER DEFAULT 1",
            "ALTER TABLE case_battle_participants ADD COLUMN IF NOT EXISTS team_id INTEGER DEFAULT 1",
            "ALTER TABLE case_battle_participants ADD COLUMN IF NOT EXISTS staked_inventory_id INTEGER REFERENCES inventory(id) ON DELETE SET NULL",
            "ALTER TABLE case_battle_participants ADD COLUMN IF NOT EXISTS staked_value DECIMAL(10,2)",
        ]:
            try:
                await conn.execute(sql)
            except Exception:
                pass
        # 'cancelled' was added to the status CHECK after initial release --
        # existing databases need the constraint recreated to accept it.
        try:
            await conn.execute("ALTER TABLE case_battles DROP CONSTRAINT IF EXISTS case_battles_status_check")
            await conn.execute(
                "ALTER TABLE case_battles ADD CONSTRAINT case_battles_status_check "
                "CHECK (status IN ('waiting','active','completed','cancelled'))"
            )
        except Exception as e:
            logger.warning(f"case_battles status constraint migration skipped: {e}")
    logger.info("✅ Battle tables ready")


async def _refund_battle(conn, battle_id: int):
    """Refund every real (non-bot) participant's stake for a battle that's
    being cancelled -- cash back for cash entries, item un-staked back to
    'kept' for item-staked entries. Shared by both the startup sweep and the
    runtime expiry loop below."""
    participants = await conn.fetch("""
        SELECT user_id, is_bot, staked_inventory_id, entry_fee
        FROM case_battle_participants p
        JOIN case_battles b ON b.id = p.battle_id
        WHERE p.battle_id = $1
    """, battle_id)
    for p in participants:
        if p['is_bot'] or p['user_id'] <= 0:
            continue
        if p['staked_inventory_id']:
            await conn.execute(
                "UPDATE inventory SET status='kept' WHERE id=$1 AND status='staked'",
                p['staked_inventory_id']
            )
        else:
            await conn.execute(
                "UPDATE users SET balance = balance + $1 WHERE user_id = $2",
                p['entry_fee'], p['user_id']
            )


async def recover_stale_case_battles():
    """Startup recovery -- case_battles has no way to resume an in-progress
    battle across a restart (battle_manager.rooms/ws_connections/tick_tasks
    are all in-memory and gone the moment the process exits), so anything
    left in 'waiting' or 'active' from before this restart is unrecoverable
    and refunded rather than left stuck forever with the entry fee/item
    already taken. Superset of the old item-stake-only recovery: now also
    refunds cash entries and actually marks the battle 'cancelled' so it
    can't be mistaken for still-in-progress by the frontend or a late
    WebSocket reconnect."""
    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            stale = await conn.fetch("""
                SELECT id FROM case_battles WHERE status IN ('waiting','active') FOR UPDATE
            """)
            for row in stale:
                await _refund_battle(conn, row['id'])
                await conn.execute(
                    "UPDATE case_battles SET status='cancelled', ended_at=NOW() WHERE id=$1",
                    row['id']
                )
            if stale:
                logger.info(f"⚔️ Recovered {len(stale)} stale case battle(s) from before restart")


# Backwards-compat alias -- kept in case anything else still imports the old name.
recover_stale_item_battle_stakes = recover_stale_case_battles


async def expire_stale_case_battles_loop():
    """Runtime safety net (no restart needed) for the case where a match is
    created and fees/items are deducted in _create_pvp_battle, but not every
    participant's client ever opens a WebSocket to the room -- e.g. their
    matchmaking WS died right as the match was found, so they never received
    'match_found' and never learn the battle_id. Nothing else ever notices
    that battle again (start_battle() only fires on the first WS connect),
    so it would otherwise sit in 'waiting' forever with money already gone.
    Also covers 'active' battles whose tick loop died from an unexpected
    exception (defense in depth) -- 10 minutes comfortably exceeds the
    longest possible legitimate game (10 rounds * ~18s/round).
    Each cancel is claimed with an atomic status-guarded UPDATE first, so a
    legitimate late WS connect racing this loop can never be double-handled:
    whichever of start_battle()'s or this loop's UPDATE ... WHERE status=...
    lands first wins, the other is a no-op."""
    while True:
        await asyncio.sleep(60)
        try:
            pool = await get_db()
            async with pool.acquire() as conn:
                stuck_waiting = await conn.fetch(
                    "SELECT id FROM case_battles WHERE status='waiting' AND created_at <= NOW() - INTERVAL '2 minutes'"
                )
                stuck_active = await conn.fetch(
                    "SELECT id FROM case_battles WHERE status='active' AND started_at <= NOW() - INTERVAL '10 minutes'"
                )
                cancelled = 0
                for row in list(stuck_waiting) + list(stuck_active):
                    async with conn.transaction():
                        claimed = await conn.fetchval("""
                            UPDATE case_battles SET status='cancelled', ended_at=NOW()
                            WHERE id=$1 AND status IN ('waiting','active') RETURNING id
                        """, row['id'])
                        if not claimed:
                            continue
                        await _refund_battle(conn, row['id'])
                        cancelled += 1
                    await battle_manager.broadcast(row['id'], {
                        'type': 'battle_cancelled',
                        'message': 'This battle timed out and was cancelled — your entry has been refunded.',
                    })
                    battle_manager.rooms.pop(row['id'], None)
                    battle_manager._cancel_tick(row['id'])
                if cancelled:
                    logger.info(f"⚔️ Expired {cancelled} stuck case battle(s), refunded entries")
        except Exception as e:
            logger.warning(f"expire_stale_case_battles_loop failed: {e}")
