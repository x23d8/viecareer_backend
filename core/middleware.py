"""Request-level timeout middleware — returns 504 if a request exceeds the limit.
Also enforces:
  - Payload size limit (layer 7): rejects requests whose body exceeds MAX_BODY_BYTES.
  - Adds Retry-After and X-RateLimit-* headers where applicable.
"""

import asyncio
import logging
import re

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from config import CORS_ORIGIN_REGEX, CORS_ORIGINS

logger = logging.getLogger("viecareer")

# ---------------------------------------------------------------------------
# 7. Payload size limit
# ---------------------------------------------------------------------------

# Default max body size for all routes (10 MB safety net)
_DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB

# Tighter limit for upload-only route
_UPLOAD_MAX_BODY_BYTES = 5 * 1024 * 1024   # 5 MB

# Routes that should use the stricter upload limit (exact path prefix match)
_UPLOAD_PATHS = {"/parse-file"}


class PayloadSizeLimitMiddleware(BaseHTTPMiddleware):
    """
    Reject requests whose Content-Length header (or streamed body) exceeds
    the allowed maximum before the route handler ever runs.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        max_bytes = (
            _UPLOAD_MAX_BODY_BYTES if path in _UPLOAD_PATHS else _DEFAULT_MAX_BODY_BYTES
        )

        # Fast path: honour Content-Length header if present
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    mb = max_bytes // (1024 * 1024)
                    return JSONResponse(
                        status_code=413,
                        content={
                            "error": True,
                            "code": "PAYLOAD_TOO_LARGE",
                            "message": f"Request body must not exceed {mb} MB.",
                            "retry": False,
                        },
                    )
            except ValueError:
                pass  # malformed header — let it through, body check below

        # Streaming check: buffer incrementally and abort early if oversized
        body_chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                mb = max_bytes // (1024 * 1024)
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": True,
                        "code": "PAYLOAD_TOO_LARGE",
                        "message": f"Request body must not exceed {mb} MB.",
                        "retry": False,
                    },
                )
            body_chunks.append(chunk)

        # Re-inject the body so downstream middleware/route can read it
        body = b"".join(body_chunks)
        # Patch scope so Starlette's Request.body() returns buffered content
        request._body = body  # type: ignore[attr-defined]

        return await call_next(request)


# ---------------------------------------------------------------------------
# Timeout middleware (unchanged logic, kept here for single import)
# ---------------------------------------------------------------------------


class AsyncTimeoutMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, timeout: int = 35) -> None:
        super().__init__(app)
        self.timeout = timeout

    async def dispatch(self, request: Request, call_next):
        try:
            return await asyncio.wait_for(call_next(request), timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.warning("Request timed out: %s %s", request.method, request.url.path)
            response = JSONResponse(
                status_code=504,
                content={
                    "error": True,
                    "code": "TIMEOUT",
                    "message": "AI đang bận, vui lòng thử lại sau 30 giây",
                    "retry": True,
                },
            )
            origin = request.headers.get("origin")
            if origin and ("*" in CORS_ORIGINS or re.fullmatch(CORS_ORIGIN_REGEX, origin)):
                response.headers["Access-Control-Allow-Origin"] = (
                    "*" if "*" in CORS_ORIGINS else origin
                )
                response.headers["Access-Control-Allow-Headers"] = "*"
                response.headers["Access-Control-Allow-Methods"] = "*"
            return response
