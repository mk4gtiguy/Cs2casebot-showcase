import secrets
import string
from asyncpg.exceptions import UniqueViolationError
from fastapi import APIRouter, HTTPException, Request
from shared import get_db, require_auth, add_balance, logger, check_rate_limit, RATE_WRITE

router = APIRouter()

_REFERRAL_REWARD_REFERRER_BALANCE = 500.0
_REFERRAL_REWARD_REFERRED_BALANCE = 500.0
_REFERRAL_REWARD_REFERRER_TICKETS = 1

_CODE_CHARS = string.ascii_uppercase + string.digits

def _gen_code() -> str:
    return ''.join(secrets.choice(_CODE_CHARS) for _ in range(7))


@router.get("/api/referral/info")
async def referral_info(request: Request):
    user_id = await require_auth(request)
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT referral_code, referred_by FROM users WHERE user_id=$1", user_id
        )
        if not row:
            raise HTTPException(404, "User not found")

        code = row["referral_code"]
        if not code:
            # Generate unique code lazily on first request
            for _ in range(100):  # generous retry budget for astronomically rare collisions
                candidate = _gen_code()
                try:
                    await conn.execute(
                        "UPDATE users SET referral_code=$1 WHERE user_id=$2 AND referral_code IS NULL",
                        candidate, user_id,
                    )
                    # Re-fetch in case another request beat us
                    code = await conn.fetchval(
                        "SELECT referral_code FROM users WHERE user_id=$1", user_id
                    )
                    if code:
                        break
                except UniqueViolationError:
                    continue

        referral_count = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=$1", user_id
        ) or 0

        return {
            "code": code,
            "referral_count": int(referral_count),
            "total_earned_balance": float(referral_count) * _REFERRAL_REWARD_REFERRER_BALANCE,
            "total_earned_tickets": int(referral_count) * _REFERRAL_REWARD_REFERRER_TICKETS,
            "already_referred": row["referred_by"] is not None,
        }


@router.post("/api/referral/apply")
async def apply_referral(request: Request):
    user_id = await require_auth(request)
    await check_rate_limit(request, RATE_WRITE)
    body = await request.json()
    code = (body.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(400, "No referral code provided")

    pool = await get_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                "SELECT referred_by FROM users WHERE user_id=$1 FOR UPDATE", user_id
            )
            if not user:
                raise HTTPException(404, "User not found")
            if user["referred_by"] is not None:
                raise HTTPException(400, "You have already used a referral code")

            referrer = await conn.fetchrow(
                "SELECT user_id FROM users WHERE referral_code=$1", code
            )
            if not referrer:
                raise HTTPException(400, "Invalid referral code")
            referrer_id = referrer["user_id"]
            if referrer_id == user_id:
                raise HTTPException(400, "You cannot use your own referral code")

            # Record and reward
            await conn.execute(
                "INSERT INTO referrals (referrer_id, referred_id) VALUES ($1, $2)",
                referrer_id, user_id,
            )
            await conn.execute(
                "UPDATE users SET referred_by=$1 WHERE user_id=$2", referrer_id, user_id
            )

            # Reward the new user
            await add_balance(user_id, _REFERRAL_REWARD_REFERRED_BALANCE, conn)

            # Reward the referrer: balance + ticket
            await add_balance(referrer_id, _REFERRAL_REWARD_REFERRER_BALANCE, conn)
            await conn.execute(
                "UPDATE users SET tickets = COALESCE(tickets, 0) + $1 WHERE user_id=$2",
                _REFERRAL_REWARD_REFERRER_TICKETS, referrer_id,
            )
            await conn.execute(
                """INSERT INTO ticket_transactions (user_id, amount, source, metadata)
                   VALUES ($1, $2, 'referral_reward', '{"action": "referral"}')""",
                referrer_id, _REFERRAL_REWARD_REFERRER_TICKETS,
            )

    logger.info(f"Referral applied: {user_id} used code {code} (referrer={referrer_id})")
    return {
        "success": True,
        "reward": _REFERRAL_REWARD_REFERRED_BALANCE,
        "message": f"Code applied! You received ${_REFERRAL_REWARD_REFERRED_BALANCE:.0f}.",
    }
