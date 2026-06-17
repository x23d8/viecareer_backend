import csv
import io
import logging
import uuid

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from core.api_usage import ensure_tracking_tables
from core.clerk_auth import AdminUser, require_admin
from core.database import get_connection
from core.rate_limit import limiter
from schemas.events import LandingMetricGroupBy, LandingSource
from services.admin_user_service import UserMetricGroupBy, get_admin_user_metrics, list_admin_users
from services.feature_tracking_service import FeatureGroupBy, get_feature_metrics
from services.landing_event_service import ensure_landing_events_schema, get_landing_metrics

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = logging.getLogger("viecareer")

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = logging.getLogger("viecareer")

GroupBy = Literal["day", "week", "month"]
UserStatus = Literal["active", "suspended", "pending"]
SessionStatus = Literal["completed", "in_progress", "scheduled", "cancelled"]
SessionType = Literal["technical", "behavioral", "mixed", "coding"]
ActivityType = Literal["Auth", "Session", "Payment", "Account", "System"]

FEATURE_DEFINITIONS: dict[str, dict[str, str]] = {
    "page_view": {
        "label": "Page views",
        "description": "Landing and product page visits",
        "kind": "traffic",
    },
    "upload_cv": {
        "label": "CV uploads",
        "description": "Files parsed through the upload endpoint",
        "kind": "activation",
    },
    "cv_jd_analysis": {
        "label": "CV/JD analysis",
        "description": "Resume review and CV/JD matching workflows",
        "kind": "ai",
    },
    "realtime_interview": {
        "label": "Mock interviews",
        "description": "Realtime interview sessions and related AI calls",
        "kind": "interview",
    },
    "coding_interview": {
        "label": "Coding practice",
        "description": "Coding plan generation and coding interview activity",
        "kind": "ai",
    },
    "action_plan": {
        "label": "Action plans",
        "description": "Interview preparation plans and next-step guidance",
        "kind": "planning",
    },
    "chatbot": {
        "label": "VieBot chat",
        "description": "Career assistant chat messages",
        "kind": "ai",
    },
    "pricing": {
        "label": "Pricing",
        "description": "Pricing page visits and plan click intent",
        "kind": "traffic",
    },
    "feedback": {
        "label": "Feedback reports",
        "description": "Interview scoring and saved reports",
        "kind": "assessment",
    },
}

FEATURE_KEYS = list(FEATURE_DEFINITIONS.keys())
ACTIVITY_TYPES: tuple[ActivityType, ...] = ("Auth", "Session", "Payment", "Account", "System")

ACTIVITY_FEATURE_CASE = """
case
  when event_name = 'file_parse' then 'upload_cv'
  when event_name in ('cvjd_saved', 'cvjd_cache_hit') then 'cv_jd_analysis'
  when event_name in ('interview_started', 'interview_completed', 'realtime_token_created') then 'realtime_interview'
  when event_name = 'report_saved' then 'feedback'
  else null
end
"""

ACTIVITY_COMPLETION_CASE = """
case
  when event_name = 'cvjd_saved' then 'cv_jd_analysis'
  when event_name = 'interview_completed' then 'realtime_interview'
  when event_name = 'report_saved' then 'feedback'
  else null
end
"""

API_FEATURE_CASE = """
case
  when feature in ('cvjd_extract') then 'cv_jd_analysis'
  when feature in ('interview_score') then 'feedback'
  when feature in ('realtime_session', 'realtime_response', 'next_question') then 'realtime_interview'
  when feature in ('coding_plan') then 'coding_interview'
  when feature in ('interview_blueprint') then 'action_plan'
  when feature like 'chat_%%' then 'chatbot'
  else null
end
"""

LANDING_FEATURE_CASE = """
case
  when event_name = 'landing_view' and coalesce(path, '/') like '/pricing%%' then 'pricing'
  when event_name = 'landing_view' then 'page_view'
  when event_name = 'pricing_click' then 'pricing'
  when event_name in ('signup_click', 'login_click') then 'page_view'
  else null
end
"""


def _admin_payload(admin: AdminUser) -> dict[str, Any]:
    return {
        "id": admin.internal_user_id,
        "external_auth_id": admin.user_id,
        "email": admin.email,
        "full_name": admin.full_name,
        "role_id": admin.role_id,
    }


def _date_window(date_from: date | None, date_to: date | None, days: int | None = None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if days and not date_from and not date_to:
        end_date = now.date()
        start_date = end_date - timedelta(days=days - 1)
    else:
        start_date = date_from or (now.date() - timedelta(days=29))
        end_date = date_to or now.date()

    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start_at, end_at


def _money(value: Any) -> str:
    return str(Decimal(str(value or 0)).quantize(Decimal("0.000001")))


def _ensure_activity_logs_schema() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                create table if not exists activity_logs (
                  id bigserial primary key,
                  actor_user_id uuid null references users(id) on delete set null,
                  target_user_id uuid null references users(id) on delete set null,
                  log_type varchar(20) not null check (log_type in ('auth', 'session', 'payment', 'account', 'system')),
                  action varchar(255) not null,
                  detail text null,
                  ip_address varchar(64) null,
                  meta_json jsonb null,
                  created_at timestamptz not null default now()
                )
                """
            )
            cur.execute("create index if not exists idx_activity_logs_type_created on activity_logs (log_type, created_at desc)")
            cur.execute("create index if not exists idx_activity_logs_actor_created on activity_logs (actor_user_id, created_at desc)")
        conn.commit()


@router.get("/me")
def get_current_admin(current_admin: AdminUser = Depends(require_admin)):
    return {"data": _admin_payload(current_admin)}


@router.get("/overview")
def get_admin_overview(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    group_by: LandingMetricGroupBy = Query(default="day"),
    current_admin: AdminUser = Depends(require_admin),
):
    try:
        ensure_tracking_tables()
        start_at, end_at = _date_window(date_from, date_to)
        landing = get_landing_metrics(date_from=start_at.date(), date_to=(end_at - timedelta(days=1)).date(), source=None, group_by=group_by)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    with latest_subscription as (
                      select distinct on (user_id) user_id, plan_id, status
                      from user_subscriptions
                      order by user_id, start_at desc, id desc
                    )
                    select
                      count(u.*)::int as total_users,
                      count(u.*) filter (where coalesce(p.code, 'free') = 'free')::int as free_users,
                      coalesce(sum(
                        case
                          when ls.status = 'active'
                          then coalesce(nullif(regexp_replace(p.monthly_price::text, '[^0-9.-]', '', 'g'), '')::numeric, 0)
                          else 0
                        end
                      ), 0) as revenue
                    from users u
                    left join latest_subscription ls on ls.user_id = u.id
                    left join plans p on p.id = ls.plan_id
                    """
                )
                user_totals = dict(cur.fetchone() or {})

                cur.execute(
                    """
                    select
                      count(*)::int as cv_jd_analyses
                    from cv_jd_analyses
                    where created_at >= %s and created_at < %s
                    """,
                    (start_at, end_at),
                )
                cv_totals = dict(cur.fetchone() or {})

                cur.execute(
                    """
                    select
                      count(*) filter (where status = 'completed')::int as completed_interviews
                    from interview_sessions
                    where created_at >= %s and created_at < %s
                    """,
                    (start_at, end_at),
                )
                session_totals = dict(cur.fetchone() or {})

                cur.execute(
                    """
                    select
                      coalesce(sum(cost_usd), 0) as total_ai_cost_usd,
                      count(*)::int as ai_calls
                    from api_usage_logs
                    where occurred_at >= %s and occurred_at < %s
                    """,
                    (start_at, end_at),
                )
                ai_totals = dict(cur.fetchone() or {})

                cur.execute(
                    """
                    select
                      date_trunc(%s, occurred_at)::date as period,
                      coalesce(sum(cost_usd), 0) as total_cost_usd
                    from api_usage_logs
                    where occurred_at >= %s and occurred_at < %s
                    group by period
                    order by period asc
                    """,
                    (group_by, start_at, end_at),
                )
                ai_cost_by_day = [
                    {"period": row["period"], "total_cost_usd": _money(row["total_cost_usd"])}
                    for row in cur.fetchall()
                ]

                cur.execute(
                    """
                    select
                      date_trunc('month', coalesce(ls.start_at, now()))::date as period,
                      coalesce(sum(coalesce(nullif(regexp_replace(p.monthly_price::text, '[^0-9.-]', '', 'g'), '')::numeric, 0)), 0) as revenue
                    from user_subscriptions ls
                    join plans p on p.id = ls.plan_id
                    where ls.status = 'active'
                      and ls.start_at >= %s
                      and ls.start_at < %s
                    group by period
                    order by period asc
                    """,
                    (start_at, end_at),
                )
                revenue_by_month = [
                    {"period": row["period"], "revenue": _money(row["revenue"])}
                    for row in cur.fetchall()
                ]

        days = max(1, (end_at.date() - start_at.date()).days)
        feature_metrics = get_feature_metrics(
            days=days,
            group_by=group_by,
            date_from=start_at.date(),
            date_to=(end_at - timedelta(days=1)).date(),
        )
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load admin overview.",
        ) from exc

    return {
        "data": {
            "current_admin": _admin_payload(current_admin),
            "window": {
                "date_from": start_at.date(),
                "date_to": (end_at - timedelta(days=1)).date(),
                "group_by": group_by,
            },
            "kpis": {
                "total_users": int(user_totals.get("total_users") or 0),
                "free_users": int(user_totals.get("free_users") or 0),
                "cv_jd_analyses": int(cv_totals.get("cv_jd_analyses") or 0),
                "completed_interviews": int(session_totals.get("completed_interviews") or 0),
                "total_ai_cost_usd": _money(ai_totals.get("total_ai_cost_usd")),
                "revenue": _money(user_totals.get("revenue")),
                "ai_calls": int(ai_totals.get("ai_calls") or 0),
            },
            "landing_metrics": landing,
            "function_metrics": feature_metrics["features"],
            "charts": {
                "landing_visits": landing["trend"],
                "ai_cost_by_day": ai_cost_by_day,
                "revenue_by_month": revenue_by_month,
            },
        }
    }


