"""Subscription plan quota enforcement."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status

from config import APP_ENV
from core.database import get_connection

logger = logging.getLogger("viecareer")

FEATURE_CV_JD_ANALYSIS = "cv_jd_analysis"
FEATURE_REALTIME_INTERVIEW = "realtime_interview"
FEATURE_CHATBOT = "chatbot"
FEATURE_CODING_PLAN = "coding_plan"
FEATURE_AI_CODE_FEEDBACK = "ai_code_feedback"

METRIC_COUNT = "count"
METRIC_SESSION_COUNT = "session_count"
METRIC_SESSION_MINUTES = "session_minutes"
METRIC_MESSAGE_COUNT = "message_count"
METRIC_GENERATION_COUNT = "generation_count"
METRIC_ATTEMPT_COUNT = "attempt_count"

PLAN_USAGE_BASED = "usage_based"
QUOTA_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")

_schema_lock = Lock()
_schema_ready = False


@dataclass(frozen=True)
class _Plan:
    id: int | None
    code: str
    name: str
    billing_period: str = "month"
    included_members: int = 1
    subscription_id: int | None = None
    subscription_started_at: datetime | None = None
    subscription_ends_at: datetime | None = None


@dataclass(frozen=True)
class _Limit:
    feature_code: str
    metric: str
    period: str
    limit_value: int | None


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    plan_code: str
    plan_name: str
    feature_code: str
    metric: str
    period: str
    limit_value: int | None
    used: int
    remaining: int | None
    reset_at: datetime | None

    def payload(self) -> dict[str, Any]:
        return {
            "plan_code": self.plan_code,
            "plan_name": self.plan_name,
            "feature_code": self.feature_code,
            "metric": self.metric,
            "period": self.period,
            "limit": self.limit_value,
            "used": self.used,
            "remaining": self.remaining,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
        }


_DEFAULT_PLANS: tuple[tuple[str, str, str, int, int, str, int], ...] = (
    ("free", "Free / Freemium", "0.00", 3, 20, "month", 1),
    ("standard", "Standard", "99000.00", 15, 50, "month", 1),
    ("pro", "Pro / Premium", "299000.00", 30, 50, "month", 1),
    ("team", "Team", "1199000.00", 30, 100, "month", 5),
    ("usage_based", "Usage-Based", "29000.00", 1, 20, "usage", 1),
)

_DEFAULT_LIMITS: tuple[tuple[str, str, str, str, int | None], ...] = (
    # Free allowances are one-time trials. A calendar boundary, logout, plan
    # expiry, or a new request must never restore them.
    ("free", FEATURE_CV_JD_ANALYSIS, METRIC_COUNT, "lifetime", 3),
    ("free", FEATURE_REALTIME_INTERVIEW, METRIC_SESSION_COUNT, "lifetime", 3),
    ("free", FEATURE_REALTIME_INTERVIEW, METRIC_SESSION_MINUTES, "session", 10),
    ("free", FEATURE_CHATBOT, METRIC_MESSAGE_COUNT, "lifetime", 3),
    ("free", FEATURE_CODING_PLAN, METRIC_GENERATION_COUNT, "lifetime", 3),
    ("free", FEATURE_AI_CODE_FEEDBACK, METRIC_ATTEMPT_COUNT, "lifetime", 3),
    ("standard", FEATURE_CV_JD_ANALYSIS, METRIC_COUNT, "week", 15),
    ("standard", FEATURE_REALTIME_INTERVIEW, METRIC_SESSION_COUNT, "week", 10),
    ("standard", FEATURE_REALTIME_INTERVIEW, METRIC_SESSION_MINUTES, "session", 25),
    ("standard", FEATURE_CHATBOT, METRIC_MESSAGE_COUNT, "day", 50),
    ("standard", FEATURE_CODING_PLAN, METRIC_GENERATION_COUNT, "week", 10),
    ("standard", FEATURE_AI_CODE_FEEDBACK, METRIC_ATTEMPT_COUNT, "week", 10),
    ("pro", FEATURE_CV_JD_ANALYSIS, METRIC_COUNT, "week", 30),
    ("pro", FEATURE_REALTIME_INTERVIEW, METRIC_SESSION_COUNT, "week", 20),
    ("pro", FEATURE_REALTIME_INTERVIEW, METRIC_SESSION_MINUTES, "session", 45),
    ("pro", FEATURE_CHATBOT, METRIC_MESSAGE_COUNT, "day", 150),
    ("pro", FEATURE_CODING_PLAN, METRIC_GENERATION_COUNT, "week", 15),
    ("pro", FEATURE_AI_CODE_FEEDBACK, METRIC_ATTEMPT_COUNT, "week", 15),
    ("team", FEATURE_CV_JD_ANALYSIS, METRIC_COUNT, "week", 30),
    ("team", FEATURE_REALTIME_INTERVIEW, METRIC_SESSION_COUNT, "week", 20),
    ("team", FEATURE_REALTIME_INTERVIEW, METRIC_SESSION_MINUTES, "session", 45),
    ("team", FEATURE_CHATBOT, METRIC_MESSAGE_COUNT, "day", 150),
    ("team", FEATURE_CODING_PLAN, METRIC_GENERATION_COUNT, "week", 15),
    ("team", FEATURE_AI_CODE_FEEDBACK, METRIC_ATTEMPT_COUNT, "week", 15),
    ("usage_based", FEATURE_REALTIME_INTERVIEW, METRIC_SESSION_COUNT, "purchase", 1),
    ("usage_based", FEATURE_REALTIME_INTERVIEW, METRIC_SESSION_MINUTES, "session", 15),
)

_API_USAGE_FEATURES: dict[tuple[str, str], tuple[str, ...]] = {
    (FEATURE_CV_JD_ANALYSIS, METRIC_COUNT): ("cvjd_extract",),
    (FEATURE_CV_JD_ANALYSIS, METRIC_ATTEMPT_COUNT): ("cvjd_extract",),
    (FEATURE_CHATBOT, METRIC_MESSAGE_COUNT): ("chat_viebot",),
    (FEATURE_CODING_PLAN, METRIC_GENERATION_COUNT): ("coding_plan",),
    (FEATURE_AI_CODE_FEEDBACK, METRIC_ATTEMPT_COUNT): ("coding_feedback",),
}

_USAGE_BASED_FEATURES = {
    FEATURE_REALTIME_INTERVIEW,
}

_QUOTA_SUMMARY_FEATURES: tuple[tuple[str, str], ...] = (
    (FEATURE_CV_JD_ANALYSIS, METRIC_COUNT),
    (FEATURE_CHATBOT, METRIC_MESSAGE_COUNT),
    (FEATURE_CODING_PLAN, METRIC_GENERATION_COUNT),
    (FEATURE_AI_CODE_FEEDBACK, METRIC_ATTEMPT_COUNT),
)

_PLAN_PRIORITY = {
    "team": 400,
    "pro": 300,
    "standard": 200,
    "free": 100,
    PLAN_USAGE_BASED: 50,
}


async def check_plan_quota(
    *,
    external_auth_id: str,
    feature_code: str,
    metric: str,
    requested_units: int = 1,
) -> QuotaDecision:
    """Raise HTTP 429 when the authenticated user has exhausted plan quota."""

    if APP_ENV in {"development", "dev"}:
        return _unlimited_decision("development", "Development", feature_code, metric)

    return await asyncio.to_thread(
        _check_plan_quota_sync,
        external_auth_id=external_auth_id,
        feature_code=feature_code,
        metric=metric,
        requested_units=requested_units,
    )


async def get_plan_quota_summary(*, external_auth_id: str) -> list[dict[str, Any]]:
    """Return usage/remaining counters for user-visible plan quotas."""

    return await asyncio.to_thread(
        get_plan_quota_summary_sync,
        external_auth_id=external_auth_id,
    )


def get_plan_quota_summary_sync(*, external_auth_id: str) -> list[dict[str, Any]]:
    """Synchronous quota summary helper for non-async routers/services."""

    if not external_auth_id:
        return []
    if APP_ENV in {"development", "dev"}:
        return [
            _unlimited_decision("development", "Development", feature, metric).payload()
            for feature, metric in _QUOTA_SUMMARY_FEATURES
        ]

    with get_connection() as conn:
        with conn.cursor() as cur:
            _ensure_quota_schema(cur)
            summary = [
                _quota_decision_sync(
                    cur,
                    external_auth_id=external_auth_id,
                    feature_code=feature_code,
                    metric=metric,
                ).payload()
                for feature_code, metric in _QUOTA_SUMMARY_FEATURES
            ]
        conn.commit()

    return summary


async def get_realtime_session_limit_seconds(*, external_auth_id: str) -> int | None:
    """Return the current user's per-interview time limit in seconds."""

    if APP_ENV in {"development", "dev"}:
        return None

    minutes = await asyncio.to_thread(
        _get_limit_value_sync,
        external_auth_id=external_auth_id,
        feature_code=FEATURE_REALTIME_INTERVIEW,
        metric=METRIC_SESSION_MINUTES,
    )
    return minutes * 60 if minutes is not None else None


