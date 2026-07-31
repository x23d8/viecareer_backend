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
from schemas.realtime import RealtimeSessionEndRequest, RealtimeSessionMetadata
from services.interview_attempt_service import get_interview_attempt_status
from services.realtime_service import (
    create_webrtc_start_payload,
    finalize_realtime_session,
    relay_realtime_websocket,
)
from services.realtime_transcript_service import (
    create_live_interview_session,
    get_live_interview_duration_limit,
)

logger = logging.getLogger("viecareer")

router = APIRouter(tags=["realtime"])


@router.get("/realtime-token")
@limiter.limit("5/minute")
async def get_realtime_token(
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
):
    """Reject the legacy token-only flow because it cannot meter attempts."""
    raise HTTPException(
        status_code=410,
        detail={
            "code": "REALTIME_START_REQUIRED",
            "message": "Use POST /realtime/start to create a metered interview session.",
        },
    )


@router.get("/interview-attempts")
@limiter.limit("60/minute")
async def interview_attempts(
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
):
    result = await get_interview_attempt_status(external_auth_id=current_user.user_id)
    return {"data": result.payload()}


@router.post("/realtime/start")
@limiter.limit("5/minute")
async def start_realtime_session(
    request: Request,
    payload: RealtimeSessionMetadata,
    current_user: ClerkUser = Depends(require_auth),
):
    """Create a persisted, attempt-metered OpenAI Realtime WebRTC session."""
    if payload.external_auth_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="Realtime metadata user does not match the authenticated user")

    verified_request_email(current_user, str(payload.email))
    metadata = payload.model_dump(mode="json")

    await check_daily_quota(current_user.user_id)
    # Creating the persisted session consumes one attempt in the same database
    # transaction. The attempt is intentionally not refunded on early exit.
    session_id = await asyncio.to_thread(create_live_interview_session, metadata)
    if not session_id:
        raise HTTPException(status_code=500, detail="Could not persist realtime interview context")
    max_duration_seconds = await asyncio.to_thread(get_live_interview_duration_limit, session_id)
    if max_duration_seconds:
        metadata["duration_minutes"] = max_duration_seconds // 60
        if isinstance(metadata.get("blueprint"), dict):
            metadata["blueprint"]["durationMinutes"] = max_duration_seconds // 60

    response = await asyncio.to_thread(
        create_webrtc_start_payload,
        session_id=session_id,
        metadata=metadata,
        max_duration_seconds=max_duration_seconds,
    )
    attempts = await get_interview_attempt_status(external_auth_id=current_user.user_id)
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
            "duration_minutes": payload.duration_minutes,
            "attempts_remaining": attempts.remaining,
        },
    )
    return {
        **response,
        "realtimeSessionId": response["realtime_session_id"],
        "clientSecret": response["client_secret"],
        "connectUrl": response["connect_url"],
        "voiceMode": response["voice_mode"],
        "showTranscript": response["show_transcript"],
        "attempts": attempts.payload(),
    }


@router.post("/realtime/{session_id}/end")
@limiter.limit("10/minute")
async def end_realtime_session(
    session_id: str,
    payload: RealtimeSessionEndRequest,
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
):
    """Finalize a browser-owned WebRTC session without refunding its attempt."""
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
    """Relay a server-owned, authenticated OpenAI Realtime session."""
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