@router.get("/feature-metrics")
def get_feature_metrics(
    days: int = Query(30, ge=1, le=365),
    group_by: GroupBy = Query("day"),
    current_admin: AdminUser = Depends(require_admin),
):
    del current_admin
    try:
        ensure_tracking_tables()
        ensure_landing_events_schema()
        with get_connection() as conn:
            with conn.cursor() as cur:
                features = _empty_feature_map()
                _merge_feature_counts(features, _fetch_activity_feature_counts(cur, days))
                _merge_feature_counts(features, _fetch_landing_feature_counts(cur, days))
                _merge_ai_counts(features, _fetch_ai_feature_counts(cur, days))
                _merge_completion_counts(features, _fetch_completion_counts(cur, days))

                trend = _fetch_feature_trend(cur, days, group_by)
                top_pages = _fetch_top_pages(cur, days)
                dropoff = _fetch_interview_dropoff(cur, days)
                active_users = _fetch_feature_active_users(cur, days)
                window = _window_payload(days, group_by)
    except Exception as exc:
        logger.exception("Could not load admin feature metrics")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load feature metrics.",
        ) from exc

    feature_rows = [_feature_payload(key, values) for key, values in features.items()]
    top_feature = max(feature_rows, key=lambda item: item["events"], default=None)
    if top_feature and top_feature["events"] <= 0:
        top_feature = None

    cv_feature = features["cv_jd_analysis"]
    summary = {
        "total_events": sum(item["events"] for item in feature_rows),
        "active_users": active_users,
        "ai_calls": sum(item["ai_calls"] for item in feature_rows),
        "total_cost_usd": _money(sum(Decimal(str(item["total_cost_usd"])) for item in feature_rows)),
        "cv_uploads": features["upload_cv"]["events"],
        "resume_reviews": max(cv_feature["completions"], cv_feature["ai_calls"]),
        "completed_mock_interviews": dropoff["completed"],
        "abandoned_interviews": dropoff["abandoned"],
        "most_used_feature": top_feature,
    }

    return {
        "data": {
            "window": window,
            "summary": summary,
            "features": feature_rows,
            "trend": trend,
            "top_pages": top_pages,
            "dropoffs": {"realtime_interview": dropoff},
        }
    }


@router.get("/users")
def list_admin_users(
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: str | None = Query(default=None, max_length=120),
    status_filter: UserStatus | None = Query(default=None, alias="status"),
    plan: str | None = Query(default=None, max_length=50),
    current_admin: AdminUser = Depends(require_admin),
):
    del current_admin
    plan_filter = None if not plan or plan == "all" else plan.strip().lower()
    try:
        ensure_tracking_tables()
        with get_connection() as conn:
            with conn.cursor() as cur:
                where_sql, params = _user_filters(search, status_filter, plan_filter)
                cur.execute(
                    f"""
                    select
                      count(*)::int as total,
                      count(*) filter (where status = 'active')::int as active,
                      count(*) filter (where status = 'suspended')::int as suspended,
                      count(*) filter (where status = 'pending')::int as pending
                    from ({_user_base_query()}) u
                    {where_sql}
                    """,
                    params,
                )
                stats = dict(cur.fetchone() or {})

                cur.execute(
                    f"""
                    select count(*)::int as total
                    from ({_user_base_query()}) u
                    {where_sql}
                    """,
                    params,
                )
                total = int((cur.fetchone() or {}).get("total") or 0)

                cur.execute(
                    f"""
                    select *
                    from ({_user_base_query()}) u
                    {where_sql}
                    order by u.created_at desc, u.email asc
                    limit %s offset %s
                    """,
                    [*params, limit, offset],
                )
                items = [_user_payload(row) for row in cur.fetchall()]
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load users.",
        ) from exc

    return {
        "data": {
            "stats": {
                "total": int(stats.get("total") or 0),
                "active": int(stats.get("active") or 0),
                "suspended": int(stats.get("suspended") or 0),
                "pending": int(stats.get("pending") or 0),
            },
            "items": items,
            "pagination": {"limit": limit, "offset": offset, "total": total},
        }
    }


@router.get("/users/{user_id}/metrics")
def get_user_metrics(
    user_id: str,
    days: int = Query(30, ge=1, le=365),
    group_by: GroupBy = Query("day"),
    current_admin: AdminUser = Depends(require_admin),
):
    del current_admin
    internal_user_id = _uuid_or_404(user_id, "Invalid user id.")
    try:
        ensure_tracking_tables()
        with get_connection() as conn:
            with conn.cursor() as cur:
                user = _fetch_user_payload(cur, internal_user_id)
                if not user:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

                external_auth_id = user.get("external_auth_id")
                features = _empty_feature_map()
                _merge_feature_counts(features, _fetch_activity_feature_counts(cur, days, internal_user_id, external_auth_id))
                _merge_ai_counts(features, _fetch_ai_feature_counts(cur, days, internal_user_id, external_auth_id))
                _merge_completion_counts(features, _fetch_completion_counts(cur, days, internal_user_id, external_auth_id))

                trend = _fetch_feature_trend(cur, days, group_by, internal_user_id, external_auth_id)
                model_costs = _fetch_user_model_costs(cur, days, internal_user_id, external_auth_id)
                recent_events = _fetch_user_recent_events(cur, internal_user_id, external_auth_id)
                summary = _fetch_user_summary(cur, days, internal_user_id, external_auth_id, features)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load user metrics.",
        ) from exc

    return {
        "data": {
            "window": _window_payload(days, group_by),
            "user": user,
            "summary": summary,
            "features": [_feature_payload(key, values) for key, values in features.items()],
            "trend": trend,
            "model_costs": model_costs,
            "recent_events": recent_events,
        }
    }


