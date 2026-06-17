from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from config import BILLING_JOB_SECRET
from core.clerk_auth import ClerkUser, require_auth
from core.rate_limit import limiter
from schemas.billing import BillingCheckoutRequest
from services.billing_service import (
    create_payos_checkout,
    expire_due_subscriptions_sync,
    get_billing_overview,
    handle_payos_webhook,
    list_payment_history,
    sync_payos_order,
)

router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("/plans")
@limiter.limit("60/minute")
def billing_plans(
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
) -> dict[str, Any]:
    overview = get_billing_overview(
        external_auth_id=current_user.user_id,
        email=current_user.email,
    )
    return {"data": overview["plans"]}


@router.get("/me")
@limiter.limit("60/minute")
def billing_me(
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
) -> dict[str, Any]:
    return {
        "data": get_billing_overview(
            external_auth_id=current_user.user_id,
            email=current_user.email,
        )
    }


@router.get("/history")
@limiter.limit("60/minute")
def billing_history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    current_user: ClerkUser = Depends(require_auth),
) -> dict[str, Any]:
    return {
        "data": list_payment_history(
            external_auth_id=current_user.user_id,
            email=current_user.email,
            limit=limit,
        )
    }


@router.post("/checkout")
@limiter.limit("20/minute")
async def billing_checkout(
    request: Request,
    payload: BillingCheckoutRequest,
    current_user: ClerkUser = Depends(require_auth),
) -> dict[str, Any]:
    checkout = await create_payos_checkout(
        external_auth_id=current_user.user_id,
        email=current_user.email,
        plan_code=payload.plan_code,
    )
    return {"data": checkout}


@router.post("/orders/{order_code}/sync")
@limiter.limit("30/minute")
async def billing_sync_order(
    request: Request,
    order_code: int,
    current_user: ClerkUser = Depends(require_auth),
) -> dict[str, Any]:
    return {
        "data": await sync_payos_order(
            external_auth_id=current_user.user_id,
            email=current_user.email,
            order_code=order_code,
        )
    }


@router.post("/payos/webhook")
@limiter.limit("120/minute")
def payos_webhook(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    return handle_payos_webhook(payload)


@router.post("/jobs/expire-subscriptions")
@limiter.limit("12/minute")
def expire_subscriptions_job(
    request: Request,
    x_billing_job_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    if not BILLING_JOB_SECRET or x_billing_job_secret != BILLING_JOB_SECRET:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Invalid billing job secret.")
    return {"data": {"expired_count": expire_due_subscriptions_sync()}}