def ensure_plan_quota_schema() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            _ensure_quota_schema(cur)
        conn.commit()


def _check_plan_quota_sync(
    *,
    external_auth_id: str,
    feature_code: str,
    metric: str,
    requested_units: int,
) -> QuotaDecision:
    if not external_auth_id:
        return _unlimited_decision("free", "Free / Freemium", feature_code, metric)

    with get_connection() as conn:
        with conn.cursor() as cur:
            _ensure_quota_schema(cur)
            decision = _quota_decision_sync(
                cur,
                external_auth_id=external_auth_id,
                feature_code=feature_code,
                metric=metric,
                requested_units=requested_units,
            )

            if not decision.allowed and decision.plan_code == PLAN_USAGE_BASED:
                decision = _free_plan_decision(cur, external_auth_id, feature_code, metric, requested_units)
            conn.commit()

    if not decision.allowed:
        _raise_quota_exceeded(decision)
    return decision


def _quota_decision_sync(
    cur,
    *,
    external_auth_id: str,
    feature_code: str,
    metric: str,
    requested_units: int = 1,
) -> QuotaDecision:
    plan = _effective_plan(cur, external_auth_id, feature_code)
    limit = _find_limit(cur, plan.code, feature_code, metric)
    if not limit:
        return _unlimited_decision(plan.code, plan.name, feature_code, metric)

    if limit.limit_value is None:
        return QuotaDecision(
            allowed=True,
            plan_code=plan.code,
            plan_name=plan.name,
            feature_code=feature_code,
            metric=metric,
            period=limit.period,
            limit_value=None,
            used=0,
            remaining=None,
            reset_at=None,
        )

    window_start, reset_at = _window_for_period(limit.period, plan)
    used = _usage_count(cur, external_auth_id, feature_code, metric, window_start)
    remaining = max(0, int(limit.limit_value) - used)
    return QuotaDecision(
        allowed=used + max(1, requested_units) <= int(limit.limit_value),
        plan_code=plan.code,
        plan_name=plan.name,
        feature_code=feature_code,
        metric=metric,
        period=limit.period,
        limit_value=int(limit.limit_value),
        used=used,
        remaining=remaining,
        reset_at=reset_at,
    )


