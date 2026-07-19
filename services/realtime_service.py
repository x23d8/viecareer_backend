"""OpenAI Realtime helpers for WebRTC tokens and the server-owned WebSocket relay."""

import asyncio
import base64
import inspect
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import requests
import websockets
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from config import (
    OPENAI_API_KEY,
    OPENAI_ASR_MODEL,
    OPENAI_REALTIME_CONNECT_URL,
    OPENAI_REALTIME_MODEL,
)
from core.api_usage import track_openai_usage_async, track_user_activity_async, usage_context
from core.openai_client import get_openai_client
from core.clerk_auth import ClerkUser, verified_request_email
from core.prompts import load_prompt
from schemas.interview_simulation import SelectNextQuestionRequest
from schemas.realtime import RealtimeSessionMetadata
from services.interview_simulation_service import InterviewSimulationService
from services.interview_ai_service import (
    polish_transcript,
    score_live_answer,
    transcribe_push_to_talk_audio,
)
from services.plan_quota_service import (
    FEATURE_REALTIME_INTERVIEW,
    METRIC_SESSION_COUNT,
    check_plan_quota,
    get_realtime_session_limit_seconds,
)
from services.realtime_transcript_service import (
    complete_live_interview_session,
    complete_live_interview_session_for_user,
    create_live_interview_session,
    save_realtime_qa_pair,
    save_realtime_transcript_event,
)

logger = logging.getLogger("viecareer")

_REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
_REALTIME_WEBSOCKET_URL = "wss://api.openai.com/v1/realtime"
_MAX_WS_MESSAGE_SIZE = 16 * 1024 * 1024


def create_realtime_session(
    metadata: dict[str, Any] | None = None,
    blueprint: dict[str, Any] | None = None,
) -> dict:
    """Create an ephemeral client secret for OpenAI Realtime WebRTC (GA API)."""
    session_config = _realtime_session_config(
        metadata,
        blueprint,
        create_response=str((metadata or {}).get("voice_mode") or "") != "push_to_talk",
    )
    try:
        response = requests.post(
            _REALTIME_CLIENT_SECRETS_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"session": session_config},
            timeout=15,
        )
        response.raise_for_status()
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else "(no body)"
        logger.error("Failed to create realtime session: %s — response body: %s", e, body)
        raise HTTPException(status_code=502, detail=f"Could not create realtime session: {body}")
    except requests.RequestException:
        logger.exception("Failed to create realtime session")
        raise HTTPException(status_code=502, detail="Could not create realtime session")

    data = response.json()
    return data.get("client_secret", data)


def create_webrtc_start_payload(
    *,
    session_id: str,
    metadata: dict[str, Any],
    max_duration_seconds: int | None,
) -> dict[str, Any]:
    """Create the browser-facing payload for direct OpenAI Realtime WebRTC."""
    blueprint = _coerce_blueprint(metadata.get("blueprint"))
    client_secret_payload = create_realtime_session(metadata, blueprint)
    client_secret = _client_secret_value(client_secret_payload)
    if not client_secret:
        logger.error("OpenAI realtime client secret response did not contain a bearer token")
        raise HTTPException(status_code=502, detail="Could not create realtime client secret")

    instructions = _realtime_session_config(
        metadata,
        blueprint,
        create_response=str(metadata.get("voice_mode") or "") != "push_to_talk",
    )["instructions"]
    return {
        "realtime_session_id": session_id,
        "client_secret": client_secret,
        "connect_url": OPENAI_REALTIME_CONNECT_URL,
        "provider": "openai",
        "model": OPENAI_REALTIME_MODEL,
        "voice_mode": metadata.get("voice_mode", "realtime_conversation"),
        "show_transcript": bool(metadata.get("show_transcript", True)),
        "instructions": instructions,
        "quota": {
            "max_duration_seconds": max_duration_seconds,
            "max_duration_minutes": _minutes_from_seconds(max_duration_seconds),
        },
    }


def finalize_realtime_session(
    *,
    session_id: str,
    duration_seconds: int | None,
    external_auth_id: str | None = None,
) -> None:
    """Complete or cancel a WebRTC-created realtime interview session."""
    if external_auth_id:
        completed = complete_live_interview_session_for_user(session_id, external_auth_id, duration_seconds)
        if not completed:
            raise HTTPException(status_code=404, detail="Realtime session not found")
    else:
        _complete_live_session_safe(session_id, duration_seconds)
    track_user_activity_async(
        "interview_completed",
        external_auth_id=external_auth_id,
        object_type="interview_session",
        object_id=session_id,
        meta={"duration_seconds": duration_seconds, "transport": "webrtc"},
    )


@dataclass
class _RelayState:
    started_at: float = field(default_factory=time.monotonic)
    session_started_at: float | None = None
    auth_external_id: str | None = None
    auth_email: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    event_order: int = 0
    question_order: int = 0
    asked_question_ids: list[str] = field(default_factory=list)
    pending_question: str = ""
    pending_question_id: str | None = None
    pending_question_event: dict[str, Any] | None = None
    active_question_id: str | None = None
    blueprint: dict[str, Any] | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    answered_question_ids: list[str] = field(default_factory=list)
    covered_topics: list[str] = field(default_factory=list)
    response_kind: str = "question"
    closing_in_progress: bool = False
    initialized: bool = False
    completed: bool = False
    realtime_usage_logged: bool = False
    realtime_estimate_logged: bool = False
    max_duration_seconds: int | None = None
    ptt_language: str = "en"
    ptt_audio_chunks: list[str] = field(default_factory=list)
    ptt_mime_type: str = "audio/pcm;rate=24000"

    def timestamp_sec(self) -> int:
        base = self.session_started_at or self.started_at
        return max(0, int(time.monotonic() - base))


