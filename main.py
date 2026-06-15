"""VieCareer API — application entrypoint. Run with: uvicorn main:app"""

import logging

from fastapi import FastAPI, Response
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from config import CORS_ORIGIN_REGEX, CORS_ORIGINS, ENABLE_DB_ADMIN_ROUTES
from core.exceptions import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from core.middleware import AsyncTimeoutMiddleware, PayloadSizeLimitMiddleware
from core.rate_limit import limiter
from routers import admin, analysis, auth, chat, files, health, interview_simulation, realtime

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="VieCareer API")

# ── Attach slowapi limiter state ────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Middleware stack (applied innermost-first, i.e. last-added = outermost) ─

# 7. Payload size — must be outermost so large payloads are rejected early
app.add_middleware(PayloadSizeLimitMiddleware)

# Timeout — wraps everything so long-running AI calls are cut off
app.add_middleware(AsyncTimeoutMiddleware, timeout=90)

# 1. IP rate limiter via slowapi
app.add_middleware(SlowAPIMiddleware)

# CORS — credentials cannot be combined with a wildcard origin.
# Production: explicit origins only. Add localhost for local dev via env.
_allow_credentials = CORS_ORIGINS != ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Exception handlers ──────────────────────────────────────────────────────
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)


@app.head("/ping")
def ping():
    return Response(status_code=200)


# ── Routers ─────────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(admin.router)
app.include_router(auth.router)
app.include_router(files.router)
app.include_router(chat.router)
app.include_router(analysis.router)
app.include_router(interview_simulation.router)
app.include_router(realtime.router)

if ENABLE_DB_ADMIN_ROUTES:
    from routers import database

    app.include_router(database.router)
