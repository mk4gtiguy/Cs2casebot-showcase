# ============================================================
# routes/games_heavy.py
# CS2CaseBot | Heavy Games Backend
#
# Games: Live Race (CS2 agents, 4-player WebSocket rooms,
#        bot opponents with personality-driven movement)
# ============================================================

import json
import asyncio
import time
from typing import Dict, Set, Optional, List, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from pydantic import BaseModel

import shared
from shared import (
    logger, get_db, require_auth, get_user_id_from_session,
    ensure_user_exists, deduct_balance, add_balance,
    convert_decimals, broadcast_to_set, RACE_AGENTS,
    secure_random, secure_randint, secure_choice, secure_shuffle,
    apply_house_edge, HOUSE_EDGE, credit_win, require_game_enabled,
)

router = APIRouter(prefix="/api/games/race", tags=["games-race"])

MIN_BET     = 100
MAX_BET     = 750_000
MAX_PLAYERS = 4
TRACK_LENGTH = 400.0    # reduced from 1000 → races finish in ~8-12 seconds
TICK_RATE    = 0.10     # 100ms ticks → 10 updates/sec (Fix 12: halved for bandwidth)
LOBBY_SECS   = 12       # reduced lobby wait from 15
BOT_FILL_AT  = 3        # fill with bots faster

def clamp_bet(v: float) -> float:
    return shared.clamp_bet(v, MIN_BET, MAX_BET)

async def log_game(conn, user_id: int, bet: float, win: float, meta: dict = None):
    # win_inclusive=False: matches this file's original 'win > bet' (push = loss).
    await shared.log_game(conn, user_id, 'live_race', bet, win, meta,
                          win_inclusive=False)

# ============================================================
# CS2 AGENT DEFINITIONS
# ============================================================
# Each agent has a movement profile that determines how they
# race: base_speed, burst_chance, burst_mult, stamina_decay,
# and recovery_rate. Bots pick agents based on personality.

AGENT_PROFILES = {
    'sas': {
        'name':          'SAS Operator',
        'emoji':         '🟢',
        'color':         '#4caf50',
        'base_speed':    5.8,
        'burst_chance':  0.08,   # 8% chance of a speed burst per tick
        'burst_mult':    2.2,    # burst = base × this
        'stamina_decay': 0.003,  # stamina drains per tick when bursting
        'recovery_rate': 0.005,  # stamina recovers per tick when normal
        'personality':   'steady',
    },
    'phoenix': {
        'name':          'Phoenix Operative',
        'emoji':         '🔴',
        'color':         '#f44336',
        'base_speed':    6.5,
        'burst_chance':  0.15,
        'burst_mult':    2.8,
        'stamina_decay': 0.006,
        'recovery_rate': 0.003,
        'personality':   'aggressive',
    },
    'swat': {
        'name':          'SWAT Commander',
        'emoji':         '🔵',
        'color':         '#2196f3',
        'base_speed':    5.5,
        'burst_chance':  0.05,
        'burst_mult':    1.8,
        'stamina_decay': 0.002,
        'recovery_rate': 0.008,
        'personality':   'conservative',
    },
    'guerrilla': {
        'name':          'Guerrilla Warfare',
        'emoji':         '🟡',
        'color':         '#ffd700',
        'base_speed':    6.2,
        'burst_chance':  0.12,
        'burst_mult':    2.5,
        'stamina_decay': 0.005,
        'recovery_rate': 0.004,
        'personality':   'erratic',
    },
    'ksk': {
        'name':          'KSK Operator',
        'emoji':         '🟣',
        'color':         '#9c27b0',
        'base_speed':    6.0,
        'burst_chance':  0.10,
        'burst_mult':    2.3,
        'stamina_decay': 0.004,
        'recovery_rate': 0.005,
        'personality':   'tactical',
    },
    'seal': {
        'name':          'SEAL Frogman',
        'emoji':         '🩵',
        'color':         '#00bcd4',
        'base_speed':    5.9,
        'burst_chance':  0.09,
        'burst_mult':    2.1,
        'stamina_decay': 0.003,
        'recovery_rate': 0.006,
        'personality':   'steady',
    },
    'ksm': {
        'name':          'Sabre CT',
        'emoji':         '🟠',
        'color':         '#ff9800',
        'base_speed':    6.3,
        'burst_chance':  0.11,
        'burst_mult':    2.6,
        'stamina_decay': 0.005,
        'recovery_rate': 0.004,
        'personality':   'aggressive',
    },
    'ground': {
        'name':          'Ground Rebel',
        'emoji':         '⚪',
        'color':         '#9e9e9e',
        'base_speed':    5.6,
        'burst_chance':  0.07,
        'burst_mult':    2.0,
        'stamina_decay': 0.003,
        'recovery_rate': 0.007,
        'personality':   'conservative',
    },
}

