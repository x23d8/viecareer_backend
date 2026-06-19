"""Liveness and dependency-aware readiness endpoints."""

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import CLERK_JWKS_URL, OPENAI_API_KEY
from core.database import get_connection

router = APIRouter(tags=["health"])


def _check_database() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("select 1 as ok")
            row = cur.fetchone()
            if not row or int(row["ok"]) != 1:
                raise RuntimeError("Database readiness query failed")


@router.get("/")
def home():
    return {"status": "healthy"}


@router.get("/health/live")
def live():
    return {"status": "alive"}


@router.get("/health/ready")
async def ready():
    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", OPENAI_API_KEY),
            ("CLERK_JWKS_URL", CLERK_JWKS_URL),
        )
        if not value
    ]
    if missing:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "missing": missing},
        )

    try:
        await asyncio.to_thread(_check_database)
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unavailable"},
        )

    return {"status": "ready", "database": "available"}