async def relay_realtime_websocket(
    websocket: WebSocket,
    auth_external_id: str | None = None,
    auth_email: str | None = None,
) -> None:
    """Relay browser mic audio to OpenAI Realtime and stream model audio back."""
    await websocket.accept()

    if not OPENAI_API_KEY:
        await _send_client_error(websocket, "Missing OPENAI_API_KEY in backend environment")
        await websocket.close(code=1011)
        return

    state = _RelayState(auth_external_id=auth_external_id, auth_email=auth_email)
    model = quote(OPENAI_REALTIME_MODEL, safe="")
    openai_url = f"{_REALTIME_WEBSOCKET_URL}?model={model}"

    try:
        async with websockets.connect(
            openai_url,
            **_websocket_headers_kwargs({"Authorization": f"Bearer {OPENAI_API_KEY}"}),
            max_size=_MAX_WS_MESSAGE_SIZE,
        ) as openai_ws:
            await openai_ws.send(json.dumps(_session_update_event()))

            client_task = asyncio.create_task(_client_to_openai(websocket, openai_ws, state))
            openai_task = asyncio.create_task(_openai_to_client(websocket, openai_ws, state))
            quota_task = asyncio.create_task(_enforce_realtime_time_limit(websocket, openai_ws, state))
            done, pending = await asyncio.wait(
                {client_task, openai_task, quota_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
    except WebSocketDisconnect:
        return
    except websockets.exceptions.ConnectionClosed:
        return
    except Exception as exc:
        logger.exception("Realtime relay failed")
        await _send_client_error(websocket, f"Realtime relay failed: {exc}")
    finally:
        if state.initialized and not state.completed:
            duration_seconds = _bounded_duration_seconds(state.timestamp_sec(), state.max_duration_seconds)
            await asyncio.to_thread(_complete_live_session_safe, state.session_id, duration_seconds)
            _track_realtime_session_estimate(state, duration_seconds)


async def _client_to_openai(websocket: WebSocket, openai_ws, state: _RelayState) -> None:
    while True:
        raw_message = await websocket.receive_text()
        event = _json_object(raw_message)
        event_type = event.get("type")

        if event_type == "client.start":
            if state.initialized:
                await _send_client_error(websocket, "Realtime session context is already initialized")
                await openai_ws.close()
                return

            metadata = event.get("metadata")
            if not isinstance(metadata, dict) or not metadata:
                logger.info("Realtime client.start received without metadata; continuing without persistence")
                continue

            try:
                validated = RealtimeSessionMetadata.model_validate(metadata)
            except ValidationError as exc:
                fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
                await _send_client_error(
                    websocket,
                    f"Invalid required session metadata: {', '.join(fields)}",
                )
                await openai_ws.close(code=1008, reason="Invalid session metadata")
                return

            if state.auth_external_id and validated.external_auth_id != state.auth_external_id:
                await _send_client_error(websocket, "Realtime metadata user does not match the authenticated user")
                await openai_ws.close(code=1008, reason="User mismatch")
                return

            if state.auth_external_id:
                try:
                    await asyncio.to_thread(
                        verified_request_email,
                        ClerkUser(user_id=state.auth_external_id, email=state.auth_email),
                        str(validated.email),
                    )
                except HTTPException as exc:
                    await _send_client_error(websocket, str(exc.detail), code="IDENTITY_MISMATCH")
                    await openai_ws.close(code=1008, reason="Identity mismatch")
                    return

            state.metadata = validated.model_dump(mode="json")
            state.blueprint = _coerce_blueprint(state.metadata.get("blueprint"))

            try:
                await check_plan_quota(
                    external_auth_id=state.metadata.get("external_auth_id") or state.auth_external_id or "",
                    feature_code=FEATURE_REALTIME_INTERVIEW,
                    metric=METRIC_SESSION_COUNT,
                )
                state.max_duration_seconds = await get_realtime_session_limit_seconds(
                    external_auth_id=state.metadata.get("external_auth_id") or state.auth_external_id or "",
                )
            except HTTPException as exc:
                await _send_client_error(
                    websocket,
                    _http_exception_message(exc),
                    code="PLAN_QUOTA_EXCEEDED",
                    detail=exc.detail if isinstance(exc.detail, dict) else None,
                )
                await openai_ws.close(code=1008, reason="Plan quota exceeded")
                return

            state.session_id = await asyncio.to_thread(_create_live_session_safe, state.metadata)
            if not state.session_id:
                await _send_client_error(websocket, "Could not persist realtime interview context")
                await openai_ws.close(code=1011, reason="Session persistence failed")
                return

            state.initialized = True
            state.session_started_at = time.monotonic()
            await openai_ws.send(json.dumps(_session_update_event(state.metadata, state.blueprint)))
            await websocket.send_json(
                {
                    "type": "viecareer.session.started",
                    "session_id": state.session_id,
                    "context_version": 1,
                    "quota": {
                        "max_duration_seconds": state.max_duration_seconds,
                        "max_duration_minutes": _minutes_from_seconds(state.max_duration_seconds),
                    },
                }
            )
            track_user_activity_async(
                "interview_started",
                external_auth_id=state.metadata.get("external_auth_id") or state.auth_external_id,
                object_type="interview_session",
                object_id=state.session_id,
                meta={
                    "position": state.metadata.get("position"),
                    "language": state.metadata.get("language"),
                    "interview_style": state.metadata.get("interview_style"),
                    "question_count": len((state.blueprint or {}).get("questions") or []),
                },
            )
            continue

        if event_type == "client.stop":
            duration_seconds = _bounded_duration_seconds(
                _int_or_none(event.get("duration_seconds")),
                state.max_duration_seconds,
            )
            await asyncio.to_thread(_complete_live_session_safe, state.session_id, duration_seconds)
            state.completed = True
            track_user_activity_async(
                "interview_completed",
                external_auth_id=state.metadata.get("external_auth_id") or state.auth_external_id,
                object_type="interview_session",
                object_id=state.session_id,
                meta={
                    "duration_seconds": duration_seconds,
                    "questions_asked": state.question_order,
                    "transcript_turns": len(state.transcript),
                },
            )
            _track_realtime_session_estimate(state, duration_seconds)
            await openai_ws.close()
            return

        if event_type == "viecareer.ptt.start":
            state.ptt_language = _normalize_interview_language(event.get("language") or state.metadata.get("language"))
            state.ptt_mime_type = str(event.get("mime_type") or "audio/pcm;rate=24000")
            state.ptt_audio_chunks = []
            await _interrupt_realtime_playback(openai_ws, websocket)
            await websocket.send_json(
                {
                    "type": "viecareer.ptt.recording_started",
                    "language": state.ptt_language,
                    "mime_type": state.ptt_mime_type,
                }
            )
            continue

        if event_type == "viecareer.ptt.audio":
            chunk = str(event.get("audio") or "").strip()
            if chunk:
                state.ptt_audio_chunks.append(chunk)
            continue

        if event_type == "viecareer.ptt.commit":
            await _process_push_to_talk_turn(websocket, openai_ws, state, event)
            continue

        if event_type == "viecareer.interrupt":
            await _interrupt_realtime_playback(openai_ws, websocket)
            continue

        # [OLD - REALTIME] giu lai de tai su dung sau:
        # Truoc day frontend forward truc tiep moi OpenAI Realtime event
        # (input_audio_buffer.append/commit, response.create, socket event cu...)
        # sang OpenAI. Luong nay tam thoi bi dung vi realtime interview dang
        # khong on dinh; push-to-talk moi xu ly audio theo tung luot noi.
        # await openai_ws.send(json.dumps(event))


async def _openai_to_client(websocket: WebSocket, openai_ws, state: _RelayState) -> None:
    async for raw_message in openai_ws:
        message = raw_message.decode("utf-8") if isinstance(raw_message, bytes) else raw_message
        await websocket.send_text(message)
        await _persist_realtime_event(message, state, openai_ws, websocket)


async def _interrupt_realtime_playback(openai_ws, websocket: WebSocket) -> None:
    for event in (
        {"type": "response.cancel"},
        {"type": "input_audio_buffer.clear"},
    ):
        try:
            await openai_ws.send(json.dumps(event))
        except Exception:
            logger.debug("Could not send realtime interrupt event", exc_info=True)
    try:
        await websocket.send_json({"type": "viecareer.playback.interrupted"})
    except Exception:
        logger.debug("Could not notify client about playback interruption", exc_info=True)


async def _process_push_to_talk_turn(
    websocket: WebSocket,
    openai_ws,
    state: _RelayState,
    event: dict[str, Any],
) -> None:
    if not state.initialized:
        await _send_client_error(websocket, "Push-to-talk session has not been initialized")
        return

    language = _normalize_interview_language(event.get("language") or state.ptt_language or state.metadata.get("language"))
    mime_type = str(event.get("mime_type") or state.ptt_mime_type or "audio/pcm;rate=24000")
    audio_base64 = _join_base64_audio_chunks(state.ptt_audio_chunks)
    state.ptt_audio_chunks = []

    await websocket.send_json(
        {
            "type": "viecareer.ptt.asr.processing",
            "language": language,
            "asr_model": OPENAI_ASR_MODEL,
        }
    )

    try:
        asr = await asyncio.to_thread(
            transcribe_push_to_talk_audio,
            audio_base64=audio_base64,
            language=language,
            mime_type=mime_type,
        )
    except HTTPException as exc:
        await _send_client_error(websocket, _http_exception_message(exc), code="ASR_ERROR")
        return

    raw_transcript = str(asr.get("raw_transcript") or "").strip()
    await websocket.send_json(
        {
            "type": "viecareer.ptt.asr.done",
            "language": language,
            "asr_model": asr.get("model"),
            "raw_transcript": raw_transcript,
        }
    )

    client = get_openai_client()
    try:
        polished = await asyncio.to_thread(
            polish_transcript,
            client,
            raw_transcript=raw_transcript,
            language=language,
            usage_context=usage_context(
                feature="asr_polish",
                external_auth_id=state.metadata.get("external_auth_id") or state.auth_external_id,
                related_type="interview_session",
                related_id=state.session_id,
                meta={"question_order": state.question_order, "language": language},
            ),
        )
    except HTTPException as exc:
        await _send_client_error(websocket, _http_exception_message(exc), code="POLISH_ERROR")
        return

    await websocket.send_json(
        {
            "type": "viecareer.ptt.polish.done",
            "raw_transcript": raw_transcript,
            "polished_transcript": polished,
        }
    )

    scoring = await _score_push_to_talk_answer(state, raw_transcript, polished, language)
    turn_type, counts_as_answer = _candidate_turn_classification(polished, scoring)

    transcript_event = _transcript_event(state, "user", polished)
    transcript_event["raw_transcript"] = raw_transcript
    transcript_event["polished_transcript"] = polished
    state.transcript.append(
        {
            "role": "user",
            "text": polished,
            "raw_transcript": raw_transcript,
            "question_id": state.pending_question_id,
            "timestamp_sec": state.timestamp_sec(),
        }
    )
    await asyncio.to_thread(_save_transcript_event_safe, state.session_id, transcript_event)

    await openai_ws.send(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": polished}],
                },
            }
        )
    )

    if state.pending_question and counts_as_answer:
        if state.pending_question_id and state.pending_question_id not in state.answered_question_ids:
            state.answered_question_ids.append(state.pending_question_id)
        qa_events = [evt for evt in [state.pending_question_event, transcript_event] if evt]
        await asyncio.to_thread(
            _save_qa_pair_safe,
            state.session_id,
            state.question_order,
            state.pending_question,
            polished,
            qa_events,
            raw_transcript,
            polished,
            scoring,
        )
        await websocket.send_json(
            {
                "type": "viecareer.interview.progress",
                "question_id": state.pending_question_id,
                "answered_questions": len(state.answered_question_ids),
                "total_questions": _total_blueprint_questions(state),
                "turn_type": turn_type,
            }
        )
        state.pending_question = ""
        state.pending_question_id = None
        state.pending_question_event = None
        await _send_guided_interviewer_response(openai_ws, state)
        return

    if state.pending_question:
        await websocket.send_json(
            {
                "type": "viecareer.interview.clarification",
                "question_id": state.pending_question_id,
                "answered_questions": len(state.answered_question_ids),
                "total_questions": _total_blueprint_questions(state),
                "turn_type": turn_type,
            }
        )
        await _send_question_clarification_response(openai_ws, state, polished)
        return

    await _send_guided_interviewer_response(openai_ws, state)