# ============================================================
# RACE ROOM
# ============================================================

class Racer:
    """Single participant in a race."""
    def __init__(self, user_id: int, username: str, agent_id: str,
                 bet: float, is_bot: bool = False):
        self.user_id    = user_id
        self.username   = username
        self.agent_id   = agent_id
        self.agent      = AGENT_PROFILES.get(agent_id, AGENT_PROFILES['sas'])
        self.bet        = bet
        self.is_bot     = is_bot

        # Race state
        self.position   = 0.0
        self.stamina    = 1.0    # 0.0 – 1.0
        self.bursting   = False
        self.finished   = False
        self.finish_pos = None   # 1st, 2nd, 3rd, 4th
        self.finish_time = None

        # Bot-specific drift for unpredictability
        self._drift     = 0.85 + secure_random() * 0.30
        self._luck      = 0.9 + secure_random() * 0.20

    def tick(self, elapsed: float) -> float:
        """Advance one physics tick. Returns distance moved."""
        if self.finished:
            return 0.0

        profile = self.agent
        base    = profile['base_speed'] * self._drift * self._luck

        # Personality modifiers
        personality = profile['personality']

        # Stamina system
        if self.bursting:
            if self.stamina > 0:
                self.stamina = max(0, self.stamina - profile['stamina_decay'])
            else:
                self.bursting = False   # ran out of steam
        else:
            self.stamina = min(1.0, self.stamina + profile['recovery_rate'])

        # Burst trigger
        burst_roll = secure_random()
        if not self.bursting and self.stamina > 0.3:
            if personality == 'aggressive' and burst_roll < profile['burst_chance'] * 1.4:
                self.bursting = True
            elif personality == 'erratic':
                # Erratic: burst in random waves
                if burst_roll < profile['burst_chance'] * (1.0 + (-0.5 + secure_random() * 1.0)):
                    self.bursting = True
            elif personality == 'tactical' and elapsed > 2.0 and self.position < TRACK_LENGTH * 0.7:
                # Tactical: conserves, then sprints in the last 30%
                if burst_roll < profile['burst_chance'] * 2.0:
                    self.bursting = True
            elif personality == 'conservative' and self.stamina > 0.7:
                # Only bursts when very fresh
                if burst_roll < profile['burst_chance'] * 0.6:
                    self.bursting = True
            elif burst_roll < profile['burst_chance']:
                self.bursting = True

        speed = base * (profile['burst_mult'] if self.bursting else 1.0)

        # Final position noise
        noise = 0.92 + secure_random() * 0.16
        dist  = speed * noise * TICK_RATE

        self.position = min(TRACK_LENGTH, self.position + dist)
        return dist

    def to_dict(self) -> dict:
        return {
            'user_id':    self.user_id,
            'username':   self.username,
            'agent_id':   self.agent_id,
            'agent_name': self.agent['name'],
            'emoji':      self.agent['emoji'],
            'color':      self.agent['color'],
            'position':   round(self.position, 2),
            'progress':   round(self.position / TRACK_LENGTH * 100, 1),
            'stamina':    round(self.stamina, 3),
            'bursting':   self.bursting,
            'finished':   self.finished,
            'finish_pos': self.finish_pos,
            'bet':        self.bet,
            'is_bot':     self.is_bot,
        }