@router.get("/sessions")
def list_admin_sessions(
    limit: int = Query(8, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: str | None = Query(default=None, max_length=120),
    status_filter: SessionStatus | None = Query(default=None, alias="status"),
    session_type: SessionType | None = Query(default=None, alias="type"),
    current_admin: AdminUser = Depends(require_admin),
):
    del current_admin
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                where_sql, params = _session_filters(search, status_filter, session_type)
                cur.execute(
                    f"""
                    select
                      count(*)::int as total,
                      count(*) filter (where s.status = 'completed')::int as completed,
                      count(*) filter (where s.status = 'in_progress')::int as in_progress,
                      count(*) filter (where s.status = 'scheduled')::int as scheduled,
                      coalesce(round(avg(s.overall_score) filter (where s.status = 'completed' and s.overall_score is not null)), 0)::int as avg_score
                    from interview_sessions s
                    join users u on u.id = s.user_id
                    {where_sql}
                    """,
                    params,
                )
                stats_row = cur.fetchone() or {}

                cur.execute(
                    f"""
                    select count(*)::int as total
                    from interview_sessions s
                    join users u on u.id = s.user_id
                    {where_sql}
                    """,
                    params,
                )
                total = int((cur.fetchone() or {}).get("total") or 0)

                cur.execute(
                    f"""
                    select
                      s.id,
                      s.position,
                      s.company,
                      s.session_type,
                      s.status,
                      s.scheduled_at,
                      s.started_at,
                      s.ended_at,
                      s.duration_seconds,
                      s.overall_score,
                      s.created_at,
                      u.id as user_id,
                      u.email,
                      u.full_name
                    from interview_sessions s
                    join users u on u.id = s.user_id
                    {where_sql}
                    order by coalesce(s.started_at, s.scheduled_at, s.created_at) desc
                    limit %s offset %s
                    """,
                    [*params, limit, offset],
                )
                items = [_session_payload(row) for row in cur.fetchall()]
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load interview sessions.",
        ) from exc

    return {
        "data": {
            "stats": {
                "total": int(stats_row.get("total") or 0),
                "completed": int(stats_row.get("completed") or 0),
                "inProgress": int(stats_row.get("in_progress") or 0),
                "scheduled": int(stats_row.get("scheduled") or 0),
                "avgScore": int(stats_row.get("avg_score") or 0),
            },
            "items": items,
            "pagination": {"limit": limit, "offset": offset, "total": total},
        }
    }


@router.get("/activity/logs")
def list_activity_logs(
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: str | None = Query(default=None, max_length=120),
    type_filter: ActivityType | None = Query(default=None, alias="type"),
    current_admin: AdminUser = Depends(require_admin),
):
    del current_admin
    try:
        ensure_tracking_tables()
        with get_connection() as conn:
            with conn.cursor() as cur:
                where_sql, params = _activity_filters(search, type_filter)
                base_query = _activity_base_query()
                cur.execute(
                    f"""
                    select
                      count(*)::int as total,
                      count(*) filter (where occurred_at::date = current_date)::int as today
                    from ({base_query}) a
                    {where_sql}
                    """,
                    params,
                )
                stats_row = cur.fetchone() or {}

                cur.execute(
                    f"""
                    select type, count(*)::int as total
                    from ({base_query}) a
                    {where_sql}
                    group by type
                    """,
                    params,
                )
                type_counts = {item: 0 for item in ACTIVITY_TYPES}
                for row in cur.fetchall():
                    row_type = row.get("type")
                    if row_type in type_counts:
                        type_counts[row_type] = int(row.get("total") or 0)

                cur.execute(
                    f"""
                    select count(*)::int as total
                    from ({base_query}) a
                    {where_sql}
                    """,
                    params,
                )
                total = int((cur.fetchone() or {}).get("total") or 0)

                cur.execute(
                    f"""
                    select *
                    from ({base_query}) a
                    {where_sql}
                    order by occurred_at desc
                    limit %s offset %s
                    """,
                    [*params, limit, offset],
                )
                items = [_activity_payload(row) for row in cur.fetchall()]
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load activity logs.",
        ) from exc

    return {
        "data": {
            "stats": {
                "total": int(stats_row.get("total") or 0),
                "today": int(stats_row.get("today") or 0),
            },
            "type_counts": type_counts,
            "items": items,
            "pagination": {"limit": limit, "offset": offset, "total": total},
        }
    }


