from datetime import date

from fastapi import APIRouter, Depends, Query, Request

from core.clerk_auth import AdminUser, require_admin
from core.rate_limit import limiter
from schemas.events import LandingMetricGroupBy, LandingSource
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