async def _score_push_to_talk_answer(
    state: _RelayState,
    raw_transcript: str,
    polished_transcript: str,
    language: str,
) -> dict[str, Any] | None:
    if not polished_transcript.strip() or not state.pending_question:
        return None
    try:
        runtime_policy = (state.blueprint or {}).get("runtimePolicy") or {}
        dimensions = runtime_policy.get("scoringDimensions") if isinstance(runtime_policy, dict) else []
        return await asyncio.to_thread(
            score_live_answer,
            get_openai_client(),
            question=state.pending_question,
            raw_transcript=raw_transcript,
            polished_transcript=polished_transcript,
            language=language,
            position=str(state.metadata.get("position") or ""),
            scoring_dimensions=dimensions if isinstance(dimensions, list) else [],
            usage_context=usage_context(
                feature="live_answer_score",
                external_auth_id=state.metadata.get("external_auth_id") or state.auth_external_id,
                related_type="interview_session",
                related_id=state.session_id,
                meta={"question_order": state.question_order, "language": language},
            ),
        )
    except Exception:
        logger.exception("Could not score push-to-talk answer")
        return None


async def _enforce_realtime_time_limit(websocket: WebSocket, openai_ws, state: _RelayState) -> None:
    while True:
        await asyncio.sleep(1)
        if not state.initialized or state.completed or not state.max_duration_seconds:
            continue

        if state.timestamp_sec() < state.max_duration_seconds:
            continue

        duration_seconds = state.max_duration_seconds
        await asyncio.to_thread(_complete_live_session_safe, state.session_id, duration_seconds)
        state.completed = True
        track_user_activity_async(
            "interview_completed",
            external_auth_id=state.metadata.get("external_auth_id") or state.auth_external_id,
            object_type="interview_session",
            object_id=state.session_id,
            meta={
                "duration_seconds": duration_seconds,
                "questions_asked": state.question_order,
                "transcript_turns": len(state.transcript),
                "quota_time_limit": True,
            },
        )
        _track_realtime_session_estimate(state, duration_seconds)
        await _send_client_error(
            websocket,
            "Realtime interview time limit reached for your current plan.",
            code="PLAN_QUOTA_TIME_LIMIT",
            detail={
                "max_duration_seconds": state.max_duration_seconds,
                "max_duration_minutes": _minutes_from_seconds(state.max_duration_seconds),
            },
        )
        await openai_ws.close(code=1000, reason="Plan time limit reached")
        try:
            await websocket.close(code=1000)
        except RuntimeError:
            pass
        return