@router.get("/activity/logs.csv")
def export_activity_logs_csv(
    search: str | None = Query(default=None, max_length=120),
    type_filter: ActivityType | None = Query(default=None, alias="type"),
    current_admin: AdminUser = Depends(require_admin),
):
    del current_admin
    try:
        ensure_tracking_tables()
        with get_connection() as conn:
            with conn.cursor() as cur:
                where_sql, params = _activity_filters(search, type_filter)
                cur.execute(
                    f"""
                    select *
                    from ({_activity_base_query()}) a
                    {where_sql}
                    order by occurred_at desc
                    limit 5000
                    """,
                    params,
                )
                rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not export activity logs.",
        ) from exc

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "timestamp", "type", "user", "email", "action", "detail", "success", "duration_ms"])
    for row in rows:
        writer.writerow(
            [
                row.get("id"),
                _iso(row.get("occurred_at")),
                row.get("type"),
                row.get("user_name"),
                row.get("email"),
                row.get("action"),
                row.get("detail"),
                row.get("success"),
                row.get("duration_ms"),
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=viecareer-activity-logs.csv"},
    )


@router.get("/usage/summary")
def get_usage_summary(
    days: int = Query(28, ge=1, le=90),
    current_admin: AdminUser = Depends(require_admin),
):
    del current_admin
    try:
        ensure_tracking_tables()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select
                      count(*) as call_count,
                      coalesce(sum(total_tokens), 0) as total_tokens,
                      coalesce(sum(cost_usd), 0) as total_cost_usd,
                      count(*) filter (where status = 'error') as error_count
                    from api_usage_logs
                    where occurred_at >= now() - (%s * interval '1 day')
                    """,
                    (days,),
                )
                totals = cur.fetchone()
                cur.execute(
                    """
                    select
                      l.occurred_at::date as usage_date,
                      l.feature,
                      l.model,
                      l.user_id,
                      l.external_auth_id,
                      u.email,
                      u.full_name,
                      count(*) as call_count,
                      coalesce(sum(l.prompt_tokens), 0) as prompt_tokens,
                      coalesce(sum(l.completion_tokens), 0) as completion_tokens,
                      coalesce(sum(l.total_tokens), 0) as total_tokens,
                      coalesce(sum(l.cost_usd), 0) as total_cost_usd,
                      count(*) filter (where l.status = 'error') as error_count
                    from api_usage_logs l
                    left join users u on u.id = l.user_id
                    where l.occurred_at >= now() - (%s * interval '1 day')
                    group by
                      l.occurred_at::date,
                      l.feature,
                      l.model,
                      l.user_id,
                      l.external_auth_id,
                      u.email,
                      u.full_name
                    order by usage_date desc, l.feature, l.model, u.email nulls last
                    """,
                    (days,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load usage summary.",
        ) from exc

    return {"data": {"days": days, "totals": totals, "items": rows}}


@router.get("/feature-metrics")
@limiter.limit("30/minute")
def get_admin_feature_metrics(
    request: Request,
    days: int = Query(30, ge=1, le=180),
    group_by: FeatureGroupBy = Query(default="day"),
    current_admin: AdminUser = Depends(require_admin),
):
    try:
        return {"data": get_feature_metrics(days=days, group_by=group_by)}
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load feature metrics.",
        ) from exc


@router.get("/users")
@limiter.limit("30/minute")
def get_admin_users(
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: str | None = Query(default=None, max_length=120),
    status_filter: str | None = Query(default=None, alias="status"),
    plan_filter: str | None = Query(default=None, alias="plan"),
    current_admin: AdminUser = Depends(require_admin),
):
    try:
        return {
            "data": list_admin_users(
                limit=limit,
                offset=offset,
                search=search.strip() if search else None,
                status_filter=status_filter,
                plan_filter=plan_filter,
            )
        }
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load users.",
        ) from exc


@router.get("/users/{user_id}/metrics")
@limiter.limit("30/minute")
def get_admin_user_metric_detail(
    request: Request,
    user_id: str,
    days: int = Query(30, ge=1, le=180),
    group_by: UserMetricGroupBy = Query(default="day"),
    current_admin: AdminUser = Depends(require_admin),
):
    try:
        return {"data": get_admin_user_metrics(user_id=user_id, days=days, group_by=group_by)}
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load user metrics.",
        ) from exc


@router.get("/activity/recent")
def get_recent_activity(
    limit: int = Query(100, ge=1, le=500),
    current_admin: AdminUser = Depends(require_admin),
):
    del current_admin
    try:
        ensure_tracking_tables()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select
                      e.id,
                      e.occurred_at,
                      e.event_name,
                      e.object_type,
                      e.object_id,
                      e.success,
                      e.duration_ms,
                      e.meta_json,
                      e.external_auth_id,
                      u.email,
                      u.full_name
                    from user_activity_events e
                    left join users u on u.id = e.user_id
                    order by e.occurred_at desc
                    limit %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load recent activity.",
        ) from exc

    return {"data": rows}


@router.get("/activity-logs")
@limiter.limit("30/minute")
def get_admin_activity_logs(
    request: Request,
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    type_filter: str | None = Query(default=None, alias="type"),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_admin: AdminUser = Depends(require_admin),
):
    try:
        _ensure_activity_logs_schema()
        start_at, end_at = _date_window(date_from, date_to)
        where_parts = ["l.created_at >= %s", "l.created_at < %s"]
        params: list[Any] = [start_at, end_at]

        if type_filter and type_filter != "all":
            where_parts.append("l.log_type = %s")
            params.append(type_filter.lower())

        if search:
            term = f"%{search.strip()}%"
            where_parts.append(
                """
                (
                  l.action ilike %s
                  or coalesce(l.detail, '') ilike %s
                  or coalesce(actor.email, '') ilike %s
                  or coalesce(actor.full_name, '') ilike %s
                  or coalesce(target.email, '') ilike %s
                  or coalesce(target.full_name, '') ilike %s
                )
                """
            )
            params.extend([term, term, term, term, term, term])

        where_sql = " and ".join(where_parts)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select
                      count(*)::int as total,
                      count(*) filter (where l.created_at::date = current_date)::int as today,
                      count(*) filter (where l.log_type = 'auth')::int as auth,
                      count(*) filter (where l.log_type = 'session')::int as session,
                      count(*) filter (where l.log_type = 'payment')::int as payment,
                      count(*) filter (where l.log_type = 'account')::int as account,
                      count(*) filter (where l.log_type = 'system')::int as system
                    from activity_logs l
                    left join users actor on actor.id = l.actor_user_id
                    left join users target on target.id = l.target_user_id
                    where {where_sql}
                    """,
                    params,
                )
                stats = dict(cur.fetchone() or {})

                cur.execute(
                    f"""
                    select
                      l.id,
                      l.created_at,
                      l.log_type,
                      l.action,
                      l.detail,
                      l.ip_address,
                      l.meta_json,
                      actor.id as actor_user_id,
                      actor.email as actor_email,
                      actor.full_name as actor_full_name,
                      target.id as target_user_id,
                      target.email as target_email,
                      target.full_name as target_full_name
                    from activity_logs l
                    left join users actor on actor.id = l.actor_user_id
                    left join users target on target.id = l.target_user_id
                    where {where_sql}
                    order by l.created_at desc, l.id desc
                    limit %s offset %s
                    """,
                    [*params, limit, offset],
                )
                items = [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load activity logs.",
        ) from exc

    return {
        "data": {
            "window": {"date_from": start_at.date(), "date_to": (end_at - timedelta(days=1)).date()},
            "stats": stats,
            "items": items,
            "pagination": {"limit": limit, "offset": offset, "total": int(stats.get("total") or 0)},
        }
    }


@router.get("/activity-logs/export.csv")
def export_admin_activity_logs(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    type_filter: str | None = Query(default=None, alias="type"),
    search: str | None = Query(default=None, max_length=120),
    current_admin: AdminUser = Depends(require_admin),
):
    try:
        _ensure_activity_logs_schema()
        start_at, end_at = _date_window(date_from, date_to)
        where_parts = ["l.created_at >= %s", "l.created_at < %s"]
        params: list[Any] = [start_at, end_at]
        if type_filter and type_filter != "all":
            where_parts.append("l.log_type = %s")
            params.append(type_filter.lower())
        if search:
            term = f"%{search.strip()}%"
            where_parts.append(
                """
                (
                  l.action ilike %s
                  or coalesce(l.detail, '') ilike %s
                  or coalesce(actor.email, '') ilike %s
                  or coalesce(actor.full_name, '') ilike %s
                )
                """
            )
            params.extend([term, term, term, term])
        where_sql = " and ".join(where_parts)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select
                      l.id,
                      l.created_at,
                      l.log_type,
                      l.action,
                      l.detail,
                      l.ip_address,
                      actor.email as actor_email,
                      actor.full_name as actor_full_name,
                      target.email as target_email,
                      target.full_name as target_full_name
                    from activity_logs l
                    left join users actor on actor.id = l.actor_user_id
                    left join users target on target.id = l.target_user_id
                    where {where_sql}
                    order by l.created_at desc, l.id desc
                    limit 5000
                    """,
                    params,
                )
                rows = [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not export activity logs.",
        ) from exc

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "id",
            "created_at",
            "log_type",
            "action",
            "detail",
            "ip_address",
            "actor_email",
            "actor_full_name",
            "target_email",
            "target_full_name",
        ],
    )
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="admin-activity-logs.csv"'},
    )


@router.get("/ai-costs/summary")
@limiter.limit("30/minute")
def get_admin_ai_cost_summary(
    request: Request,
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    feature: str | None = Query(default=None, max_length=80),
    model: str | None = Query(default=None, max_length=120),
    status_filter: str | None = Query(default=None, alias="status"),
    current_admin: AdminUser = Depends(require_admin),
):
    try:
        ensure_tracking_tables()
        _ensure_activity_logs_schema()
        start_at, end_at = _date_window(date_from, date_to)
        where_parts = ["occurred_at >= %s", "occurred_at < %s"]
        params: list[Any] = [start_at, end_at]
        if feature and feature != "all":
            where_parts.append("feature = %s")
            params.append(feature)
        if model and model != "all":
            where_parts.append("model = %s")
            params.append(model)
        if status_filter and status_filter != "all":
            where_parts.append("status = %s")
            params.append(status_filter)
        where_sql = " and ".join(where_parts)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select
                      count(*)::int as ai_calls,
                      coalesce(sum(cost_usd), 0) as total_cost_usd,
                      coalesce(sum(prompt_tokens), 0)::int as prompt_tokens,
                      coalesce(sum(completion_tokens), 0)::int as completion_tokens,
                      coalesce(sum(total_tokens), 0)::int as total_tokens,
                      coalesce(sum(input_audio_seconds), 0)::int as input_audio_seconds,
                      coalesce(sum(output_audio_seconds), 0)::int as output_audio_seconds,
                      count(*) filter (where status = 'error')::int as error_count
                    from api_usage_logs
                    where {where_sql}
                    """,
                    params,
                )
                summary = dict(cur.fetchone() or {})

                cur.execute(
                    f"""
                    select
                      occurred_at::date as period,
                      count(*)::int as ai_calls,
                      coalesce(sum(cost_usd), 0) as total_cost_usd,
                      coalesce(sum(total_tokens), 0)::int as total_tokens,
                      coalesce(sum(input_audio_seconds), 0)::int as input_audio_seconds,
                      coalesce(sum(output_audio_seconds), 0)::int as output_audio_seconds,
                      count(*) filter (where status = 'error')::int as error_count
                    from api_usage_logs
                    where {where_sql}
                    group by period
                    order by period asc
                    """,
                    params,
                )
                daily_costs = [
                    {**dict(row), "total_cost_usd": _money(row["total_cost_usd"])}
                    for row in cur.fetchall()
                ]

                cur.execute(
                    f"""
                    select
                      feature,
                      count(*)::int as ai_calls,
                      coalesce(sum(total_tokens), 0)::int as total_tokens,
                      coalesce(sum(cost_usd), 0) as total_cost_usd,
                      count(*) filter (where status = 'error')::int as error_count
                    from api_usage_logs
                    where {where_sql}
                    group by feature
                    order by total_cost_usd desc, ai_calls desc, feature asc
                    """
                    ,
                    params,
                )
                feature_breakdown = [
                    {**dict(row), "total_cost_usd": _money(row["total_cost_usd"])}
                    for row in cur.fetchall()
                ]

                cur.execute(
                    f"""
                    select
                      model,
                      count(*)::int as ai_calls,
                      coalesce(sum(prompt_tokens), 0)::int as prompt_tokens,
                      coalesce(sum(completion_tokens), 0)::int as completion_tokens,
                      coalesce(sum(total_tokens), 0)::int as total_tokens,
                      coalesce(sum(cost_usd), 0) as total_cost_usd,
                      count(*) filter (where status = 'error')::int as error_count
                    from api_usage_logs
                    where {where_sql}
                    group by model
                    order by total_cost_usd desc, ai_calls desc, model asc
                    """,
                    params,
                )
                model_breakdown = [
                    {**dict(row), "total_cost_usd": _money(row["total_cost_usd"])}
                    for row in cur.fetchall()
                ]

                cur.execute(
                    """
                    select count(*)::int as blocked_spam_count
                    from activity_logs
                    where created_at >= %s and created_at < %s
                      and log_type = 'system'
                      and (
                        action ilike '%%spam%%'
                        or action ilike '%%rate_limit%%'
                        or action ilike '%%quota%%'
                        or coalesce(detail, '') ilike '%%spam%%'
                        or coalesce(detail, '') ilike '%%rate limit%%'
                        or coalesce(detail, '') ilike '%%quota%%'
                      )
                    """,
                    (start_at, end_at),
                )
                blocked = dict(cur.fetchone() or {})
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load AI cost summary.",
        ) from exc

    return {
        "data": {
            "window": {"date_from": start_at.date(), "date_to": (end_at - timedelta(days=1)).date()},
            "summary": {
                "ai_calls": int(summary.get("ai_calls") or 0),
                "total_cost_usd": _money(summary.get("total_cost_usd")),
                "prompt_tokens": int(summary.get("prompt_tokens") or 0),
                "completion_tokens": int(summary.get("completion_tokens") or 0),
                "total_tokens": int(summary.get("total_tokens") or 0),
                "input_audio_seconds": int(summary.get("input_audio_seconds") or 0),
                "output_audio_seconds": int(summary.get("output_audio_seconds") or 0),
                "error_count": int(summary.get("error_count") or 0),
                "blocked_spam_count": int(blocked.get("blocked_spam_count") or 0),
            },
            "daily_costs": daily_costs,
            "feature_breakdown": feature_breakdown,
            "model_breakdown": model_breakdown,
        }
    }