def _free_plan_decision(
    cur,
    external_auth_id: str,
    feature_code: str,
    metric: str,
    requested_units: int,
) -> QuotaDecision:
    plan = _free_plan(cur)
    limit = _find_limit(cur, plan.code, feature_code, metric)
    if not limit or limit.limit_value is None:
        return _unlimited_decision(plan.code, plan.name, feature_code, metric)

    window_start, reset_at = _window_for_period(limit.period, plan)
    used = _usage_count(cur, external_auth_id, feature_code, metric, window_start)
    remaining = max(0, int(limit.limit_value) - used)
    return QuotaDecision(
        allowed=used + max(1, requested_units) <= int(limit.limit_value),
        plan_code=plan.code,
        plan_name=plan.name,
        feature_code=feature_code,
        metric=metric,
        period=limit.period,
        limit_value=int(limit.limit_value),
        used=used,
        remaining=remaining,
        reset_at=reset_at,
    )


def _get_limit_value_sync(*, external_auth_id: str, feature_code: str, metric: str) -> int | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            _ensure_quota_schema(cur)
            plan = _effective_plan(cur, external_auth_id, feature_code)
            limit = _find_limit(cur, plan.code, feature_code, metric)
            conn.commit()
            return None if not limit or limit.limit_value is None else int(limit.limit_value)