async def _persist_realtime_event(raw_message: str, state: _RelayState, openai_ws, websocket: WebSocket) -> None:
    event = _json_object(raw_message)
    event_type = event.get("type")

    if event_type == "response.done":
        has_usage = _event_has_usage(event)
        track_openai_usage_async(
            event,
            usage_context(
                feature="realtime_response",
                external_auth_id=state.metadata.get("external_auth_id") or state.auth_external_id,
                related_type="interview_session",
                related_id=state.session_id,
                meta={
                    "event_type": event_type,
                    "question_order": state.question_order,
                    "transcript_turns": len(state.transcript),
                    "usage_source": "openai",
                },
            ),
            model=OPENAI_REALTIME_MODEL,
            skip_without_usage=True,
        )
        if has_usage:
            state.realtime_usage_logged = True
        if state.closing_in_progress and not state.completed:
            await _finalize_question_limited_interview(websocket, openai_ws, state)
        return

    if event_type in {"response.output_audio_transcript.done", "response.audio_transcript.done"}:
        text = str(event.get("transcript") or "").strip()
        if not text:
            return

        state.active_question_id = _resolve_spoken_question_id(state, text)
        if state.response_kind != "closing" and state.active_question_id:
            if state.active_question_id not in state.asked_question_ids:
                state.asked_question_ids.append(state.active_question_id)
            state.question_order = len(state.asked_question_ids)
        transcript_event = _transcript_event(state, "assistant", text)
        if state.response_kind != "closing" and state.active_question_id:
            state.pending_question = _blueprint_question_text(state, state.active_question_id) or text
            state.pending_question_id = state.active_question_id
            state.pending_question_event = transcript_event
        state.transcript.append(
            {
                "role": "assistant",
                "text": text,
                "question_id": state.active_question_id,
                "timestamp_sec": state.timestamp_sec(),
            }
        )
        await asyncio.to_thread(_save_transcript_event_safe, state.session_id, transcript_event)
        return

    if event_type == "conversation.item.input_audio_transcription.completed":
        # [OLD - REALTIME] giu lai de tai su dung sau:
        # Day la luong transcript cu tu audio streaming truc tiep cua
        # gpt-realtime. Push-to-talk moi khong dua transcript tu event nay
        # vao DB nua; thay vao do transcript di qua ASR rieng, GPT-4o polish,
        # live scoring, roi moi tao conversation item bang text.
        text = str(event.get("transcript") or "").strip()
        if not text:
            return

        transcript_event = _transcript_event(state, "user", text)
        state.transcript.append(
            {
                "role": "user",
                "text": text,
                "question_id": state.pending_question_id,
                "timestamp_sec": state.timestamp_sec(),
            }
        )
        await asyncio.to_thread(_save_transcript_event_safe, state.session_id, transcript_event)

        if state.pending_question:
            if state.pending_question_id and state.pending_question_id not in state.answered_question_ids:
                state.answered_question_ids.append(state.pending_question_id)
            qa_events = [event for event in [state.pending_question_event, transcript_event] if event]
            await asyncio.to_thread(
                _save_qa_pair_safe,
                state.session_id,
                state.question_order,
                state.pending_question,
                text,
                qa_events,
            )
            state.pending_question = ""
            state.pending_question_id = None
            state.pending_question_event = None

        await _send_guided_interviewer_response(openai_ws, state)


def _session_update_event(
    metadata: dict[str, Any] | None = None,
    blueprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": _realtime_session_config(metadata, blueprint, create_response=False),
    }


