"""Plan interview quotas plus non-expiring pay-per-attempt credits."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status

from core.database import get_connection

INTERVIEW_ATTEMPT_PRICE_VND = 29_000
INTERVIEW_ATTEMPT_PLAN_CODE = "usage_based"
QUOTA_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

# These values intentionally mirror the public pricing table. Free attempts
# are lifetime trials, paid-plan allowances renew every calendar week, and
# purchased add-ons never reset or expire.
PLAN_INCLUDED_ATTEMPTS: dict[str, int] = {
    "free": 3,
    "standard": 10,
    "pro": 20,
    "team": 20,
}
PLAN_MAX_SESSION_MINUTES: dict[str, int] = {
    "free": 10,
    "standard": 25,
    "pro": 45,
    "team": 45,
}
PURCHASED_ATTEMPT_MAX_SESSION_MINUTES = 15

AttemptSource = Literal["included", "purchased"]

_schema_lock = Lock()
_schema_ready = False


@dataclass(frozen=True)
class _AttemptPlan:
    code: str
    name: str
    included: int
    period_start: datetime | None
    period_end: datetime | None


@dataclass(frozen=True)
class InterviewAttemptStatus:
    plan_code: str
    plan_name: str
    included: int
    included_used: int
    purchased: int
    purchased_used: int
    remaining: int
    period_start: datetime | None = None
    period_end: datetime | None = None
    price_vnd: int = INTERVIEW_ATTEMPT_PRICE_VND

    @property
    def total(self) -> int:
        return self.included + self.purchased

    @property
    def used(self) -> int:
        return self.included_used + self.purchased_used

    @property
    def included_remaining(self) -> int:
        return max(0, self.included - self.included_used)

    @property
    def purchased_remaining(self) -> int:
        return max(0, self.purchased - self.purchased_used)

    @property
    def plan_max_session_minutes(self) -> int:
        return PLAN_MAX_SESSION_MINUTES.get(self.plan_code, PLAN_MAX_SESSION_MINUTES["free"])

    @property
    def next_attempt_max_session_minutes(self) -> int:
        if self.included_remaining > 0:
            return self.plan_max_session_minutes
        return PURCHASED_ATTEMPT_MAX_SESSION_MINUTES

    def payload(self) -> dict[str, Any]:
        return {
            "plan_code": self.plan_code,
            "plan_name": self.plan_name,
            # Keep "initial" for older clients; it means the allowance included
            # in the currently effective plan.
            "initial": self.included,
            "included": self.included,
            "included_used": self.included_used,
            "included_remaining": self.included_remaining,
            "purchased": self.purchased,
            "purchased_used": self.purchased_used,
            "purchased_remaining": self.purchased_remaining,
            "total": self.total,
            "used": self.used,
            "remaining": self.remaining,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "plan_max_session_minutes": self.plan_max_session_minutes,
            "purchased_max_session_minutes": PURCHASED_ATTEMPT_MAX_SESSION_MINUTES,
            "next_attempt_max_session_minutes": self.next_attempt_max_session_minutes,
            "price_vnd": self.price_vnd,
            "currency": "VND",
            "refundable": False,
        }

    def quota_payload(self) -> dict[str, Any]:
        period = "lifetime" if self.plan_code == "free" else "week"
        return {
            "plan_code": self.plan_code,
            "plan_name": self.plan_name,
            "feature_code": "realtime_interview",
            "metric": "session_count",
            "period": period,
            "limit": self.total,
            "used": self.used,
            "remaining": self.remaining,
            "reset_at": self.period_end.isoformat() if self.period_end else None,
        }


@dataclass(frozen=True)
class InterviewAttemptReservation:
    source: AttemptSource
    status_before: InterviewAttemptStatus

    @property
    def max_session_minutes(self) -> int:
        if self.source == "purchased":
            return PURCHASED_ATTEMPT_MAX_SESSION_MINUTES
        return self.status_before.plan_max_session_minutes

    @property
    def plan_code(self) -> str:
        return self.status_before.plan_code


async def get_interview_attempt_status(*, external_auth_id: str) -> InterviewAttemptStatus:
    return await asyncio.to_thread(
        get_interview_attempt_status_sync,
        external_auth_id=external_auth_id,
    )


def get_interview_attempt_status_sync(*, external_auth_id: str) -> InterviewAttemptStatus:
    if not external_auth_id:
        return _empty_status()

    ensure_interview_attempt_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select id from users where external_auth_id = %s limit 1",
                (external_auth_id,),
            )
            user = cur.fetchone()
            result = _status_for_user(cur, user["id"]) if user else _empty_status()
        conn.commit()
    return result


async def require_interview_attempt(*, external_auth_id: str) -> InterviewAttemptStatus:
    result = await get_interview_attempt_status(external_auth_id=external_auth_id)
    if result.remaining <= 0:
        _raise_attempts_exhausted(result)
    return result


def consume_interview_attempt(cur, *, user_id: Any) -> InterviewAttemptReservation:
    """Atomically choose and reserve a non-refundable attempt source.

    The session insert must occur in the caller's same transaction. Locking the
    user row serializes concurrent starts for one account.
    """

    cur.execute("select id from users where id = %s for update", (user_id,))
    if not cur.fetchone():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User profile was not found.")

    current = _status_for_user(cur, user_id)
    if current.included_remaining > 0:
        return InterviewAttemptReservation(source="included", status_before=current)
    if current.purchased_remaining > 0:
        return InterviewAttemptReservation(source="purchased", status_before=current)
    _raise_attempts_exhausted(current)


def _status_for_user(cur, user_id: Any) -> InterviewAttemptStatus:
    plan = _effective_attempt_plan(cur, user_id)
    cur.execute(
        """
        select
          (
            select count(*)::int
            from payment_transactions pt
            join plans p on p.id = pt.plan_id
            where pt.user_id = %s
              and pt.status = 'paid'
              and p.code = %s
          ) as purchased,
          (
            select count(*)::int
            from interview_sessions s
            where s.user_id = %s
              and s.attempt_consumed = true
              and coalesce(s.attempt_source, 'included') = 'included'
              and coalesce(s.attempt_plan_code, 'free') = %s
              and (%s::timestamptz is null or s.created_at >= %s)
              and (%s::timestamptz is null or s.created_at < %s)
          ) as included_used,
          (
            select count(*)::int
            from interview_sessions s
            where s.user_id = %s
              and s.attempt_consumed = true
              and s.attempt_source = 'purchased'
          ) as purchased_used
        """,
        (
            user_id,
            INTERVIEW_ATTEMPT_PLAN_CODE,
            user_id,
            plan.code,
            plan.period_start,
            plan.period_start,
            plan.period_end,
            plan.period_end,
            user_id,
        ),
    )
    row = cur.fetchone() or {}
    purchased = max(0, int(row.get("purchased") or 0))
    included_used = max(0, int(row.get("included_used") or 0))
    purchased_used = max(0, int(row.get("purchased_used") or 0))
    return InterviewAttemptStatus(
        plan_code=plan.code,
        plan_name=plan.name,
        included=plan.included,
        included_used=included_used,
        purchased=purchased,
        purchased_used=purchased_used,
        remaining=max(0, plan.included - included_used) + max(0, purchased - purchased_used),
        period_start=plan.period_start,
        period_end=plan.period_end,
    )


def _effective_attempt_plan(cur, user_id: Any) -> _AttemptPlan:
    cur.execute(
        """
        select
          active_plan.code,
          active_plan.name,
          active_plan.start_at,
          active_plan.end_at,
          u.free_interview_attempts_granted_at
        from users u
        left join lateral (
          select p.code, p.name, s.start_at, s.end_at, s.id
          from user_subscriptions s
          join plans p on p.id = s.plan_id
          where s.user_id = u.id
            and s.status = 'active'
            and s.start_at <= now()
            and (s.end_at is null or s.end_at > now())
            and p.code in ('standard', 'pro', 'team')
          order by
            case p.code
              when 'team' then 3
              when 'pro' then 2
              when 'standard' then 1
              else 0
            end desc,
            s.start_at desc,
            s.id desc
          limit 1
        ) active_plan on true
        where u.id = %s
        limit 1
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return _AttemptPlan(
            code="free",
            name="Free / Freemium",
            included=PLAN_INCLUDED_ATTEMPTS["free"],
            period_start=None,
            period_end=None,
        )
    code = str(row.get("code") or "free")
    if code == "free":
        return _AttemptPlan(
            code="free",
            name="Free / Freemium",
            included=PLAN_INCLUDED_ATTEMPTS["free"],
            period_start=_as_aware_datetime(row.get("free_interview_attempts_granted_at")),
            period_end=None,
        )
    now = datetime.now(QUOTA_TIMEZONE)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    week_end = week_start + timedelta(days=7)
    subscription_start = _as_aware_datetime(row.get("start_at"))
    subscription_end = _as_aware_datetime(row.get("end_at"))
    return _AttemptPlan(
        code=code,
        name=str(row.get("name") or code),
        included=PLAN_INCLUDED_ATTEMPTS.get(code, PLAN_INCLUDED_ATTEMPTS["free"]),
        period_start=max(week_start, subscription_start) if subscription_start else week_start,
        period_end=min(week_end, subscription_end) if subscription_end else week_end,
    )