class RaceRoom:
    """Manages one complete race lifecycle."""

    def __init__(self, room_code: str):
        self.room_code  = room_code
        self.racers:    Dict[int, Racer]    = {}
        self.ws_set:    Set[WebSocket]      = set()
        self.ws_map:    Dict[int, WebSocket] = {}
        self.phase      = 'lobby'       # lobby → countdown → racing → finished
        self.task:      Optional[asyncio.Task] = None
        self.created_at = time.time()
        self.finish_order: List[int]    = []   # user_ids in finish order
        self.payouts:      Dict[int, float] = {}

    # ── WebSocket helpers ─────────────────────────────────
    def add_ws(self, user_id: int, ws: WebSocket):
        self.ws_set.add(ws)
        self.ws_map[user_id] = ws

    def remove_ws(self, user_id: int, ws: WebSocket):
        self.ws_set.discard(ws)
        self.ws_map.pop(user_id, None)

    async def broadcast(self, msg: dict):
        dead = await broadcast_to_set(self.ws_set, msg)
        self.ws_set -= dead

    # ── Player count helpers ──────────────────────────────
    @property
    def real_player_count(self) -> int:
        return sum(1 for r in self.racers.values() if not r.is_bot)

    @property
    def total_count(self) -> int:
        return len(self.racers)

    # ── Bot filling ───────────────────────────────────────
    def fill_bots(self):
        """Fill remaining slots with bot racers."""
        used_agents = {r.agent_id for r in self.racers.values()}
        available   = [a for a in AGENT_PROFILES if a not in used_agents]
        available[:] = secure_shuffle(available)

        bot_names = [
            '🤖 Ghost', '🤖 Shadow', '🤖 Phantom', '🤖 Specter'
        ]
        slot = 0
        while self.total_count < MAX_PLAYERS and available:
            agent_id = available.pop(0)
            bot_id   = -(slot + 1)
            bot_name = bot_names[slot % len(bot_names)]
            self.racers[bot_id] = Racer(
                user_id  = bot_id,
                username = bot_name,
                agent_id = agent_id,
                bet      = 0,
                is_bot   = True,
            )
            slot += 1

    # ── Main race lifecycle ───────────────────────────────
    async def run(self):
        """Full race: lobby → countdown → race → results."""
        try:
            await self._lobby_phase()
            await self._countdown_phase()
            await self._race_phase()
            await self._results_phase()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Race room {self.room_code} error: {e}")

    async def _lobby_phase(self):
        self.phase = 'lobby'
        await self.broadcast({
            'type':        'lobby_open',
            'room_code':   self.room_code,
            'lobby_secs':  LOBBY_SECS,
            'max_players': MAX_PLAYERS,
        })

        start = time.time()
        while time.time() - start < LOBBY_SECS:
            await asyncio.sleep(1)
            elapsed  = time.time() - start
            remaining = max(0, LOBBY_SECS - int(elapsed))

            # Fill bots after BOT_FILL_AT seconds if not full
            if elapsed >= BOT_FILL_AT and self.total_count < MAX_PLAYERS:
                self.fill_bots()

            await self.broadcast({
                'type':       'lobby_tick',
                'seconds':    remaining,
                'players':    [r.to_dict() for r in self.racers.values()],
                'player_count': self.real_player_count,
            })

            if self.real_player_count == MAX_PLAYERS:
                break   # Full lobby — start early

        # Final bot fill if still not full
        if self.total_count < MAX_PLAYERS:
            self.fill_bots()

    async def _countdown_phase(self):
        self.phase = 'countdown'
        for n in [3, 2, 1]:
            await self.broadcast({
                'type':    'countdown',
                'number':  n,
                'racers':  [r.to_dict() for r in self.racers.values()],
            })
            await asyncio.sleep(1)
        await self.broadcast({'type': 'go', 'racers': [r.to_dict() for r in self.racers.values()]})

    async def _race_phase(self):
        self.phase = 'racing'
        start_time = time.time()
        place      = 1

        while place <= MAX_PLAYERS:
            await asyncio.sleep(TICK_RATE)
            elapsed = time.time() - start_time

            # Tick every racer
            for racer in self.racers.values():
                if racer.finished:
                    continue
                racer.tick(elapsed)
                if racer.position >= TRACK_LENGTH and not racer.finished:
                    racer.finished    = True
                    racer.finish_pos  = place
                    racer.finish_time = elapsed
                    self.finish_order.append(racer.user_id)
                    await self.broadcast({
                        'type':       'racer_finished',
                        'user_id':    racer.user_id,
                        'username':   racer.username,
                        'agent_id':   racer.agent_id,
                        'emoji':      racer.agent['emoji'],
                        'place':      place,
                        'time':       round(elapsed, 2),
                    })
                    place += 1

            # Broadcast tick to all clients
            await self.broadcast({
                'type':    'race_tick',
                'elapsed': round(elapsed, 2),
                'racers':  [r.to_dict() for r in self.racers.values()],
            })

            # All finished?
            if all(r.finished for r in self.racers.values()):
                break

            # Safety timeout — 120s max race
            if elapsed > 120:
                for r in self.racers.values():
                    if not r.finished:
                        r.finished   = True
                        r.finish_pos = place
                        self.finish_order.append(r.user_id)
                        place += 1
                break

    async def _results_phase(self):
        self.phase = 'finished'
        pool = await get_db()

        # Calculate payouts:
        # 1st: 50% of pot, 2nd: 30%, 3rd: 15%, 4th: 5%
        # House takes HOUSE_EDGE from total pot first.
        # Bots have bet=0, which would make the pot tiny when bots fill the
        # room, causing 1st-place real players to get back only ~48% of their
        # bet. Fix: bots contribute a virtual bet equal to the minimum real
        # player bet so payouts are fair regardless of bot count.
        PLACE_SHARES = {1: 0.50, 2: 0.30, 3: 0.15, 4: 0.05}

        real_bets  = [r.bet for r in self.racers.values() if not r.is_bot]
        n_bots     = sum(1 for r in self.racers.values() if r.is_bot)
        min_bet    = min(real_bets) if real_bets else 0
        total_pot  = sum(real_bets) + n_bots * min_bet
        net_pot    = round(total_pot * (1 - HOUSE_EDGE), 2)

        results    = []
        last_err   = None
        for _attempt in range(3):
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        for uid in self.finish_order:
                            racer = self.racers.get(uid)
                            if not racer:
                                continue
                            place = racer.finish_pos or MAX_PLAYERS
                            share = PLACE_SHARES.get(place, 0)
                            payout = round(net_pot * share, 2) if not racer.is_bot else 0

                            if payout and not racer.is_bot:
                                payout = await credit_win(uid, payout, conn)
                                await log_game(conn, uid, racer.bet, payout, {
                                    'room':     self.room_code,
                                    'agent':    racer.agent_id,
                                    'place':    place,
                                    'net_pot':  net_pot,
                                })

                            self.payouts[uid] = payout

                            results.append({
                                'user_id':    uid,
                                'username':   racer.username,
                                'agent_id':   racer.agent_id,
                                'agent_name': racer.agent['name'],
                                'emoji':      racer.agent['emoji'],
                                'color':      racer.agent['color'],
                                'place':      place,
                                'finish_time': round(racer.finish_time or 0, 2),
                                'bet':        racer.bet,
                                'payout':     payout,
                                'is_bot':     racer.is_bot,
                            })
                last_err = None
                break   # success
            except Exception as e:
                last_err = e
                results  = []
                self.payouts = {}
                logger.error(f"Race {self.room_code} payout attempt {_attempt+1}/3 failed: {e}")
                if _attempt < 2:
                    await asyncio.sleep(1)

        if last_err:
            logger.error(f"Race {self.room_code} payout permanently failed: {last_err}")
            # Refund all real players their original bet so the promise of a
            # refund in the broadcast message is actually honoured.
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        for racer in self.racers.values():
                            if not racer.is_bot and racer.bet > 0:
                                await add_balance(racer.user_id, racer.bet, conn)
                                logger.info(
                                    f"Race {self.room_code}: refunded {racer.bet} to user {racer.user_id}"
                                )
            except Exception as refund_err:
                logger.error(f"Race {self.room_code} refund also failed: {refund_err}")
            await self.broadcast({
                'type':    'error',
                'message': 'Payout system error — your bet has been refunded. Contact support.',
            })
            return

        await self.broadcast({
            'type':      'race_results',
            'results':   results,
            'total_pot': total_pot,
            'net_pot':   net_pot,
            'payouts':   self.payouts,
        })

        # Keep room alive for 30s for spectators to see results
        await asyncio.sleep(30)
        async with _race_room_lock:
            _race_rooms.pop(self.room_code, None)