def _ensure_quota_schema(cur) -> None:
    global _schema_ready
    if _schema_ready:
        return

    with _schema_lock:
        if _schema_ready:
            return

        cur.execute("alter table plans add column if not exists billing_period varchar(20) not null default 'month'")
        cur.execute("alter table plans add column if not exists included_members int not null default 1")
        cur.execute(
            """
            create table if not exists plan_feature_limits (
              id bigserial primary key,
              plan_id bigint not null references plans(id) on delete cascade,
              feature_code varchar(60) not null,
              metric varchar(40) not null,
              period varchar(20) not null check (period in ('lifetime', 'day', 'week', 'month', 'session', 'purchase')),
              limit_value int null,
              created_at timestamptz not null default now(),
              updated_at timestamptz not null default now(),
              unique (plan_id, feature_code, metric, period)
            )
            """
        )
        cur.execute("alter table plan_feature_limits drop constraint if exists plan_feature_limits_period_check")
        cur.execute(
            """
            alter table plan_feature_limits
            add constraint plan_feature_limits_period_check
            check (period in ('lifetime', 'day', 'week', 'month', 'session', 'purchase'))
            """
        )
        cur.execute(
            """
            create index if not exists idx_plan_feature_limits_plan_feature
            on plan_feature_limits (plan_id, feature_code, metric)
            """
        )
        _seed_plans(cur)
        _seed_limits(cur)
        _schema_ready = True


def _seed_plans(cur) -> None:
    for code, name, price, quota_total, rate_limit, billing_period, included_members in _DEFAULT_PLANS:
        cur.execute(
            """
            insert into plans (
              code,
              name,
              monthly_price,
              quota_total,
              rate_limit_per_minute,
              billing_period,
              included_members
            )
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (code) do update
            set
              name = excluded.name,
              monthly_price = excluded.monthly_price,
              quota_total = excluded.quota_total,
              rate_limit_per_minute = excluded.rate_limit_per_minute,
              billing_period = excluded.billing_period,
              included_members = excluded.included_members
            """,
            (code, name, price, quota_total, rate_limit, billing_period, included_members),
        )


def _seed_limits(cur) -> None:
    # Reconcile the product catalogue exactly. Keeping an old monthly row next
    # to a new weekly/lifetime row makes LIMIT 1 non-deterministic and can
    # accidentally restore a trial allowance.
    cur.execute(
        """
        delete from plan_feature_limits l
        using plans p
        where l.plan_id = p.id
          and p.code = any(%s)
          and l.feature_code = any(%s)
        """,
        (
            [plan[0] for plan in _DEFAULT_PLANS],
            [
                FEATURE_CV_JD_ANALYSIS,
                FEATURE_REALTIME_INTERVIEW,
                FEATURE_CHATBOT,
                FEATURE_CODING_PLAN,
                FEATURE_AI_CODE_FEEDBACK,
            ],
        ),
    )
    for plan_code, feature_code, metric, period, limit_value in _DEFAULT_LIMITS:
        cur.execute(
            """
            insert into plan_feature_limits (
              plan_id,
              feature_code,
              metric,
              period,
              limit_value
            )
            select p.id, %s, %s, %s, %s
            from plans p
            where p.code = %s
            on conflict (plan_id, feature_code, metric, period) do update
            set limit_value = excluded.limit_value,
                updated_at = now()
            """,
            (feature_code, metric, period, limit_value, plan_code),
        )