@router.get("/ai-costs/events")
@limiter.limit("30/minute")
def get_admin_ai_cost_events(
    request: Request,
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    feature: str | None = Query(default=None, max_length=80),
    model: str | None = Query(default=None, max_length=120),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_admin: AdminUser = Depends(require_admin),
):
    try:
        ensure_tracking_tables()
        start_at, end_at = _date_window(date_from, date_to)
        where_parts = ["l.occurred_at >= %s", "l.occurred_at < %s"]
        params: list[Any] = [start_at, end_at]
        if feature and feature != "all":
            where_parts.append("l.feature = %s")
            params.append(feature)
        if model and model != "all":
            where_parts.append("l.model = %s")
            params.append(model)
        if status_filter and status_filter != "all":
            where_parts.append("l.status = %s")
            params.append(status_filter)
        if search:
            term = f"%{search.strip()}%"
            where_parts.append(
                """
                (
                  coalesce(u.email, '') ilike %s
                  or coalesce(u.full_name, '') ilike %s
                  or coalesce(l.external_auth_id, '') ilike %s
                  or l.user_id::text ilike %s
                )
                """
            )
            params.extend([term, term, term, term])
        where_sql = " and ".join(where_parts)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select count(*)::int as total
                    from api_usage_logs l
                    left join users u on u.id = l.user_id
                    where {where_sql}
                    """,
                    params,
                )
                total = int((cur.fetchone() or {}).get("total") or 0)

                cur.execute(
                    f"""
                    select
                      l.id,
                      l.occurred_at,
                      l.user_id,
                      l.external_auth_id,
                      u.email,
                      u.full_name,
                      l.provider,
                      l.model,
                      l.feature,
                      l.related_type,
                      l.related_id,
                      l.status,
                      l.prompt_tokens,
                      l.completion_tokens,
                      l.total_tokens,
                      l.input_audio_seconds,
                      l.output_audio_seconds,
                      l.cost_usd,
                      l.duration_ms,
                      l.error_code
                    from api_usage_logs l
                    left join users u on u.id = l.user_id
                    where {where_sql}
                    order by l.occurred_at desc, l.id desc
                    limit %s offset %s
                    """,
                    [*params, limit, offset],
                )
                items = []
                for row in cur.fetchall():
                    item = dict(row)
                    item["cost_usd"] = _money(item.get("cost_usd"))
                    items.append(item)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load AI cost events.",
        ) from exc

    return {
        "data": {
            "window": {"date_from": start_at.date(), "date_to": (end_at - timedelta(days=1)).date()},
            "items": items,
            "pagination": {"limit": limit, "offset": offset, "total": total},
        }
    }


@router.get("/sessions")
@limiter.limit("30/minute")
def get_admin_sessions(
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: str | None = Query(default=None, max_length=120),
    status_filter: str | None = Query(default=None, alias="status"),
    session_type: str | None = Query(default=None, alias="type"),
    current_admin: AdminUser = Depends(require_admin),
):
    try:
        where_parts = ["true"]
        params: list[Any] = []
        if search:
            term = f"%{search.strip()}%"
            where_parts.append(
                """
                (
                  coalesce(u.email, '') ilike %s
                  or coalesce(u.full_name, '') ilike %s
                  or coalesce(s.position, '') ilike %s
                  or coalesce(s.company, '') ilike %s
                )
                """
            )
            params.extend([term, term, term, term])
        if status_filter and status_filter != "all":
            where_parts.append("s.status = %s")
            params.append(status_filter)
        if session_type and session_type != "all":
            where_parts.append("s.session_type = %s")
            params.append(session_type)
        where_sql = " and ".join(where_parts)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select
                      count(*)::int as total,
                      count(*) filter (where s.status = 'completed')::int as completed,
                      count(*) filter (where s.status = 'in_progress')::int as in_progress,
                      count(*) filter (where s.status = 'scheduled')::int as scheduled,
                      coalesce(round(avg(s.overall_score) filter (where s.overall_score is not null))::int, 0) as avg_score
                    from interview_sessions s
                    left join users u on u.id = s.user_id
                    where {where_sql}
                    """,
                    params,
                )
                stats = dict(cur.fetchone() or {})

                cur.execute(
                    f"""
                    select
                      s.id,
                      s.user_id,
                      u.email,
                      u.full_name,
                      s.position,
                      s.company,
                      s.session_type,
                      s.status,
                      s.started_at,
                      s.ended_at,
                      s.duration_seconds,
                      s.overall_score,
                      s.created_at
                    from interview_sessions s
                    left join users u on u.id = s.user_id
                    where {where_sql}
                    order by coalesce(s.started_at, s.created_at) desc
                    limit %s offset %s
                    """,
                    [*params, limit, offset],
                )
                items = [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load sessions.",
        ) from exc

    return {"data": {"stats": stats, "items": items, "pagination": {"limit": limit, "offset": offset, "total": int(stats.get("total") or 0)}}}


