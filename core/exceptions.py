"""Global exception handling — logs full errors, returns safe messages to clients."""

import logging

from fastapi import Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("viecareer")

# HTTP status → (code, message, retry)
_STATUS_MAP: dict[int, tuple[str, str, bool]] = {
    503: ("AI_ERROR", "Dịch vụ AI tạm thời gián đoạn", True),
    504: ("TIMEOUT", "AI đang bận, vui lòng thử lại sau 30 giây", True),
    422: ("PARSE_ERROR", "File không đọc được, vui lòng kiểm tra lại", False),
}


def _err(code: str, message: str, retry: bool) -> dict:
    return {"error": True, "code": code, "message": message, "retry": retry}


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)
    if exc.status_code in _STATUS_MAP:
        code, message, retry = _STATUS_MAP[exc.status_code]
        return JSONResponse(
            status_code=exc.status_code,
            content=_err(code, message, retry),
            headers=exc.headers,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=_err("ERROR", str(exc.detail), False),
        headers=exc.headers,
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_err("PARSE_ERROR", "Dữ liệu không hợp lệ, vui lòng kiểm tra lại", False),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler so unexpected errors never leak stack traces to clients."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=_err("SERVER_ERROR", "Internal server error", False),
    )
