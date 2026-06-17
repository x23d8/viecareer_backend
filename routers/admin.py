import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from core.api_usage import ensure_tracking_tables
from core.clerk_auth import AdminUser, require_admin
from core.database import get_connection
from core.rate_limit import limiter
from schemas.events import LandingMetricGroupBy, LandingSource
from services.admin_user_service import UserMetricGroupBy, get_admin_user_metrics, list_admin_users
from services.feature_tracking_service import FeatureGroupBy, get_feature_metrics
from services.landing_event_service import get_landing_metrics

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _admin_payload(admin: AdminUser) -> dict:
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


@router.get("/usage/summary")
def get_usage_summary(
    days: int = Query(28, ge=1, le=90),
    current_admin: AdminUser = Depends(require_admin),
):
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
    return {
        "data": get_landing_metrics(
            date_from=date_from,
            date_to=date_to,
            source=source,
            group_by=group_by,
        )
    }
