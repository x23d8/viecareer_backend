"""
Multi-layer rate limiting for VieCareer backend.

Layers implemented:
  1. IP limit        — slowapi, blocks spam from a single IP
  2. User limit      — in-memory per Clerk user-id token bucket
  3. Endpoint limit  — stricter slowapi limits on AI / upload / score routes
  4. Daily quota     — in-memory OpenAI cost guard (requests per day)
  5. Concurrency     — asyncio.Semaphore caps parallel AI calls
  6. WebSocket limit — active WS connection counter per IP
  7. Payload limit   — enforced in middleware (see middleware.py)
"""

import asyncio
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field

from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger("viecareer")

# ---------------------------------------------------------------------------
# 1. IP Rate Limiter (slowapi) — shared singleton used by all routers
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
"""
Default: 120 requests/min per IP across all routes.
Tighter overrides are applied per-endpoint with @limiter.limit().
"""

# ---------------------------------------------------------------------------
# 2. User-level rate limit — sliding-window token bucket (in-memory)
# ---------------------------------------------------------------------------

@dataclass
class _UserBucket:
    """Sliding window counter for a single user."""
    count: int = 0
    window_start: float = field(default_factory=time.monotonic)


# user_id -> bucket
_user_buckets: dict[str, _UserBucket] = defaultdict(lambda: _UserBucket())
_user_lock = asyncio.Lock()

# Limits: max N requests per W seconds
_USER_WINDOW_SEC = 60
_USER_GENERAL_MAX = 60       # 60 req/min for normal endpoints
_USER_AI_MAX = 20            # 20 req/min for AI endpoints
_USER_UPLOAD_MAX = 10        # 10 req/min for upload
_USER_SCORE_MAX = 10         # 10 req/min for scoring / report save


async def check_user_limit(user_id: str, max_requests: int = _USER_GENERAL_MAX) -> None:
    """
    Raise a 429 HTTPException if this user has exceeded max_requests
    within the last _USER_WINDOW_SEC seconds.
    """
    from fastapi import HTTPException, status

    if not user_id:
        return  # unauthenticated path — IP limiter handles it

    async with _user_lock:
        bucket = _user_buckets[user_id]
        now = time.monotonic()
        if now - bucket.window_start >= _USER_WINDOW_SEC:
            bucket.count = 0
            bucket.window_start = now
        bucket.count += 1
        if bucket.count > max_requests:
            wait = int(_USER_WINDOW_SEC - (now - bucket.window_start)) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": True,
                    "code": "USER_RATE_LIMIT",
                    "message": f"Too many requests. Please wait {wait}s before retrying.",
                    "retry_after": wait,
                },
                headers={"Retry-After": str(wait)},
            )


# Convenience aliases for callers
async def check_user_limit_ai(user_id: str) -> None:
    await check_user_limit(user_id, _USER_AI_MAX)

async def check_user_limit_upload(user_id: str) -> None:
    await check_user_limit(user_id, _USER_UPLOAD_MAX)

async def check_user_limit_score(user_id: str) -> None:
    await check_user_limit(user_id, _USER_SCORE_MAX)


# ---------------------------------------------------------------------------
# 4. Daily quota — per-day OpenAI request guard (in-memory)
# ---------------------------------------------------------------------------

@dataclass
class _DailyQuota:
    date_str: str = ""
    count: int = 0


_daily_quota = _DailyQuota()
_daily_lock = asyncio.Lock()

# Hard cap: total AI calls across ALL users per calendar day (UTC)
DAILY_AI_QUOTA = int(os.getenv("DAILY_AI_QUOTA", "2000"))  # adjust to your budget


async def check_daily_quota() -> None:
    """Raise 429 if the backend-wide daily OpenAI call quota is exhausted."""
    from fastapi import HTTPException, status

    today = time.strftime("%Y-%m-%d", time.gmtime())
    async with _daily_lock:
        if _daily_quota.date_str != today:
            _daily_quota.date_str = today
            _daily_quota.count = 0
        _daily_quota.count += 1
        if _daily_quota.count > DAILY_AI_QUOTA:
            logger.warning("Daily AI quota exhausted (%d/%d)", _daily_quota.count, DAILY_AI_QUOTA)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": True,
                    "code": "DAILY_QUOTA_EXHAUSTED",
                    "message": "Service daily capacity reached. Please try again tomorrow.",
                    "retry": False,
                },
            )


# ---------------------------------------------------------------------------
# 5. Concurrency semaphore — cap simultaneous AI calls
# ---------------------------------------------------------------------------

_AI_CONCURRENCY = 10  # max parallel OpenAI requests across all users
_ai_semaphore = asyncio.Semaphore(_AI_CONCURRENCY)


class ai_concurrency_limit:  # noqa: N801 — used as async context manager
    """
    Async context manager that acquires the AI concurrency semaphore.
    Raises 503 immediately if all slots are occupied (non-blocking).

    Usage:
        async with ai_concurrency_limit():
            result = await call_openai(...)
    """

    async def __aenter__(self):
        from fastapi import HTTPException, status

        acquired = _ai_semaphore._value > 0  # fast path check
        if not acquired:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": True,
                    "code": "AI_BUSY",
                    "message": "AI service is at capacity. Please try again in a moment.",
                    "retry": True,
                },
            )
        await _ai_semaphore.acquire()
        return self

    async def __aexit__(self, *_):
        _ai_semaphore.release()


# ---------------------------------------------------------------------------
# 6. WebSocket connection limiter
# ---------------------------------------------------------------------------

_ws_lock = asyncio.Lock()
_ws_connections: dict[str, int] = defaultdict(int)  # ip -> active count

WS_MAX_PER_IP = 3      # max concurrent WS connections per IP
WS_MAX_TOTAL = 50      # absolute cap on all active WS sessions


async def acquire_ws_slot(ip: str) -> None:
    """Register a new WebSocket connection; raise 429 if limits are hit."""
    from fastapi import HTTPException, status

    async with _ws_lock:
        total = sum(_ws_connections.values())
        if total >= WS_MAX_TOTAL:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": True,
                    "code": "WS_CAPACITY",
                    "message": "Realtime service is at full capacity. Please try again shortly.",
                    "retry": True,
                },
            )
        if _ws_connections[ip] >= WS_MAX_PER_IP:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": True,
                    "code": "WS_IP_LIMIT",
                    "message": f"Maximum {WS_MAX_PER_IP} concurrent realtime sessions per IP.",
                    "retry": False,
                },
            )
        _ws_connections[ip] += 1
        logger.debug("WS slot acquired ip=%s total=%d", ip, total + 1)


async def release_ws_slot(ip: str) -> None:
    """Release a previously acquired WebSocket slot."""
    async with _ws_lock:
        if _ws_connections[ip] > 0:
            _ws_connections[ip] -= 1
        logger.debug("WS slot released ip=%s remaining=%d", ip, _ws_connections[ip])