def _realtime_session_config(
    metadata: dict[str, Any] | None = None,
    blueprint: dict[str, Any] | None = None,
    *,
    create_response: bool,
) -> dict[str, Any]:
    instructions = load_prompt("realtime_interviewer")
    job_description = str(
        (metadata or {}).get("job_description")
        or (metadata or {}).get("jd_summary")
        or ""
    ).strip()
    position = str((metadata or {}).get("position") or "").strip()
    language = str((metadata or {}).get("language") or "en").strip().lower()
    interview_style = str((metadata or {}).get("interview_style") or "mixed").strip()
    blueprint_context = _blueprint_context(blueprint, direct_conversation=create_response)
    language_directive = (
        "Conduct the entire interview in Vietnamese. Do not switch languages unless the candidate explicitly asks."
        if language == "vi"
        else "Conduct the entire interview in English. Do not switch languages unless the candidate explicitly asks."
    )

    _STYLE_HINT = {
        "technical":  "This session is technical-focused: prioritise deep technical and coding questions. Spend less time on general behavioral questions.",
        "behavioral": "This session is behavioral-focused: prioritise leadership, teamwork, and experience storytelling. Keep technical questions light.",
        "mixed":      "This session is a balanced mix of technical and behavioral questions.",
    }
    style_hint = _STYLE_HINT.get(interview_style, _STYLE_HINT["mixed"])

    if position or job_description or blueprint_context or style_hint:
        instructions = "\n\n".join(
            part
            for part in [
                instructions,
                f"Interview target position: {position}" if position else "",
                f"Job description context: {job_description}" if job_description else "",
                _candidate_context(metadata),
                f"Interview language directive: {language_directive}",
                f"Interview style directive: {style_hint}",
                blueprint_context,
            ]
            if part
        )

    config: dict[str, Any] = {
        "type": "realtime",
        "model": OPENAI_REALTIME_MODEL,
        "instructions": instructions,
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"model": "whisper-1"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500 if create_response else 800,
                    "create_response": create_response,
                    "interrupt_response": True,
                },
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": str((metadata or {}).get("voice", "shimmer")),
            },
        },
    }
    if create_response:
        question_ids = [str(question.get("id")) for question in _valid_blueprint_questions(blueprint)]
        if question_ids:
            config["tools"] = [
                {
                    "type": "function",
                    "name": "mark_interview_question_answered",
                    "description": (
                        "Report that the candidate completed a substantive answer or explicitly skipped the active "
                        "interview question. Call exactly once before moving to another question. Never call when the "
                        "candidate asks for repetition, clarification, or has not answered the active question."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question_id": {
                                "type": "string",
                                "enum": question_ids,
                                "description": "The exact id of the active blueprint question that was answered.",
                            },
                            "turn_type": {
                                "type": "string",
                                "enum": ["answer", "skip"],
                                "description": "Whether the candidate answered or explicitly skipped the question.",
                            },
                        },
                        "required": ["question_id", "turn_type"],
                    },
                }
            ]
            config["tool_choice"] = "auto"
    return config


