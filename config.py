"""Centralized configuration — loads environment variables once at import time."""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Runtime environment
# ---------------------------------------------------------------------------

APP_ENV: str = os.getenv("APP_ENV", "production").lower()

ENABLE_DB_ADMIN_ROUTES: bool = os.getenv("ENABLE_DB_ADMIN_ROUTES", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_REALTIME_MODEL: str = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2")

# Fail fast — crash at startup rather than at the first AI request
if not OPENAI_API_KEY:
    print(
        "[FATAL] OPENAI_API_KEY is not set. "
        "Add it to backend/.env and restart the server.",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Clerk authentication
# ---------------------------------------------------------------------------

# JWKS URL for verifying Clerk-issued JWTs.
# Derived from your Clerk publishable key domain, e.g.:
#   pk_live_Y2xlcmsudmllY2FyZWVyLnNpdGUk → clerk.viecareer.site
# Set in backend/.env:
#   CLERK_JWKS_URL=https://clerk.viecareer.site/.well-known/jwks.json
CLERK_JWKS_URL: str | None = os.getenv("CLERK_JWKS_URL")
CLERK_SECRET_KEY: str | None = os.getenv("CLERK_SECRET_KEY")

# Optional: strict issuer check. Set to your Clerk frontend API URL.
# Leave blank to skip issuer verification (still verifies signature + expiry).
CLERK_ISSUER: str | None = os.getenv("CLERK_ISSUER", "").strip() or (
    CLERK_JWKS_URL.removesuffix("/.well-known/jwks.json") if CLERK_JWKS_URL else None
)

if not CLERK_JWKS_URL:
    print(
        "[FATAL] CLERK_JWKS_URL is not set. "
        "Add it to backend/.env: "
        "CLERK_JWKS_URL=https://clerk.viecareer.site/.well-known/jwks.json",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

# Production default: only the two canonical viecareer.site origins.
# Override via CORS_ORIGINS env var (comma-separated) for local dev or staging.
_raw_origins = os.getenv(
    "CORS_ORIGINS",
    "https://viecareer.site,https://www.viecareer.site",
)
CORS_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

# Regex fallback — allows localhost during local dev without any env override.
_default_cors_origin_regex = (
    r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
    if APP_ENV in {"development", "dev", "test"}
    else r"(?!)"
)
CORS_ORIGIN_REGEX = os.getenv(
    "CORS_ORIGIN_REGEX",
    _default_cors_origin_regex,
)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

# Supabase PostgreSQL connection. Must point at the transaction pooler
# host on port 6543 for low-latency pooled access.
DATABASE_URL: str | None = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")

# ---------------------------------------------------------------------------
# payOS billing
# ---------------------------------------------------------------------------

PAYOS_CLIENT_ID: str | None = os.getenv("PAYOS_CLIENT_ID")
PAYOS_API_KEY: str | None = os.getenv("PAYOS_API_KEY")
PAYOS_CHECKSUM_KEY: str | None = os.getenv("PAYOS_CHECKSUM_KEY")
PAYOS_API_BASE: str = os.getenv("PAYOS_API_BASE", "https://api-merchant.payos.vn").rstrip("/")

FRONTEND_BASE_URL: str = os.getenv("FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")
PAYOS_RETURN_URL: str = os.getenv("PAYOS_RETURN_URL", f"{FRONTEND_BASE_URL}/billing/return")
PAYOS_CANCEL_URL: str = os.getenv("PAYOS_CANCEL_URL", f"{FRONTEND_BASE_URL}/billing/cancel")
BILLING_JOB_SECRET: str | None = os.getenv("BILLING_JOB_SECRET")
BILLING_VND_PER_USD: str = os.getenv("BILLING_VND_PER_USD", "25000")

# Optional shared backend for slowapi/limits. Example: redis://redis:6379/0
RATE_LIMIT_STORAGE_URI: str | None = os.getenv("RATE_LIMIT_STORAGE_URI")