def _effective_plan(cur, external_auth_id: str, feature_code: str) -> _Plan:
    active_plans = _active_plans(cur, external_auth_id)
    if not active_plans:
        return _free_plan(cur)

    usage_based = next((plan for plan in active_plans if plan.code == PLAN_USAGE_BASED), None)
    recurring_plans = [
        plan
        for plan in active_plans
        if plan.code != PLAN_USAGE_BASED and plan.code in _PLAN_PRIORITY
    ]
    recurring = max(recurring_plans, key=lambda plan: _PLAN_PRIORITY.get(plan.code, 0), default=None)

    if recurring and recurring.code != "free":
        return recurring
    if usage_based and feature_code in _USAGE_BASED_FEATURES:
        return usage_based
    return recurring or _free_plan(cur)


def _active_plans(cur, external_auth_id: str) -> list[_Plan]:
    cur.execute(
        """
        select
          p.id,
          p.code,
          p.name,
          coalesce(p.billing_period, 'month') as billing_period,
          coalesce(p.included_members, 1) as included_members,
          s.id as subscription_id,
          s.start_at as subscription_started_at,
          s.end_at as subscription_ends_at
        from users u
        join user_subscriptions s on s.user_id = u.id
        join plans p on p.id = s.plan_id
        where u.external_auth_id = %s
          and s.status = 'active'
          and s.start_at <= now()
          and (s.end_at is null or s.end_at > now())
        order by s.start_at desc, s.id desc
        """,
        (external_auth_id,),
    )
    return [_plan_from_row(row) for row in cur.fetchall()]


def _free_plan(cur) -> _Plan:
    cur.execute(
        """
        select
          id,
          code,
          name,
          coalesce(billing_period, 'month') as billing_period,
          coalesce(included_members, 1) as included_members
        from plans
        where code = 'free'
        limit 1
        """
    )
    row = cur.fetchone()
    if row:
        return _plan_from_row(row)
    return _Plan(id=None, code="free", name="Free / Freemium")


def _plan_from_row(row: dict[str, Any]) -> _Plan:
    return _Plan(
        id=int(row["id"]) if row.get("id") is not None else None,
        code=str(row.get("code") or "free"),
        name=str(row.get("name") or row.get("code") or "Free"),
        billing_period=str(row.get("billing_period") or "month"),
        included_members=max(1, int(row.get("included_members") or 1)),
        subscription_id=int(row["subscription_id"]) if row.get("subscription_id") is not None else None,
        subscription_started_at=row.get("subscription_started_at"),
        subscription_ends_at=row.get("subscription_ends_at"),
    )


def _find_limit(cur, plan_code: str, feature_code: str, metric: str) -> _Limit | None:
    cur.execute(
        """
        select l.feature_code, l.metric, l.period, l.limit_value
        from plan_feature_limits l
        join plans p on p.id = l.plan_id
        where p.code = %s
          and l.feature_code = %s
          and l.metric = %s
        limit 1
        """,
        (plan_code, feature_code, metric),
    )
    row = cur.fetchone()
    if not row:
        return None
    return _Limit(
        feature_code=str(row["feature_code"]),
        metric=str(row["metric"]),
        period=str(row["period"]),
        limit_value=None if row.get("limit_value") is None else int(row["limit_value"]),
    )


