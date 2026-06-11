"""Realtime voice endpoints."""

import logging

from fastapi import APIRouter, Depends, Request, WebSocket

from core.clerk_auth import ClerkUser, require_auth, verify_ws_token
from core.rate_limit import (
    WS_MAX_PER_IP,
    acquire_ws_slot,
    check_daily_quota,
    limiter,
    release_ws_slot,
)
from services.realtime_service import create_realtime_session, relay_realtime_websocket

logger = logging.getLogger("viecareer")

router = APIRouter(tags=["realtime"])


@router.get("/realtime-token")
@limiter.limit("5/minute")
async def get_realtime_token(
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
):
    """
    Create an ephemeral OpenAI Realtime session token.
    Requires a valid Clerk JWT — anonymous callers are rejected.
    """
    await check_daily_quota()
    return create_realtime_session()


@router.websocket("/realtime/ws")
async def realtime_websocket(
    websocket: WebSocket,
    current_user: ClerkUser = Depends(verify_ws_token),
):
    """
    Server-owned OpenAI Realtime WebSocket relay.

    The client must pass a valid Clerk JWT as a query parameter:
        ws://host/realtime/ws?token=<clerk_jwt>

    Connection is rejected before the WebSocket handshake completes
    if the token is missing or invalid.
    """
    client_ip = websocket.client.host if websocket.client else "unknown"

    try:
        await acquire_ws_slot(client_ip)
    except Exception as exc:
        logger.warning("WS connection rejected for ip=%s user=%s: %s", client_ip, current_user.user_id, exc)
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "viecareer.error",
                "code": "WS_LIMIT",
                "message": f"Maximum {WS_MAX_PER_IP} concurrent realtime sessions per IP reached.",
            }
        )
        await websocket.close(code=1008)
        return

    try:
        await relay_realtime_websocket(websocket)
    finally:
        await release_ws_slot(client_ip)