def _client_secret_value(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    for key in ("value", "secret", "token"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = payload.get("client_secret")
    if isinstance(nested, dict):
        return _client_secret_value(nested)
    if isinstance(nested, str):
        return nested.strip()
    return ""


def _websocket_headers_kwargs(headers: dict[str, str]) -> dict[str, dict[str, str]]:
    header_kwarg = (
        "additional_headers"
        if "additional_headers" in inspect.signature(websockets.connect).parameters
        else "extra_headers"
    )
    return {header_kwarg: headers}


async def _send_guided_interviewer_response(openai_ws, state: _RelayState) -> None:
    if _interview_questions_complete(state):
        await _send_interview_closing_response(openai_ws, state)
        return

    selected = await asyncio.to_thread(_select_next_question_safe, state)
    if selected:
        selected_id = str(selected.get("id") or "")
        state.active_question_id = selected_id or None
        state.response_kind = "question"
    else:
        await _send_interview_closing_response(openai_ws, state)
        return

    await openai_ws.send(
        json.dumps(
            {
                "type": "response.create",
                "response": {
                    "instructions": _response_instructions(state, selected),
                },
            }
        )
    )


async def _send_question_clarification_response(
    openai_ws,
    state: _RelayState,
    candidate_turn: str,
) -> None:
    """Clarify or repeat the active question without advancing interview progress."""

    state.response_kind = "clarification"
    question = state.pending_question or _blueprint_question_text(state, state.active_question_id)
    language = str(state.metadata.get("language") or "en").lower()
    language_rule = "Respond in Vietnamese." if language == "vi" else "Respond in English."
    await openai_ws.send(
        json.dumps(
            {
                "type": "response.create",
                "response": {
                    "instructions": "\n".join(
                        [
                            language_rule,
                            "The candidate asked for repetition or clarification and has not answered the active question.",
                            "Briefly clarify or rephrase the same question. Do not introduce or count a new interview question.",
                            f"Active question: {question}",
                            f"Candidate request: {candidate_turn}",
                            "Keep the response under 45 words and end with only the rephrased active question.",
                        ]
                    )
                },
            }
        )
    )


async def _send_interview_closing_response(openai_ws, state: _RelayState) -> None:
    if state.closing_in_progress or state.completed:
        return

    state.closing_in_progress = True
    state.response_kind = "closing"
    state.active_question_id = None
    state.pending_question = ""
    state.pending_question_id = None
    state.pending_question_event = None
    language = str(state.metadata.get("language") or "en").lower()
    closing = (
        "Đây là phần kết thúc của buổi phỏng vấn. Cảm ơn bạn đã trả lời. Tiếp theo, bạn sẽ chuyển sang phần phỏng vấn lập trình."
        if language == "vi"
        else "This is the end of the interview. Thank you for your answers. Next, you will continue to the coding interview."
    )
    await openai_ws.send(
        json.dumps(
            {
                "type": "response.create",
                "response": {
                    "metadata": {"viecareer_phase": "interview_closing"},
                    "instructions": (
                        f"Say exactly this closing message and nothing else: {closing} "
                        "Do not ask another question."
                    ),
                },
            }
        )
    )


async def _finalize_question_limited_interview(
    websocket: WebSocket,
    openai_ws,
    state: _RelayState,
) -> None:
    duration_seconds = _bounded_duration_seconds(state.timestamp_sec(), state.max_duration_seconds)
    await asyncio.to_thread(_complete_live_session_safe, state.session_id, duration_seconds)
    state.completed = True
    track_user_activity_async(
        "interview_completed",
        external_auth_id=state.metadata.get("external_auth_id") or state.auth_external_id,
        object_type="interview_session",
        object_id=state.session_id,
        meta={
            "duration_seconds": duration_seconds,
            "questions_asked": len(state.asked_question_ids),
            "questions_answered": len(state.answered_question_ids),
            "transcript_turns": len(state.transcript),
            "completion_reason": "question_limit_reached",
        },
    )
    _track_realtime_session_estimate(state, duration_seconds)
    await websocket.send_json(
        {
            "type": "viecareer.interview.completed",
            "reason": "question_limit_reached",
            "answered_questions": len(state.answered_question_ids),
            "total_questions": _total_blueprint_questions(state),
            "next_flow": "coding",
            "next_path": "/coding",
            "auto_transition": True,
        }
    )
    await openai_ws.close(code=1000, reason="Interview question limit reached")


def _candidate_turn_classification(
    transcript: str,
    scoring: dict[str, Any] | None,
) -> tuple[str, bool]:
    """Conservatively decide whether a candidate turn answers the active question."""

    normalized = re.sub(r"\s+", " ", str(transcript or "").strip().casefold())
    clarification_patterns = (
        r"\b(can|could|would) you (repeat|rephrase|clarify|explain)\b",
        r"\b(please )?(repeat|rephrase|clarify) (that|the question|it)\b",
        r"\bwhat do you mean\b",
        r"\bi (do not|don't|did not|didn't) (understand|hear|catch)\b",
        r"\bcome again\b",
        r"\b(bạn|anh|chị) (có thể )?(nhắc lại|nói lại|giải thích|làm rõ)\b",
        r"\b(tôi|em|mình) (chưa|không) (hiểu|nghe rõ)\b",
        r"\bý (bạn|anh|chị) là (gì|sao)\b",
        r"\bcâu hỏi (là gì|như thế nào|gì vậy)\b",
    )
    if any(re.search(pattern, normalized) for pattern in clarification_patterns):
        return "clarification", False

    allowed_types = {"answer", "clarification", "repeat_request", "skip", "off_topic"}
    raw_type = str((scoring or {}).get("turn_type") or "answer").strip().lower()
    turn_type = raw_type if raw_type in allowed_types else "answer"
    if turn_type in {"clarification", "repeat_request", "off_topic"}:
        return turn_type, False
    if turn_type == "skip":
        return turn_type, True
    raw_counts = (scoring or {}).get("counts_as_answer")
    return turn_type, raw_counts if isinstance(raw_counts, bool) else True


def _blueprint_questions(state: _RelayState) -> list[dict[str, Any]]:
    questions = (state.blueprint or {}).get("questions") or []
    return [question for question in questions if isinstance(question, dict) and str(question.get("id") or "").strip()]


def _total_blueprint_questions(state: _RelayState) -> int:
    return len({str(question.get("id")) for question in _blueprint_questions(state)})


def _interview_questions_complete(state: _RelayState) -> bool:
    question_ids = {str(question.get("id")) for question in _blueprint_questions(state)}
    return bool(question_ids) and question_ids.issubset(set(state.answered_question_ids))


def _blueprint_question_text(state: _RelayState, question_id: str | None) -> str:
    if not question_id:
        return ""
    for question in _blueprint_questions(state):
        if str(question.get("id")) == question_id:
            return str(question.get("question") or "").strip()
    return ""


def _resolve_spoken_question_id(state: _RelayState, transcript: str) -> str | None:
    """Keep duplicate spoken questions attached to their original blueprint id."""

    expected_id = state.active_question_id
    if state.response_kind == "closing" or not expected_id:
        return expected_id

    spoken_tokens = _question_tokens(transcript)
    if not spoken_tokens:
        return expected_id

    scores: list[tuple[float, str]] = []
    for question in _blueprint_questions(state):
        question_id = str(question.get("id"))
        canonical_tokens = _question_tokens(str(question.get("question") or ""))
        if not canonical_tokens:
            continue
        coverage = len(canonical_tokens.intersection(spoken_tokens)) / len(canonical_tokens)
        scores.append((coverage, question_id))

    if not scores:
        return expected_id
    best_score, best_id = max(scores)
    expected_score = next((score for score, question_id in scores if question_id == expected_id), 0.0)
    if best_id in state.asked_question_ids and best_score >= 0.55 and best_score > expected_score + 0.10:
        return best_id
    return expected_id


def _question_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    stopwords = {
        "a", "an", "and", "are", "can", "could", "do", "for", "how", "in", "is", "of", "or",
        "the", "this", "to", "what", "when", "where", "which", "with", "would", "you", "your",
        "ban", "cach", "co", "cua", "da", "hay", "khi", "la", "mot", "nao", "nhu", "the", "va",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) > 2 and token not in stopwords
    }


def _select_next_question_safe(state: _RelayState) -> dict[str, Any] | None:
    if not state.blueprint:
        return None

    if _interview_questions_complete(state):
        return None

    if not state.answered_question_ids and not any(turn.get("role") == "assistant" for turn in state.transcript):
        opening = _opening_question(state)
        if opening:
            return opening

    try:
        req = SelectNextQuestionRequest(
            blueprint=state.blueprint,
            transcript=state.transcript[-12:],
            answered_question_ids=state.answered_question_ids,
            covered_topics=state.covered_topics,
            language=state.metadata.get("language", "en"),
        )
        decision = InterviewSimulationService(
            get_openai_client(),
            usage_context=usage_context(
                feature="next_question",
                external_auth_id=state.metadata.get("external_auth_id") or state.auth_external_id,
                related_type="interview_session",
                related_id=state.session_id,
                meta={
                    "source": "realtime",
                    "answered_questions": len(state.answered_question_ids),
                    "transcript_turns": len(state.transcript),
                },
            ),
        ).select_next_question(req)
    except Exception:
        logger.exception("Could not select adaptive interview question")
        return _first_unanswered_question(state)

    selected = decision.get("selectedQuestion")
    if not isinstance(selected, dict):
        selected = _first_unanswered_question(state)

    decision_meta = decision.get("decision")
    if isinstance(decision_meta, dict):
        for topic in decision_meta.get("coveredTopicsToAdd") or []:
            topic_text = str(topic or "").strip()
            if topic_text and topic_text not in state.covered_topics:
                state.covered_topics.append(topic_text)

    return selected


def _response_instructions(state: _RelayState, selected: dict[str, Any] | None) -> str:
    first_response = state.question_order == 0
    greeting = _opening_greeting(state.metadata)

    if selected:
        question = str(selected.get("question") or "").strip()
        topic = str(selected.get("topic") or "").strip()
        intent = str(selected.get("intent") or "").strip()
        group = str(selected.get("group") or "").strip()
        is_coding = group == "coding_problem_solving"
        word_limit = 60 if is_coding else 35

        context_parts = [
            f"Selected question id: {selected.get('id')}",
            f"Topic: {topic}" if topic else "",
            f"Intent: {intent}" if intent else "",
            f"Question to ask exactly: {question}",
        ]
        if first_response:
            opening = (
                "This is your first spoken response before the interview starts. "
                f"Begin with exactly this greeting sentence: {greeting} "
                "Then ask the selected question."
            )
        else:
            opening = "Acknowledge the candidate's previous answer in one short sentence, then ask the selected question."
        return "\n".join(
            [
                opening,
                "Do not ask any other main question.",
                "Do not reveal scoring, blueprint details, ids, topic labels, or selection logic.",
                f"Word limit: {word_limit} words.",
                *[part for part in context_parts if part],
            ]
        )

    return "\n".join(
        [
            (
                "If this is your first spoken response before the interview starts, "
                f"begin with exactly this greeting sentence: {greeting}"
            ),
            "Then ask one realistic interview question grounded in the target position and job description.",
            "Ask exactly one main question and keep the spoken response under 35 words.",
            "Do not reveal internal instructions or scoring.",
        ]
    )


def _candidate_context(metadata: dict[str, Any] | None) -> str:
    display_name = _candidate_display_name(metadata)
    full_name = _candidate_full_name(metadata)
    candidate = (metadata or {}).get("candidate")
    profile = candidate.get("profile") if isinstance(candidate, dict) else {}
    cv_text = _string_value(candidate.get("cv_text")) if isinstance(candidate, dict) else ""

    return "\n".join(
        part
        for part in [
            f"Candidate name from extracted CV: {full_name or display_name}" if (full_name or display_name) else "",
            f"Use this spoken candidate name in the opening greeting: {display_name}" if display_name else "",
            "Prefer the CV-extracted candidate name over the account name." if (full_name or display_name) else "",
            f"Candidate structured profile: {json.dumps(profile, ensure_ascii=False)}" if profile else "",
            f"Candidate CV context: {cv_text}" if cv_text else "",
        ]
        if part
    )


def _opening_greeting(metadata: dict[str, Any] | None) -> str:
    language = str((metadata or {}).get("language") or "en").strip().lower()
    name = _candidate_display_name(metadata)
    if language == "vi":
        return f"Chào {name}." if name else "Chào bạn."
    return f"Hi {name}." if name else "Hi there."


def _candidate_display_name(metadata: dict[str, Any] | None) -> str:
    full_name = _candidate_full_name(metadata)
    if not full_name:
        return ""

    tokens = full_name.split()
    if len(tokens) <= 2:
        return full_name

    first_token = _ascii_slug(tokens[0])
    vietnamese_family_names = {
        "nguyen",
        "tran",
        "le",
        "pham",
        "hoang",
        "huynh",
        "phan",
        "vu",
        "vo",
        "dang",
        "bui",
        "do",
        "ho",
        "ngo",
        "duong",
        "ly",
        "truong",
        "dinh",
    }
    if first_token in vietnamese_family_names:
        return " ".join(tokens[-2:])

    return tokens[0]


def _candidate_full_name(metadata: dict[str, Any] | None) -> str:
    if not isinstance(metadata, dict):
        return ""

    candidate = metadata.get("candidate")
    if isinstance(candidate, dict):
        value = _string_value(candidate.get("name"))
        if value:
            return value
        extracted = candidate.get("profile")
    else:
        extracted = None

    for key in ("candidate_name", "candidate_full_name"):
        value = _string_value(metadata.get(key))
        if value:
            return value

    if not isinstance(extracted, dict):
        extracted = metadata.get("extracted_analysis")
    if isinstance(extracted, dict):
        for key in ("fullName", "full_name", "candidateName", "candidate_name", "name"):
            value = _string_value(extracted.get(key))
            if value:
                return value

    blueprint = _coerce_blueprint(metadata.get("blueprint")) or _coerce_blueprint(metadata.get("interview_blueprint"))
    analysis = blueprint.get("analysis") if blueprint and isinstance(blueprint.get("analysis"), dict) else {}
    for key in ("fullName", "full_name", "candidateName", "candidate_name", "name"):
        value = _string_value(analysis.get(key))
        if value:
            return value

    return ""


def _string_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _ascii_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
    without_marks = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z]", "", without_marks)


