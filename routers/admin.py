from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from fastapi import HTTPException, status

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


@router.get("/me")
def get_current_admin(current_admin: AdminUser = Depends(require_admin)):
    return {"data": _admin_payload(current_admin)}


@router.get("/overview")
def get_admin_overview(current_admin: AdminUser = Depends(require_admin)):
    return {
        "data": {
            "current_admin": _admin_payload(current_admin),
            "dashboard": {},
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
