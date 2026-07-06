"""Realtime voice endpoints."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket

from core.api_usage import track_user_activity_async
from core.clerk_auth import ClerkUser, require_auth, verified_request_email, verify_ws_token
from core.rate_limit import (
    WS_MAX_PER_IP,
    acquire_ws_slot,
    check_daily_quota,
    limiter,
    release_ws_slot,
)
from services.plan_quota_service import FEATURE_REALTIME_INTERVIEW, METRIC_SESSION_COUNT, check_plan_quota
from schemas.realtime import RealtimeSessionEndRequest, RealtimeSessionMetadata
from services.plan_quota_service import get_realtime_session_limit_seconds
from services.realtime_service import (
    create_realtime_session,
    create_webrtc_start_payload,
    finalize_realtime_session,
    relay_realtime_websocket,
)
from services.realtime_transcript_service import create_live_interview_session

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
    await check_plan_quota(
        external_auth_id=current_user.user_id,
        feature_code=FEATURE_REALTIME_INTERVIEW,
        metric=METRIC_SESSION_COUNT,
    )
    await check_daily_quota()
    token = await asyncio.to_thread(create_realtime_session)
    track_user_activity_async(
        "realtime_token_created",
        external_auth_id=current_user.user_id,
        meta={"endpoint": "/realtime-token"},
    )
    return token


@router.post("/realtime/start")
@limiter.limit("5/minute")
async def start_realtime_session(
    request: Request,
    payload: RealtimeSessionMetadata,
    current_user: ClerkUser = Depends(require_auth),
):
    """Create a DB-backed OpenAI Realtime WebRTC session for the browser."""
    if payload.external_auth_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="Realtime metadata user does not match the authenticated user")

    verified_request_email(current_user, str(payload.email))
    metadata = payload.model_dump(mode="json")

    await check_plan_quota(
        external_auth_id=current_user.user_id,
        feature_code=FEATURE_REALTIME_INTERVIEW,
        metric=METRIC_SESSION_COUNT,
    )
    await check_daily_quota()
    max_duration_seconds = await get_realtime_session_limit_seconds(external_auth_id=current_user.user_id)

    session_id = await asyncio.to_thread(create_live_interview_session, metadata)
    if not session_id:
        raise HTTPException(status_code=500, detail="Could not persist realtime interview context")

    response = await asyncio.to_thread(
        create_webrtc_start_payload,
        session_id=session_id,
        metadata=metadata,
        max_duration_seconds=max_duration_seconds,
    )
    track_user_activity_async(
        "interview_started",
        external_auth_id=current_user.user_id,
        object_type="interview_session",
        object_id=session_id,
        meta={
            "position": metadata.get("position"),
            "language": metadata.get("language"),
            "interview_style": metadata.get("interview_style"),
            "voice_mode": metadata.get("voice_mode"),
            "transport": "webrtc",
            "question_count": len((metadata.get("blueprint") or {}).get("questions") or []),
        },
    )
    return {
        **response,
        "realtimeSessionId": response["realtime_session_id"],
        "clientSecret": response["client_secret"],
        "connectUrl": response["connect_url"],
        "voiceMode": response["voice_mode"],
        "showTranscript": response["show_transcript"],
    }


@router.post("/realtime/{session_id}/end")
@limiter.limit("10/minute")
async def end_realtime_session(
    session_id: str,
    payload: RealtimeSessionEndRequest,
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
):
    """Finalize a browser-owned WebRTC realtime interview session."""
    await asyncio.to_thread(
        finalize_realtime_session,
        session_id=session_id,
        duration_seconds=payload.duration_seconds,
        external_auth_id=current_user.user_id,
    )
    return {"ok": True, "session_id": session_id}


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
        await relay_realtime_websocket(
            websocket,
            auth_external_id=current_user.user_id,
            auth_email=current_user.email,
        )
    finally:
        await release_ws_slot(client_ip)