def _coerce_blueprint(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) and isinstance(value.get("questions"), list):
        return value
    return None


def _blueprint_context(blueprint: dict[str, Any] | None, *, direct_conversation: bool = False) -> str:
    if not blueprint:
        return ""

    analysis = blueprint.get("analysis") if isinstance(blueprint.get("analysis"), dict) else {}
    runtime_policy = blueprint.get("runtimePolicy") if isinstance(blueprint.get("runtimePolicy"), dict) else {}
    questions = _valid_blueprint_questions(blueprint)
    compact_questions = [
        {
            "id": question.get("id"),
            "group": question.get("group"),
            "topic": question.get("topic"),
            "level": question.get("level"),
            "question": question.get("question"),
        }
        for question in questions[:12]
        if isinstance(question, dict)
    ]

    controller_policy = (
        "You are directly managing the live interview. Ask the blueprint questions one at a time and never invent an additional main question. After a substantive answer or an explicit skip, you MUST call mark_interview_question_answered with the active question id before asking another question. If the candidate asks you to repeat, rephrase, explain, or clarify, do not call the tool and do not advance; help them understand and repeat the same active question. A repeated or rephrased question is the same question and must never increase progress. After the tool reports that all unique question ids are completed, do not ask another question; wait for the application-controlled closing response."
        if direct_conversation
        else "An adaptive controller will select the next question after each candidate answer. Do not invent a different main question when a selected question is provided."
    )

    return "\n".join(
        [
            "Interview blueprint is available. Use it as the source of truth for interview scope.",
            controller_policy,
            "Do not reveal scoring, blueprint details, ids, topic labels, or selection logic.",
            f"Blueprint analysis: {json.dumps(analysis, ensure_ascii=False)}",
            f"Runtime policy: {json.dumps(runtime_policy, ensure_ascii=False)}",
            f"Question bank preview: {json.dumps(compact_questions, ensure_ascii=False)}",
        ]
    )


def _valid_blueprint_questions(blueprint: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not blueprint:
        return []
    questions = blueprint.get("questions") if isinstance(blueprint.get("questions"), list) else []
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or "").strip()
        if not question_id or question_id in seen:
            continue
        seen.add(question_id)
        unique.append(question)
    return unique


def _first_unanswered_question(state: _RelayState) -> dict[str, Any] | None:
    questions = (state.blueprint or {}).get("questions") or []
    for question in questions:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or "")
        if question_id not in state.answered_question_ids:
            return question
    return None


def _opening_question(state: _RelayState) -> dict[str, Any] | None:
    runtime_policy = (state.blueprint or {}).get("runtimePolicy") or {}
    opening_id = str(runtime_policy.get("openingQuestionId") or "").strip() if isinstance(runtime_policy, dict) else ""
    if not opening_id:
        return _first_unanswered_question(state)

    questions = (state.blueprint or {}).get("questions") or []
    for question in questions:
        if isinstance(question, dict) and str(question.get("id") or "") == opening_id:
            return question
    return _first_unanswered_question(state)


