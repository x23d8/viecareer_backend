"""
Clerk JWT authentication dependency for FastAPI.

How it works:
  1. The frontend (Next.js + Clerk) issues a signed JWT to the browser.
  2. The browser includes it in every API request as:
       Authorization: Bearer <jwt>
  3. This module verifies the signature against Clerk's public JWKS endpoint,
     checks expiry and issuer, then returns a ClerkUser with the Clerk user_id.
  4. For WebSocket connections (where headers are limited) the token is passed
     as a query parameter:  ws://host/realtime/ws?token=<jwt>

Configuration (backend/.env):
  CLERK_JWKS_URL=https://clerk.viecareer.site/.well-known/jwks.json
  CLERK_ISSUER=https://clerk.viecareer.site   # optional — set for stricter check
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

import httpx
import jwt
from fastapi import Depends, HTTPException, Query, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient, PyJWKClientError

from config import APP_ENV, CLERK_ISSUER, CLERK_JWKS_URL, CLERK_SECRET_KEY
from core.database import get_connection

logger = logging.getLogger("viecareer")

# ---------------------------------------------------------------------------
# JWKS client — fetches & caches Clerk's public keys (TTL handled internally)
# ---------------------------------------------------------------------------

_jwks_client: Optional[PyJWKClient] = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        if not CLERK_JWKS_URL:
            raise RuntimeError(
                "CLERK_JWKS_URL is not configured. "
                "Add it to backend/.env: CLERK_JWKS_URL=https://clerk.<domain>/.well-known/jwks.json"
            )
        # cache_keys=True → PyJWT caches JWKS and refreshes on unknown kid
        _jwks_client = PyJWKClient(CLERK_JWKS_URL, cache_keys=True, lifespan=3600)
    return _jwks_client


# ---------------------------------------------------------------------------
# ClerkUser — the authenticated principal injected into route handlers
# ---------------------------------------------------------------------------

@dataclass
class ClerkUser:
    user_id: str                              # Clerk user_id, e.g. "user_2abc..."
    email: Optional[str] = None              # present when Clerk session includes email claim
    role: Optional[str] = None               # sourced from verified Clerk JWT claims only
    public_metadata: dict[str, Any] | None = None


@dataclass
class AdminUser(ClerkUser):
    internal_user_id: str = ""
    full_name: str = ""
    role_id: int = 1


# ---------------------------------------------------------------------------
# JWT claim helpers
# ---------------------------------------------------------------------------

def _extract_public_metadata(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return Clerk public metadata from supported JWT claim shapes."""
    for key in ("public_metadata", "publicMetadata"):
        metadata = payload.get(key)
        if isinstance(metadata, dict):
            return metadata
    return None


def _extract_role(payload: dict[str, Any]) -> str | None:
    """
    Extract the application role from verified Clerk JWT claims.

    Supported claim shapes:
      - {"role": "admin"}
      - {"public_metadata": {"role": "admin"}}
      - {"publicMetadata": {"role": "admin"}}
    """
    direct_role = payload.get("role")
    if isinstance(direct_role, str) and direct_role.strip():
        return direct_role.strip().lower()

    public_metadata = _extract_public_metadata(payload)
    metadata_role = public_metadata.get("role") if public_metadata else None
    if isinstance(metadata_role, str) and metadata_role.strip():
        return metadata_role.strip().lower()

    return None


# ---------------------------------------------------------------------------
# Core token verification (used by both HTTP and WebSocket paths)
# ---------------------------------------------------------------------------

def _verify_token(token: str) -> ClerkUser:
    """
    Decode and verify a Clerk-issued JWT.
    Raises HTTPException 401 on any failure.
    """
    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)
    except PyJWKClientError as exc:
        logger.warning("JWKS fetch / key lookup failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not fetch authentication keys. Please try again.",
        )
    except Exception as exc:
        logger.warning("JWT signing key error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
        )

    decode_options: dict = {
        "verify_exp": True,
        "verify_nbf": True,
    }
    decode_kwargs: dict = {
        "algorithms": ["RS256"],
        "options": decode_options,
        "leeway": 10,  # 10 s clock skew tolerance
    }
    # Only verify issuer when CLERK_ISSUER is explicitly configured
    if CLERK_ISSUER:
        decode_kwargs["issuer"] = CLERK_ISSUER
    else:
        decode_kwargs["options"]["verify_iss"] = False

    # Clerk JWTs do not use a standard "aud" claim — skip audience verification
    decode_kwargs["options"]["verify_aud"] = False

    try:
        payload = jwt.decode(token, signing_key.key, **decode_kwargs)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired. Please sign in again.",
        )
    except jwt.InvalidTokenError as exc:
        logger.warning("JWT decode failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
        )

    user_id: str | None = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is missing subject claim.",
        )

    return ClerkUser(
        user_id=user_id,
        email=payload.get("email"),
        role=_extract_role(payload),
        public_metadata=_extract_public_metadata(payload),
    )