# ============================================================
# ROOM REGISTRY
# ============================================================

_race_rooms:     Dict[str, RaceRoom]   = {}
_race_room_lock  = asyncio.Lock()

def _find_open_room(bet: float) -> Optional[RaceRoom]:
    """Find a lobby room with matching bet size that isn't full."""
    for room in _race_rooms.values():
        if (room.phase == 'lobby'
                and room.real_player_count < MAX_PLAYERS
                and room.real_player_count > 0):
            # Bet tolerance: within 10× of smallest bet in room
            bets_in_room = [r.bet for r in room.racers.values() if not r.is_bot]
            if bets_in_room:
                min_b = min(bets_in_room)
                max_b = max(bets_in_room)
                # Allow joining if bet is within 10× range of existing bets
                if min_b * 0.1 <= bet <= max_b * 10:
                    return room
    return None

def _new_room_code() -> str:
    import string
    chars = string.ascii_uppercase + string.digits
    return ''.join(secure_choice(chars) for _ in range(6))

# ============================================================
# HTTP ENDPOINTS
# ============================================================

class JoinRaceRequest(BaseModel):
    amount:    float
    agent_id:  str = 'sas'   # chosen agent
    room_code: Optional[str] = None   # quick-join: land in a friend's specific open room

@router.post("/join")
async def join_race(req: JoinRaceRequest, request: Request):
    """Join or create a race room."""
    user_id  = await require_auth(request)
    await require_game_enabled("live-race")
    bet      = clamp_bet(req.amount)
    agent_id = req.agent_id if req.agent_id in AGENT_PROFILES else 'sas'

    # Acquire room lock before DB connection to avoid holding a pool connection
    # while suspended waiting for the lock (pool exhaustion risk).
    async with _race_room_lock:
        for existing_room in _race_rooms.values():
            if user_id in existing_room.racers and existing_room.phase in ('lobby', 'racing'):
                raise HTTPException(400, "Already in an active race room")

        pool = await get_db()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ensure_user_exists(user_id, conn=conn)
                user = await conn.fetchrow(
                    "SELECT username, balance FROM users WHERE user_id=$1", user_id
                )

                room = None
                if req.room_code:
                    candidate = _race_rooms.get(req.room_code)
                    if (candidate and candidate.phase == 'lobby'
                            and candidate.real_player_count < MAX_PLAYERS):
                        room = candidate
                if not room:
                    room = _find_open_room(bet)
                if room and agent_id in {r.agent_id for r in room.racers.values()}:
                    # Pick different agent automatically
                    used = {r.agent_id for r in room.racers.values()}
                    free = [a for a in AGENT_PROFILES if a not in used]
                    agent_id = free[0] if free else agent_id

                is_new_room = False
                if not room:
                    # Create room object but don't register until balance confirmed
                    code = _new_room_code()
                    room = RaceRoom(code)
                    is_new_room = True
                else:
                    # Joining an existing room that bots may have filled —
                    # evict one bot to keep total at MAX_PLAYERS
                    if room.total_count >= MAX_PLAYERS:
                        bot_id = next(
                            (uid for uid, r in room.racers.items() if r.is_bot), None
                        )
                        if bot_id is None:
                            raise HTTPException(400, "Race room is full")
                        del room.racers[bot_id]

                if not await deduct_balance(user_id, bet, conn):
                    raise HTTPException(400, "Insufficient balance")

                # Balance confirmed — safe to register room and racer
                if is_new_room:
                    _race_rooms[room.room_code] = room

                racer = Racer(
                    user_id  = user_id,
                    username = user['username'] or f'Player {user_id}',
                    agent_id = agent_id,
                    bet      = bet,
                    is_bot   = False,
                )
                room.racers[user_id] = racer

        # Spawn the room task outside the transaction, still inside the room lock
        if is_new_room:
            room.task = asyncio.create_task(room.run())

    return {
        "success":    True,
        "room_code":  room.room_code,
        "agent_id":   agent_id,
        "agent":      AGENT_PROFILES[agent_id],
        "bet":        bet,
        "lobby_secs": LOBBY_SECS,
    }


