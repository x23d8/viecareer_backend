from fastapi import APIRouter, Depends, Request, status

from core.clerk_auth import ClerkUser, optional_auth
from core.rate_limit import limiter
from schemas.events import LandingEventRequest
from services.landing_event_service import record_landing_event

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.post("/events", status_code=status.HTTP_201_CREATED)
@limiter.limit("60/minute")
def create_analytics_event(
    request: Request,
    payload: LandingEventRequest,
    current_user: ClerkUser | None = Depends(optional_auth),
):
    forwarded_for = request.headers.get("x-forwarded-for")
    ip_address = forwarded_for.split(",")[0].strip() if forwarded_for else None
    if not ip_address and request.client:
        ip_address = request.client.host

    return {
        "data": record_landing_event(
            payload,
            external_auth_id=current_user.user_id if current_user else None,
            ip_address=ip_address,
            user_agent=request.headers.get("user-agent"),
        )
    }