@router.get("/landing-metrics")
@limiter.limit("30/minute")
def landing_metrics(
    request: Request,
    current_admin: AdminUser = Depends(require_admin),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    source: LandingSource | None = Query(default=None),
    group_by: LandingMetricGroupBy = Query(default="day"),
):
    del request, current_admin
    return {
        "data": get_landing_metrics(
            date_from=date_from,
            date_to=date_to,
            source=source,
            group_by=group_by,
        )
    }


def _empty_feature_map() -> dict[str, dict[str, Any]]:
    return {
        key: {
            "events": 0,
            "active_users": set(),
            "ai_calls": 0,
            "completions": 0,
            "total_tokens": 0,
            "total_cost_usd": Decimal("0"),
        }
        for key in FEATURE_KEYS
    }


def _merge_feature_counts(features: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    for row in rows:
        key = row.get("feature_key")
        if key not in features:
            continue
        features[key]["events"] += int(row.get("events") or 0)
        features[key]["active_users"].update(row.get("active_users") or [])


def _merge_ai_counts(features: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    for row in rows:
        key = row.get("feature_key")
        if key not in features:
            continue
        ai_calls = int(row.get("ai_calls") or 0)
        features[key]["events"] += ai_calls
        features[key]["ai_calls"] += ai_calls
        features[key]["total_tokens"] += int(row.get("total_tokens") or 0)
        features[key]["total_cost_usd"] += Decimal(str(row.get("total_cost_usd") or 0))
        features[key]["active_users"].update(row.get("active_users") or [])


def _merge_completion_counts(features: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    for row in rows:
        key = row.get("feature_key")
        if key in features:
            features[key]["completions"] += int(row.get("completions") or 0)


def _feature_payload(key: str, values: dict[str, Any]) -> dict[str, Any]:
    definition = FEATURE_DEFINITIONS[key]
    return {
        "feature_key": key,
        "label": definition["label"],
        "description": definition["description"],
        "kind": definition["kind"],
        "events": int(values["events"]),
        "active_users": len(values["active_users"]),
        "ai_calls": int(values["ai_calls"]),
        "completions": int(values["completions"]),
        "total_tokens": int(values["total_tokens"]),
        "total_cost_usd": _money(values["total_cost_usd"]),
    }


def _fetch_activity_feature_counts(
    cur,
    days: int,
    user_id: str | None = None,
    external_auth_id: str | None = None,
) -> list[dict[str, Any]]:
    where, params = _tracked_user_filter("occurred_at", days, user_id, external_auth_id)
    cur.execute(
        f"""
        select
          feature_key,
          count(*)::int as events,
          array_remove(array_agg(distinct actor), null) as active_users
        from (
          select
            {ACTIVITY_FEATURE_CASE} as feature_key,
            coalesce(user_id::text, external_auth_id) as actor
          from user_activity_events
          where {where}
        ) raw
        where feature_key is not null
        group by feature_key
        """,
        params,
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_landing_feature_counts(cur, days: int) -> list[dict[str, Any]]:
    cur.execute(
        f"""
        select
          feature_key,
          count(*)::int as events,
          array_remove(array_agg(distinct actor), null) as active_users
        from (
          select
            {LANDING_FEATURE_CASE} as feature_key,
            coalesce(user_id::text, visitor_id) as actor
          from landing_events
          where created_at >= now() - (%s * interval '1 day')
        ) raw
        where feature_key is not null
        group by feature_key
        """,
        (days,),
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_ai_feature_counts(
    cur,
    days: int,
    user_id: str | None = None,
    external_auth_id: str | None = None,
) -> list[dict[str, Any]]:
    where, params = _tracked_user_filter("occurred_at", days, user_id, external_auth_id)
    cur.execute(
        f"""
        select
          feature_key,
          count(*)::int as ai_calls,
          coalesce(sum(total_tokens), 0)::int as total_tokens,
          coalesce(sum(cost_usd), 0) as total_cost_usd,
          array_remove(array_agg(distinct actor), null) as active_users
        from (
          select
            {API_FEATURE_CASE} as feature_key,
            total_tokens,
            cost_usd,
            coalesce(user_id::text, external_auth_id) as actor
          from api_usage_logs
          where {where}
        ) raw
        where feature_key is not null
        group by feature_key
        """,
        params,
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_completion_counts(
    cur,
    days: int,
    user_id: str | None = None,
    external_auth_id: str | None = None,
) -> list[dict[str, Any]]:
    where, params = _tracked_user_filter("occurred_at", days, user_id, external_auth_id)
    cur.execute(
        f"""
        select feature_key, count(*)::int as completions
        from (
          select {ACTIVITY_COMPLETION_CASE} as feature_key
          from user_activity_events
          where {where}
        ) raw
        where feature_key is not null
        group by feature_key
        """,
        params,
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_feature_trend(
    cur,
    days: int,
    group_by: GroupBy,
    user_id: str | None = None,
    external_auth_id: str | None = None,
) -> list[dict[str, Any]]:
    activity_where, activity_params = _tracked_user_filter("occurred_at", days, user_id, external_auth_id)
    api_where, api_params = _tracked_user_filter("occurred_at", days, user_id, external_auth_id)

    params: list[Any] = [group_by, *activity_params, group_by, *api_params]
    landing_sql = ""
    if user_id is None:
        landing_sql = f"""
          union all
          select period, feature_key, count(*)::int as events
          from (
            select
              date_trunc(%s, created_at)::date as period,
              {LANDING_FEATURE_CASE} as feature_key
            from landing_events
            where created_at >= now() - (%s * interval '1 day')
          ) landing_raw
          where feature_key is not null
          group by period, feature_key
        """
        params.extend([group_by, days])

    cur.execute(
        f"""
        select period, feature_key, sum(events)::int as events
        from (
          select period, feature_key, count(*)::int as events
          from (
            select
              date_trunc(%s, occurred_at)::date as period,
              {ACTIVITY_FEATURE_CASE} as feature_key
            from user_activity_events
            where {activity_where}
          ) activity_raw
          where feature_key is not null
          group by period, feature_key

          union all

          select period, feature_key, count(*)::int as events
          from (
            select
              date_trunc(%s, occurred_at)::date as period,
              {API_FEATURE_CASE} as feature_key
            from api_usage_logs
            where {api_where}
          ) api_raw
          where feature_key is not null
          group by period, feature_key

          {landing_sql}
        ) combined
        group by period, feature_key
        order by period asc
        """,
        params,
    )
    by_period: dict[str, dict[str, Any]] = {}
    for row in cur.fetchall():
        period = _date_key(row["period"])
        by_period.setdefault(period, {"period": period, **{key: 0 for key in FEATURE_KEYS}})
        by_period[period][row["feature_key"]] = int(row.get("events") or 0)
    return list(by_period.values())


def _fetch_top_pages(cur, days: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        select
          coalesce(nullif(path, ''), '/') as path,
          count(*)::int as views,
          count(distinct visitor_id)::int as unique_visitors
        from landing_events
        where event_name = 'landing_view'
          and created_at >= now() - (%s * interval '1 day')
        group by coalesce(nullif(path, ''), '/')
        order by views desc, path asc
        limit 8
        """,
        (days,),
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_interview_dropoff(cur, days: int) -> dict[str, Any]:
    cur.execute(
        """
        select
          count(*)::int as started,
          count(distinct user_id)::int as started_users,
          count(*) filter (where status = 'completed')::int as completed,
          count(*) filter (where status <> 'completed')::int as abandoned,
          count(distinct user_id) filter (where status <> 'completed')::int as abandoned_users
        from interview_sessions
        where coalesce(started_at, created_at) >= now() - (%s * interval '1 day')
        """,
        (days,),
    )
    row = cur.fetchone() or {}
    started = int(row.get("started") or 0)
    completed = int(row.get("completed") or 0)
    abandoned = int(row.get("abandoned") or 0)
    return {
        "started": started,
        "started_users": int(row.get("started_users") or 0),
        "completed": completed,
        "abandoned": abandoned,
        "abandoned_users": int(row.get("abandoned_users") or 0),
        "abandonment_rate": round((abandoned / started) * 100) if started else 0,
        "label": "Mock interviews",
    }


def _fetch_feature_active_users(cur, days: int) -> int:
    cur.execute(
        """
        select count(distinct actor)::int as active_users
        from (
          select coalesce(user_id::text, external_auth_id) as actor
          from user_activity_events
          where occurred_at >= now() - (%s * interval '1 day')
          union all
          select coalesce(user_id::text, external_auth_id) as actor
          from api_usage_logs
          where occurred_at >= now() - (%s * interval '1 day')
          union all
          select coalesce(user_id::text, visitor_id) as actor
          from landing_events
          where created_at >= now() - (%s * interval '1 day')
        ) actors
        where actor is not null
        """,
        (days, days, days),
    )
    return int((cur.fetchone() or {}).get("active_users") or 0)


def _fetch_user_summary(
    cur,
    days: int,
    user_id: str,
    external_auth_id: str | None,
    features: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    where, params = _tracked_user_filter("occurred_at", days, user_id, external_auth_id)
    cur.execute(
        f"""
        select
          count(*)::int as ai_calls,
          coalesce(sum(total_tokens), 0)::int as total_tokens,
          coalesce(sum(cost_usd), 0) as total_cost_usd,
          count(distinct model)::int as models_used
        from api_usage_logs
        where {where}
        """,
        params,
    )
    usage = cur.fetchone() or {}
    cur.execute(
        f"""
        select max(occurred_at) as last_event_at
        from user_activity_events
        where {where}
        """,
        params,
    )
    last_event = cur.fetchone() or {}
    feature_rows = [_feature_payload(key, values) for key, values in features.items()]
    top_feature = max(feature_rows, key=lambda item: item["events"], default=None)
    if top_feature and top_feature["events"] <= 0:
        top_feature = None
    return {
        "total_events": sum(item["events"] for item in feature_rows),
        "ai_calls": int(usage.get("ai_calls") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "total_cost_usd": _money(usage.get("total_cost_usd") or 0),
        "features_used": sum(1 for item in feature_rows if item["events"] > 0 or item["ai_calls"] > 0),
        "models_used": int(usage.get("models_used") or 0),
        "last_event_at": _iso(last_event.get("last_event_at")),
        "top_feature": top_feature,
    }


def _fetch_user_model_costs(cur, days: int, user_id: str, external_auth_id: str | None) -> list[dict[str, Any]]:
    where, params = _tracked_user_filter("occurred_at", days, user_id, external_auth_id)
    cur.execute(
        f"""
        select
          model,
          count(*)::int as ai_calls,
          coalesce(sum(total_tokens), 0)::int as total_tokens,
          coalesce(sum(cost_usd), 0) as total_cost_usd,
          max(occurred_at) as last_used_at
        from api_usage_logs
        where {where}
        group by model
        order by total_cost_usd desc, ai_calls desc, model asc
        limit 8
        """,
        params,
    )
    return [
        {
            "model": row["model"],
            "ai_calls": int(row.get("ai_calls") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
            "total_cost_usd": _money(row.get("total_cost_usd") or 0),
            "last_used_at": _iso(row.get("last_used_at")),
        }
        for row in cur.fetchall()
    ]


def _fetch_user_recent_events(cur, user_id: str, external_auth_id: str | None) -> list[dict[str, Any]]:
    clauses = ["(user_id = %s"]
    params: list[Any] = [user_id]
    if external_auth_id:
        clauses.append("or external_auth_id = %s")
        params.append(external_auth_id)
    clauses.append(")")
    cur.execute(
        f"""
        select occurred_at, event_name, object_type, success, duration_ms
        from user_activity_events
        where {' '.join(clauses)}
        order by occurred_at desc
        limit 20
        """,
        params,
    )
    return [
        {
            "occurred_at": _iso(row.get("occurred_at")),
            "event_name": row.get("event_name"),
            "object_type": row.get("object_type"),
            "success": bool(row.get("success")),
            "duration_ms": row.get("duration_ms"),
        }
        for row in cur.fetchall()
    ]


def _user_base_query() -> str:
    return """
      select
        u.id,
        u.external_auth_id,
        u.email,
        u.full_name,
        u.status,
        u.avatar_url,
        u.created_at,
        u.last_active_at,
        r.code as role_code,
        up.birth_year,
        up.university,
        up.target_position,
        coalesce(plan_row.code, free_plan.code, 'free') as plan_code,
        coalesce(plan_row.name, free_plan.name, 'Free') as plan_name,
        coalesce(plan_row.quota_total, free_plan.quota_total, 0) as plan_quota_total,
        coalesce(plan_row.rate_limit_per_minute, free_plan.rate_limit_per_minute, 0) as plan_rate_limit_per_minute,
        coalesce(session_row.total_sessions, 0) as total_sessions,
        coalesce(usage_row.ai_calls, 0) as ai_calls,
        coalesce(usage_row.total_tokens, 0) as total_tokens,
        coalesce(usage_row.total_cost_usd, 0) as total_cost_usd,
        coalesce(activity_row.activity_events, 0) as activity_events
      from users u
      left join roles r on r.id = u.role_id
      left join user_profiles up on up.user_id = u.id
      left join lateral (
        select p.code, p.name, p.quota_total, p.rate_limit_per_minute
        from user_subscriptions us
        join plans p on p.id = us.plan_id
        where us.user_id = u.id
          and us.status = 'active'
          and (us.end_at is null or us.end_at >= now())
        order by us.start_at desc
        limit 1
      ) plan_row on true
      left join plans free_plan on free_plan.code = 'free'
      left join lateral (
        select count(*)::int as total_sessions
        from interview_sessions s
        where s.user_id = u.id
      ) session_row on true
      left join lateral (
        select
          count(*)::int as ai_calls,
          coalesce(sum(total_tokens), 0)::int as total_tokens,
          coalesce(sum(cost_usd), 0) as total_cost_usd
        from api_usage_logs a
        where a.user_id = u.id or (u.external_auth_id is not null and a.external_auth_id = u.external_auth_id)
      ) usage_row on true
      left join lateral (
        select count(*)::int as activity_events
        from user_activity_events e
        where e.user_id = u.id or (u.external_auth_id is not null and e.external_auth_id = u.external_auth_id)
      ) activity_row on true
    """


def _user_filters(
    search: str | None,
    status_filter: str | None,
    plan_filter: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if search:
        pattern = f"%{search.strip().lower()}%"
        clauses.append(
            """
            (
              lower(u.email) like %s
              or lower(u.full_name) like %s
              or lower(coalesce(u.external_auth_id, '')) like %s
              or u.id::text like %s
            )
            """
        )
        params.extend([pattern, pattern, pattern, pattern])
    if status_filter:
        clauses.append("u.status = %s")
        params.append(status_filter)
    if plan_filter:
        clauses.append("u.plan_code = %s")
        params.append(plan_filter)
    return ("where " + " and ".join(clauses), params) if clauses else ("", params)


def _fetch_user_payload(cur, user_id: str) -> dict[str, Any] | None:
    cur.execute(
        f"""
        select *
        from ({_user_base_query()}) u
        where u.id = %s
        """,
        (user_id,),
    )
    row = cur.fetchone()
    return _user_payload(row) if row else None


def _user_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "external_auth_id": row.get("external_auth_id"),
        "email": row.get("email") or "",
        "full_name": row.get("full_name") or "",
        "role_code": row.get("role_code"),
        "status": row.get("status") or "pending",
        "avatar_url": row.get("avatar_url"),
        "created_at": _iso(row.get("created_at")),
        "last_active_at": _iso(row.get("last_active_at")),
        "profile": {
            "birth_year": row.get("birth_year"),
            "university": row.get("university"),
            "target_position": row.get("target_position"),
        },
        "plan": {
            "code": row.get("plan_code") or "free",
            "name": row.get("plan_name") or "Free",
            "quota_total": int(row.get("plan_quota_total") or 0),
            "rate_limit_per_minute": int(row.get("plan_rate_limit_per_minute") or 0),
        },
        "usage": {
            "total_sessions": int(row.get("total_sessions") or 0),
            "ai_calls": int(row.get("ai_calls") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
            "total_cost_usd": _money(row.get("total_cost_usd") or 0),
            "activity_events": int(row.get("activity_events") or 0),
        },
    }


def _session_filters(
    search: str | None,
    status_filter: str | None,
    session_type: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if search:
        pattern = f"%{search.strip().lower()}%"
        clauses.append(
            """
            (
              lower(u.email) like %s
              or lower(u.full_name) like %s
              or lower(s.position) like %s
              or lower(coalesce(s.company, '')) like %s
              or s.id::text like %s
            )
            """
        )
        params.extend([pattern, pattern, pattern, pattern, pattern])
    if status_filter:
        clauses.append("s.status = %s")
        params.append(status_filter)
    if session_type:
        clauses.append("s.session_type = %s")
        params.append(session_type)
    return ("where " + " and ".join(clauses), params) if clauses else ("", params)


def _session_payload(row: dict[str, Any]) -> dict[str, Any]:
    status_value = str(row.get("status") or "scheduled")
    type_value = str(row.get("session_type") or "mixed")
    full_name = row.get("full_name") or row.get("email") or "Unknown user"
    return {
        "id": str(row["id"]),
        "userId": str(row["user_id"]),
        "userName": full_name,
        "position": row.get("position") or "Interview",
        "company": row.get("company") or "General",
        "date": _iso(row.get("started_at") or row.get("scheduled_at") or row.get("created_at")),
        "duration": _format_duration(row.get("duration_seconds")),
        "score": row.get("overall_score"),
        "status": _display_status(status_value),
        "type": _display_type(type_value),
        "avatarInitials": _initials(full_name, row.get("email") or ""),
        "avatarColor": _avatar_color(str(row["user_id"])),
    }


def _activity_base_query() -> str:
    return """
      select
        e.id::text as id,
        e.occurred_at,
        e.event_name,
        e.object_type,
        e.object_id,
        e.success,
        e.duration_ms,
        e.meta_json,
        e.external_auth_id,
        u.id::text as user_id,
        u.email,
        u.full_name,
        case
          when e.event_name in ('interview_started', 'interview_completed', 'realtime_token_created', 'report_saved') then 'Session'
          when e.event_name in ('file_parse', 'cvjd_saved', 'cvjd_cache_hit') then 'Account'
          else 'System'
        end as type,
        case
          when e.event_name = 'file_parse' then 'Parsed uploaded file'
          when e.event_name = 'cvjd_saved' then 'Saved CV/JD analysis'
          when e.event_name = 'cvjd_cache_hit' then 'Reused cached CV/JD analysis'
          when e.event_name = 'realtime_token_created' then 'Created realtime token'
          when e.event_name = 'interview_started' then 'Started interview'
          when e.event_name = 'interview_completed' then 'Completed interview'
          when e.event_name = 'report_saved' then 'Saved interview report'
          else e.event_name
        end as action,
        coalesce(e.object_type, e.event_name) as detail
      from user_activity_events e
      left join users u on u.id = e.user_id
    """


def _activity_filters(search: str | None, type_filter: str | None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if search:
        pattern = f"%{search.strip().lower()}%"
        clauses.append(
            """
            (
              lower(coalesce(a.full_name, '')) like %s
              or lower(coalesce(a.email, '')) like %s
              or lower(a.action) like %s
              or lower(a.detail) like %s
              or lower(a.id) like %s
            )
            """
        )
        params.extend([pattern, pattern, pattern, pattern, pattern])
    if type_filter:
        clauses.append("a.type = %s")
        params.append(type_filter)
    return ("where " + " and ".join(clauses), params) if clauses else ("", params)


def _activity_payload(row: dict[str, Any]) -> dict[str, Any]:
    user_name = row.get("full_name") or row.get("email") or row.get("external_auth_id") or "System"
    detail_parts = [str(row.get("detail") or "event")]
    meta = row.get("meta_json")
    if isinstance(meta, dict):
        for key in ("position", "target_position", "file_name", "duration_seconds"):
            if meta.get(key) not in (None, ""):
                detail_parts.append(f"{key}: {meta[key]}")
                break
    return {
        "id": str(row["id"]),
        "userId": row.get("user_id") or row.get("external_auth_id") or "system",
        "userName": user_name,
        "action": row.get("action") or row.get("event_name"),
        "detail": " - ".join(detail_parts),
        "timestamp": _iso(row.get("occurred_at")),
        "ip": "n/a",
        "type": row.get("type") or "System",
        "avatarInitials": _initials(user_name, row.get("email") or ""),
        "avatarColor": _avatar_color(str(row.get("user_id") or row.get("external_auth_id") or row["id"])),
    }


def _tracked_user_filter(
    date_column: str,
    days: int,
    user_id: str | None = None,
    external_auth_id: str | None = None,
) -> tuple[str, list[Any]]:
    clauses = [f"{date_column} >= now() - (%s * interval '1 day')"]
    params: list[Any] = [days]
    if user_id is not None:
        user_clauses = ["user_id = %s"]
        params.append(user_id)
        if external_auth_id:
            user_clauses.append("external_auth_id = %s")
            params.append(external_auth_id)
        clauses.append("(" + " or ".join(user_clauses) + ")")
    return " and ".join(clauses), params


def _window_payload(days: int, group_by: GroupBy) -> dict[str, Any]:
    today = date.today()
    start = today - timedelta(days=days - 1)
    return {
        "date_from": start.isoformat(),
        "date_to": today.isoformat(),
        "group_by": group_by,
        "days": days,
    }


def _display_status(value: str) -> str:
    return {
        "completed": "Completed",
        "in_progress": "In Progress",
        "scheduled": "Scheduled",
        "cancelled": "Cancelled",
    }.get(value, "Scheduled")


def _display_type(value: str) -> str:
    return {
        "technical": "Technical",
        "behavioral": "Behavioral",
        "mixed": "Mixed",
        "coding": "Coding",
    }.get(value, "Mixed")


def _format_duration(seconds: Any) -> str:
    if seconds is None:
        return "-"
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "-"
    minutes = total // 60
    remaining = total % 60
    if minutes <= 0:
        return f"{remaining}s"
    if remaining == 0:
        return f"{minutes}m"
    return f"{minutes}m {remaining}s"


def _initials(name: str, email: str) -> str:
    source = (name or email or "U").strip()
    parts = source.split()
    if len(parts) >= 2:
        return f"{parts[0][0]}{parts[1][0]}".upper()
    return source[:2].upper()


def _avatar_color(seed: str) -> str:
    colors = [
        "bg-slate-700",
        "bg-teal-600",
        "bg-blue-600",
        "bg-violet-600",
        "bg-amber-600",
        "bg-rose-600",
    ]
    value = sum(ord(char) for char in seed or "0")
    return colors[value % len(colors)]


def _uuid_or_404(value: str, message: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=message) from exc


def _money(value: Any) -> str:
    try:
        amount = Decimal(str(value or 0))
    except Exception:
        amount = Decimal("0")
    return str(amount.quantize(Decimal("0.000001")))


def _date_key(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)