@router.get("/rooms")
async def list_rooms():
    """List active rooms for the lobby view."""
    async with _race_room_lock:
        rooms = [
        {
            'room_code':    code,
            'phase':        room.phase,
            'player_count': room.real_player_count,
            'total_count':  room.total_count,
            'created_at':   room.created_at,
        }
            for code, room in _race_rooms.items()
            if room.phase in ('lobby', 'racing')
        ]
    return rooms


@router.get("/agents")
async def list_agents():
    """Return all agent profiles for the selection screen."""
    return [
        {
            'id':          agent_id,
            'name':        p['name'],
            'emoji':       p['emoji'],
            'color':       p['color'],
            'personality': p['personality'],
            'base_speed':  p['base_speed'],
            'burst_chance': p['burst_chance'],
        }
        for agent_id, p in AGENT_PROFILES.items()
    ]


@router.get("/history")
async def race_history(request: Request):
    """Recent race results for the current user."""
    user_id = await require_auth(request)
    pool    = await get_db()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM game_logs
            WHERE user_id=$1 AND game_type='live_race'
            ORDER BY created_at DESC LIMIT 20
        """, user_id)
    return [convert_decimals(dict(r)) for r in rows]


# ============================================================
# WEBSOCKET
# ============================================================

@router.websocket("/ws/{room_code}")
async def race_ws(websocket: WebSocket, room_code: str):
    """
    Primary real-time channel for a race room.
    Players and spectators both connect here.
    Server drives all state — client is read-only except for reactions.
    """
    await websocket.accept()

    token = websocket.cookies.get("session_token")
    session = shared.get_session(token) if token else None
    if not session:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    user_id = session["user_id"]
    async with _race_room_lock:
        room = _race_rooms.get(room_code)

    if not room:
        try:
            await websocket.send_json({
                'type':    'error',
                'message': 'Room not found or race has ended',
            })
        except Exception:
            pass
        await websocket.close()
        return

    room.add_ws(user_id, websocket)
    is_participant = user_id in room.racers

    # Send full room state on connect
    try:
        await websocket.send_json({
            'type':        'room_state',
            'room_code':   room_code,
            'phase':       room.phase,
            'your_agent':  room.racers[user_id].agent_id if is_participant else None,
            'racers':      [r.to_dict() for r in room.racers.values()],
            'finish_order': room.finish_order,
        })
    except Exception:
        pass

    try:
        while True:
            raw = await websocket.receive_json()
            msg_type = raw.get('type')

            # Reaction emoji during race
            if msg_type == 'reaction':
                rxn = raw.get('emoji', '')
                if rxn in ('🔥', '😱', '💨', '👑', '💀', '🍀'):
                    await room.broadcast({
                        'type':    'reaction',
                        'user_id': user_id,
                        'emoji':   rxn,
                    })

            # Spectator cheer (different from player reaction)
            elif msg_type == 'cheer':
                agent_cheering_for = raw.get('agent_id', '')
                if agent_cheering_for in AGENT_PROFILES:
                    await room.broadcast({
                        'type':    'cheer',
                        'user_id': user_id,
                        'agent':   agent_cheering_for,
                        'emoji':   AGENT_PROFILES[agent_cheering_for]['emoji'],
                    })

            elif msg_type == 'ping':
                try:
                    await websocket.send_json({'type': 'pong'})
                except Exception:
                    break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"Race WS disconnect: {e}")
    finally:
        room.remove_ws(user_id, websocket)

# ============================================================
# DB TABLE INIT  (called from server.py lifespan if needed)
# ============================================================

async def init_race_tables():
    pool = await get_db()
    async with pool.acquire() as conn:
        # race_rooms and race_participants are created in server.py
        # This ensures the game_logs table has the right index
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_game_logs_race
            ON game_logs (user_id, game_type)
            WHERE game_type = 'live_race'
        """)
    logger.info("✅ Race tables/indexes ready")