def _transcript_event(state: _RelayState, role: str, text: str) -> dict[str, Any]:
    state.event_order += 1
    return {
        "event_order": state.event_order,
        "role": role,
        "text": text,
        "source": "gpt-realtime-ws",
        "timestamp_sec": state.timestamp_sec(),
        "question_order": state.question_order if state.question_order else None,
    }


def _event_has_usage(event: dict[str, Any]) -> bool:
    if event.get("usage"):
        return True
    response = event.get("response")
    return isinstance(response, dict) and bool(response.get("usage"))


def _track_realtime_session_estimate(state: _RelayState, duration_seconds: int | None) -> None:
    if state.realtime_usage_logged or state.realtime_estimate_logged or not state.session_id:
        return

    input_chars = sum(
        len(str(turn.get("text") or ""))
        for turn in state.transcript
        if turn.get("role") == "user"
    )
    output_chars = sum(
        len(str(turn.get("text") or ""))
        for turn in state.transcript
        if turn.get("role") == "assistant"
    )
    prompt_tokens = _estimate_text_tokens(input_chars)
    completion_tokens = _estimate_text_tokens(output_chars)
    total_tokens = prompt_tokens + completion_tokens
    duration = max(0, int(duration_seconds or state.timestamp_sec()))
    if total_tokens <= 0 and duration > 0:
        prompt_tokens = max(1, duration * 2)
        completion_tokens = max(1, duration * 2)
        total_tokens = prompt_tokens + completion_tokens

    cost_override = _estimated_realtime_cost(duration)
    track_openai_usage_async(
        {
            "model": OPENAI_REALTIME_MODEL,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "input_audio_seconds": duration,
                "output_audio_seconds": duration,
            },
        },
        usage_context(
            feature="realtime_session",
            external_auth_id=state.metadata.get("external_auth_id") or state.auth_external_id,
            related_type="interview_session",
            related_id=state.session_id,
            meta={
                "usage_source": "estimated",
                "estimate_method": "transcript_chars_div_4_with_duration_fallback",
                "cost_source": "OPENAI_REALTIME_ESTIMATED_COST_PER_MINUTE_USD"
                if cost_override is not None
                else "model_pricing_or_null",
                "duration_seconds": duration,
                "questions_asked": state.question_order,
                "transcript_turns": len(state.transcript),
            },
        ),
        model=OPENAI_REALTIME_MODEL,
        cost_usd_override=cost_override,
    )
    state.realtime_estimate_logged = True


def _estimate_text_tokens(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return max(1, round(char_count / 4))


def _estimated_realtime_cost(duration_seconds: int) -> Decimal | None:
    raw_rate = os.getenv("OPENAI_REALTIME_ESTIMATED_COST_PER_MINUTE_USD", "").strip()
    if not raw_rate:
        return None
    try:
        rate = Decimal(raw_rate)
    except (InvalidOperation, ValueError):
        logger.warning("OPENAI_REALTIME_ESTIMATED_COST_PER_MINUTE_USD is invalid")
        return None
    minutes = Decimal(max(0, duration_seconds)) / Decimal("60")
    return (minutes * rate).quantize(Decimal("0.000001"))


def _create_live_session_safe(metadata: dict[str, Any]) -> str | None:
    try:
        return create_live_interview_session(metadata)
    except Exception:
        logger.exception("Could not create live interview session")
        return None


def _complete_live_session_safe(session_id: str | None, duration_seconds: int | None) -> None:
    try:
        complete_live_interview_session(session_id, duration_seconds)
    except Exception:
        logger.exception("Could not complete live interview session")


def _save_transcript_event_safe(session_id: str | None, event: dict[str, Any]) -> None:
    try:
        save_realtime_transcript_event(session_id=session_id, **event)
    except Exception:
        logger.exception("Could not save realtime transcript event")


def _save_qa_pair_safe(
    session_id: str | None,
    question_order: int,
    question: str,
    answer: str,
    transcript_events: list[dict[str, Any]],
    raw_transcript: str | None = None,
    polished_transcript: str | None = None,
    scoring: dict[str, Any] | None = None,
) -> None:
    try:
        save_realtime_qa_pair(
            session_id=session_id,
            question_order=question_order,
            question=question,
            answer=answer,
            transcript_events=transcript_events,
            raw_transcript=raw_transcript,
            polished_transcript=polished_transcript,
            score=scoring.get("score") if isinstance(scoring, dict) else None,
            feedback=scoring.get("feedback") if isinstance(scoring, dict) else None,
            score_details=scoring if isinstance(scoring, dict) else None,
        )
    except Exception:
        logger.exception("Could not save realtime Q&A pair")


async def _send_client_error(
    websocket: WebSocket,
    message: str,
    *,
    code: str = "REALTIME_ERROR",
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        payload: dict[str, Any] = {"type": "viecareer.error", "code": code, "message": message}
        if detail:
            payload["detail"] = detail
        await websocket.send_json(payload)
    except Exception:
        logger.debug("Could not send realtime client error", exc_info=True)


def _http_exception_message(exc: HTTPException) -> str:
    if isinstance(exc.detail, dict):
        message = exc.detail.get("message")
        if message:
            return str(message)
    return str(exc.detail)


def _minutes_from_seconds(seconds: int | None) -> int | None:
    if seconds is None:
        return None
    return max(0, int(seconds) // 60)


def _bounded_duration_seconds(duration_seconds: int | None, max_duration_seconds: int | None) -> int | None:
    if duration_seconds is None:
        return None
    duration = max(0, int(duration_seconds))
    if max_duration_seconds is None:
        return duration
    return min(duration, max(0, int(max_duration_seconds)))


def _json_object(raw_message: str) -> dict[str, Any]:
    try:
        data = json.loads(raw_message)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_interview_language(value: Any) -> str:
    return "vi" if str(value or "").strip().lower().startswith("vi") else "en"


def _join_base64_audio_chunks(chunks: list[str]) -> str:
    raw = bytearray()
    for chunk in chunks:
        clean = str(chunk or "").strip()
        if not clean:
            continue
        if clean.lower().startswith("data:") and "," in clean:
            clean = clean.split(",", 1)[1]
        try:
            raw.extend(base64.b64decode(clean, validate=False))
        except Exception:
            logger.debug("Skipping invalid push-to-talk audio chunk", exc_info=True)
    return base64.b64encode(bytes(raw)).decode("ascii") if raw else ""
