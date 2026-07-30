"""Authenticated state shared by the web and native clients."""

import asyncio

from fastapi import APIRouter, Depends, Query, Request

from core.clerk_auth import ClerkUser, require_auth
from core.rate_limit import limiter
from services.sync_state_service import get_sync_state


router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.get("/state")
@limiter.limit("30/minute")
async def sync_state(
    request: Request,
    report_limit: int = Query(default=20, ge=1, le=50),
    current_user: ClerkUser = Depends(require_auth),
):
    """Hydrate cross-device product state using only the authenticated JWT subject."""
    data = await asyncio.to_thread(
        get_sync_state,
        external_auth_id=current_user.user_id,
        report_limit=report_limit,
    )
    return {"data": data}