def verified_request_email(current_user: ClerkUser, requested_email: str) -> str:
    """Resolve the email from verified Clerk data and reject body mismatches."""
    requested = str(requested_email or "").strip().lower()
    verified = str(current_user.email or "").strip().lower()

    if not verified and CLERK_SECRET_KEY:
        try:
            response = httpx.get(
                f"https://api.clerk.com/v1/users/{quote(current_user.user_id, safe='')}",
                headers={"Authorization": f"Bearer {CLERK_SECRET_KEY}"},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            primary_id = payload.get("primary_email_address_id")
            for item in payload.get("email_addresses") or []:
                if item.get("id") == primary_id:
                    verified = str(item.get("email_address") or "").strip().lower()
                    break
        except Exception as exc:
            logger.exception("Could not resolve Clerk primary email for user=%s", current_user.user_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not verify the authenticated email address.",
            ) from exc

    if not verified:
        if APP_ENV == "production":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Clerk email verification is not configured.",
            )
        verified = requested

    if requested != verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email does not match the authenticated Clerk identity.",
        )
    return verified

# ---------------------------------------------------------------------------
# FastAPI HTTP dependencies
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)


def _request_context(request: Request | WebSocket) -> str:
    client_host = request.client.host if request.client else "unknown"
    origin = request.headers.get("origin", "-")
    user_agent = request.headers.get("user-agent", "-")
    return (
        f"path={request.url.path} client={client_host} "
        f"origin={origin} user_agent={user_agent[:120]}"
    )


def require_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> ClerkUser:
    """
    FastAPI dependency — require a valid Clerk JWT in the Authorization header.

    Usage:
        @router.post("/some-endpoint")
        async def handler(user: ClerkUser = Depends(require_auth)):
            ...
    """
    if not credentials or not credentials.credentials:
        logger.warning("Authentication missing Authorization bearer token: %s", _request_context(request))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return _verify_token(credentials.credentials)
    except HTTPException:
        logger.warning("Authentication rejected bearer token: %s", _request_context(request))
        raise


def optional_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> ClerkUser | None:
    """
    FastAPI dependency — optionally authenticate via Clerk JWT.
    Returns None (rather than raising) when the token is absent or invalid.

    Usage:
        @router.get("/some-endpoint")
        async def handler(user: ClerkUser | None = Depends(optional_auth)):
            ...
    """
    if not credentials or not credentials.credentials:
        return None
    try:
        return _verify_token(credentials.credentials)
    except HTTPException:
        logger.info("Optional authentication ignored invalid bearer token: %s", _request_context(request))
        return None


def require_admin(
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
) -> AdminUser:
    """
    FastAPI dependency — require an authenticated user with admin role_id.

    Admin access is decided by the users table, not by Clerk metadata or
    frontend state. In the current schema role_id=1 is the admin role.
    The authenticated Clerk subject must be explicitly linked to the row;
    email fallback is intentionally forbidden.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id, email, full_name, role_id
                    from users
                    where status = 'active'
                      and external_auth_id = %s
                    limit 1
                    """,
                    (current_user.user_id,),
                )
                row = cur.fetchone()
    except Exception as exc:
        logger.exception("Admin role lookup failed for user=%s", current_user.user_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not verify admin privileges.",
        ) from exc

    if not row or row["role_id"] != 1:
        logger.warning(
            "Admin authorization denied for user=%s role_id=%s jwt_role=%s: %s",
            current_user.user_id,
            row["role_id"] if row else "-",
            current_user.role or "-",
            _request_context(request),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required.",
        )

    return AdminUser(
        user_id=current_user.user_id,
        email=row["email"] or current_user.email,
        role=current_user.role,
        public_metadata=current_user.public_metadata,
        internal_user_id=str(row["id"]),
        full_name=row["full_name"] or "",
        role_id=row["role_id"],
    )


# ---------------------------------------------------------------------------
# WebSocket token helper (token passed as ?token= query param)
# ---------------------------------------------------------------------------

async def verify_ws_token(
    websocket: WebSocket,
    token: Optional[str] = Query(default=None, alias="token"),
) -> ClerkUser:
    """
    WebSocket auth dependency — the client passes the Clerk JWT as a query
    parameter because WebSocket headers are not reliably supported in browsers.

    Client connects to: ws://host/realtime/ws?token=<clerk_jwt>
    """
    if not token:
        logger.warning("WebSocket authentication missing token query parameter: %s", _request_context(websocket))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token.",
        )
    try:
        return _verify_token(token)
    except HTTPException:
        logger.warning("WebSocket authentication rejected query token: %s", _request_context(websocket))
        raise
