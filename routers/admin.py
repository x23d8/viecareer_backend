from fastapi import APIRouter, Depends, HTTPException, Query, status

from core.api_usage import ensure_tracking_tables
from core.clerk_auth import AdminUser, require_admin
from core.database import get_connection

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