def _usage_count(
    cur,
    external_auth_id: str,
    feature_code: str,
    metric: str,
    window_start: datetime | None,
) -> int:
    if metric == METRIC_SESSION_MINUTES:
        return 0
    if feature_code == FEATURE_REALTIME_INTERVIEW:
        return _realtime_session_count(cur, external_auth_id, window_start)

    api_features = _API_USAGE_FEATURES.get((feature_code, metric), ())
    if not api_features:
        return 0
    return _api_usage_count(cur, external_auth_id, api_features, window_start)


def _api_usage_count(
    cur,
    external_auth_id: str,
    api_features: tuple[str, ...],
    window_start: datetime | None,
) -> int:
    cur.execute(
        """
        select count(*)::int as used
        from api_usage_logs l
        where l.external_auth_id = %s
          and l.status = 'success'
          and l.feature = any(%s)
          and (%s::timestamptz is null or l.occurred_at >= %s)
        """,
        (external_auth_id, list(api_features), window_start, window_start),
    )
    row = cur.fetchone()
    return int(row["used"] or 0) if row else 0


def _realtime_session_count(cur, external_auth_id: str, window_start: datetime | None) -> int:
    # Kept for compatibility with plan reports. Attempts are consumed at session
    # creation and never restored on cancellation.
    cur.execute(
        """
        select count(*)::int as used
        from interview_sessions s
        join users u on u.id = s.user_id
        where u.external_auth_id = %s
          and s.attempt_consumed = true
          and (%s::timestamptz is null or s.created_at >= %s)
        """,
        (external_auth_id, window_start, window_start),
    )
    row = cur.fetchone()
    return int(row["used"] or 0) if row else 0


def _window_for_period(period: str, plan: _Plan) -> tuple[datetime | None, datetime | None]:
    now = datetime.now(QUOTA_TIMEZONE)
    if period == "lifetime":
        return None, None
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        return _bound_window_to_subscription(start, start + timedelta(days=1), plan)
    if period == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)
        return _bound_window_to_subscription(start, start + timedelta(days=7), plan)
    if period == "month":
        local_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if local_start.month == 12:
            local_reset_at = local_start.replace(year=local_start.year + 1, month=1)
        else:
            local_reset_at = local_start.replace(month=local_start.month + 1)
        start = local_start.astimezone(timezone.utc)
        reset_at = local_reset_at.astimezone(timezone.utc)
        return _bound_window_to_subscription(start, reset_at, plan)
    if period == "purchase":
        return plan.subscription_started_at, plan.subscription_ends_at
    return None, None


def _bound_window_to_subscription(
    start: datetime,
    reset_at: datetime,
    plan: _Plan,
) -> tuple[datetime, datetime]:
    subscription_start = _as_aware_datetime(plan.subscription_started_at)
    subscription_end = _as_aware_datetime(plan.subscription_ends_at)
    if subscription_start:
        start = max(start, subscription_start)
    if subscription_end:
        reset_at = min(reset_at, subscription_end)
    return start, reset_at


def _as_aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _unlimited_decision(plan_code: str, plan_name: str, feature_code: str, metric: str) -> QuotaDecision:
    return QuotaDecision(
        allowed=True,
        plan_code=plan_code,
        plan_name=plan_name,
        feature_code=feature_code,
        metric=metric,
        period="unlimited",
        limit_value=None,
        used=0,
        remaining=None,
        reset_at=None,
    )


def _raise_quota_exceeded(decision: QuotaDecision) -> None:
    detail = {
        "error": True,
        "code": "PLAN_QUOTA_EXCEEDED",
        "message": _quota_message(decision),
        "retry": False,
        **decision.payload(),
    }
    raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


def _quota_message(decision: QuotaDecision) -> str:
    period_label = {
        "lifetime": "lifetime",
        "day": "daily",
        "week": "weekly",
        "month": "monthly",
        "purchase": "purchase",
        "session": "session",
    }.get(decision.period, decision.period)
    return (
        f"{decision.plan_name} {period_label} quota reached for "
        f"{decision.feature_code}.{decision.metric}."
    )
