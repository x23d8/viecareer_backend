"""Auth router — Clerk-backed user synchronisation.

All mock login/register/logout endpoints have been removed.
Authentication is handled entirely by Clerk on the frontend.
The only backend auth concern is:
  1. Verifying the Clerk JWT on every protected request (core/clerk_auth.py).
  2. Syncing the Clerk user profile to our Supabase users table on first sign-in
     and on profile updates (/api/auth/sync-user).
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from core.clerk_auth import ClerkUser, require_auth
from core.rate_limit import limiter
from schemas.auth import AcceptLegalPoliciesRequest, AuthSyncUserRequest
from services.user_service import accept_legal_policies, get_legal_acceptance_status, upsert_auth_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/sync-user")
@limiter.limit("20/minute")
async def sync_user(
    request: Request,
    body: AuthSyncUserRequest,
    current_user: ClerkUser = Depends(require_auth),
) -> dict[str, Any]:
    """
    Sync the authenticated Clerk user into the Supabase users table.

    Called by the frontend immediately after sign-in / sign-up via Clerk.
    The JWT's `sub` claim must match `body.user_id` to prevent any caller
    from syncing data on behalf of a different user (account-takeover fix).
    """
    # Security: verify the token's subject matches the requested user_id
    if current_user.user_id != body.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token user_id does not match the request body.",
        )

    full_name = (body.full_name or "").strip() or str(body.email).split("@")[0]
    user = upsert_auth_user(
        external_auth_id=body.user_id,
        email=str(body.email),
        full_name=full_name,
        avatar_url=body.avatar_url,
        auth_provider=body.auth_provider,
    )
    return {"data": user}


@router.get("/legal-status")
@limiter.limit("60/minute")
async def legal_status(
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
) -> dict[str, Any]:
    status_payload = get_legal_acceptance_status(
        external_auth_id=current_user.user_id,
        email=current_user.email,
    )
    return {"data": status_payload}


@router.post("/accept-legal")
@limiter.limit("20/minute")
async def accept_legal(
    request: Request,
    body: AcceptLegalPoliciesRequest,
    current_user: ClerkUser = Depends(require_auth),
) -> dict[str, Any]:
    status_payload = accept_legal_policies(
        external_auth_id=current_user.user_id,
        email=current_user.email,
        terms_version=body.terms_version,
        privacy_policy_version=body.privacy_policy_version,
    )
    return {"data": status_payload}