def _empty_status() -> InterviewAttemptStatus:
    return InterviewAttemptStatus(
        plan_code="free",
        plan_name="Free / Freemium",
        included=PLAN_INCLUDED_ATTEMPTS["free"],
        included_used=0,
        purchased=0,
        purchased_used=0,
        remaining=PLAN_INCLUDED_ATTEMPTS["free"],
        period_start=None,
        period_end=None,
    )


def ensure_interview_attempt_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_interview_attempt_schema(cur)
            conn.commit()
        _schema_ready = True


def _ensure_interview_attempt_schema(cur) -> None:
    cur.execute(
        """
        alter table users
        add column if not exists free_interview_attempts_granted_at timestamptz not null default now()
        """
    )
    cur.execute(
        "alter table interview_sessions add column if not exists attempt_consumed boolean not null default false"
    )
    cur.execute(
        """
        alter table interview_sessions
        add column if not exists attempt_source varchar(20) null
        check (attempt_source in ('included', 'purchased'))
        """
    )
    cur.execute(
        "alter table interview_sessions add column if not exists attempt_plan_code varchar(50) null"
    )
    cur.execute(
        "alter table interview_sessions add column if not exists requested_question_count int null"
    )
    cur.execute(
        "alter table interview_sessions add column if not exists requested_duration_seconds int null"
    )
    cur.execute(
        """
        update interview_sessions
        set attempt_consumed = true,
            attempt_source = coalesce(attempt_source, 'included')
        where context_json is not null
          and started_at is not null
          and (attempt_consumed = false or attempt_source is null)
        """
    )
    cur.execute(
        """
        update interview_sessions s
        set attempt_plan_code = coalesce(
          case when s.attempt_source = 'purchased' then 'usage_based' end,
          (
            select p.code
            from user_subscriptions us
            join plans p on p.id = us.plan_id
            where us.user_id = s.user_id
              and p.code in ('standard', 'pro', 'team')
              and us.start_at <= s.created_at
              and (us.end_at is null or us.end_at > s.created_at)
            order by
              case p.code when 'team' then 3 when 'pro' then 2 when 'standard' then 1 else 0 end desc,
              us.start_at desc,
              us.id desc
            limit 1
          ),
          'free'
        )
        where s.attempt_consumed = true
          and s.attempt_plan_code is null
        """
    )
    cur.execute(
        """
        create index if not exists idx_interview_sessions_user_attempt_consumed
        on interview_sessions (user_id, attempt_consumed, attempt_source, created_at desc)
        """
    )
    cur.execute(
        """
        create index if not exists idx_interview_sessions_user_attempt_plan
        on interview_sessions (user_id, attempt_plan_code, attempt_consumed, created_at desc)
        """
    )


def _as_aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _raise_attempts_exhausted(current: InterviewAttemptStatus) -> None:
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "error": True,
            "code": "INTERVIEW_ATTEMPTS_EXHAUSTED",
            "message": "Bạn đã dùng hết lượt phỏng vấn trong gói. Mua thêm 1 lượt với giá 29.000đ để tiếp tục.",
            "retry": False,
            "feature_code": "realtime_interview",
            "checkout_plan_code": INTERVIEW_ATTEMPT_PLAN_CODE,
            **current.payload(),
        },
    )
