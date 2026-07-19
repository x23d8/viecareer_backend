"""LLM and ASR helpers for push-to-talk and coding interview evaluation."""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
import wave
from typing import Any

from fastapi import HTTPException
from psycopg2.extras import Json

from config import OPENAI_ASR_MODEL, OPENAI_MODEL, OPENAI_POLISH_MODEL
from core.api_usage import UsageContext, track_openai_usage_async
from core.database import get_connection
from core.openai_client import get_openai_client
from core.prompts import load_prompt
from core.retry import openai_retry
from services.interview_report_service import _ensure_interview_report_schema
from services.user_service import upsert_auth_user

logger = logging.getLogger("viecareer")

@openai_retry
def _chat_completion(client, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float):
    return client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )


@openai_retry
def _audio_transcription(client, model: str, audio_file, language: str):
    audio_file.seek(0)
    return client.audio.transcriptions.create(
        model=model,
        file=audio_file,
        language=language,
    )


def transcribe_push_to_talk_audio(
    *,
    audio_base64: str,
    language: str,
    mime_type: str = "audio/pcm;rate=24000",
) -> dict[str, Any]:
    """Transcribe a completed push-to-talk utterance with the OpenAI ASR API."""
    clean_audio = _compact_base64(audio_base64)
    if not clean_audio:
        raise HTTPException(status_code=422, detail="No audio was provided for transcription")

    normalized_language = _language(language)
    model = OPENAI_ASR_MODEL
    audio_file = _openai_audio_file(clean_audio, mime_type)
    try:
        response = _audio_transcription(get_openai_client(), model, audio_file, normalized_language)
    except Exception as exc:
        logger.exception("OpenAI ASR request failed")
        raise HTTPException(status_code=503, detail="ASR API is unavailable") from exc

    transcript = str(getattr(response, "text", "") or "").strip()
    if not transcript:
        raise HTTPException(status_code=502, detail="ASR API returned an empty transcript")

    return {
        "language": normalized_language,
        "model": model,
        "raw_transcript": transcript,
        "asr_response": {"text": transcript},
    }


def polish_transcript(
    client,
    *,
    raw_transcript: str,
    language: str,
    usage_context: UsageContext | None = None,
) -> str:
    """Repair ASR noise without changing the candidate's meaning."""
    raw = str(raw_transcript or "").strip()
    if not raw:
        return ""

    prompt = f"""Language: {_language(language)}
Raw ASR transcript:
{raw}

Return only the polished transcript text."""
    response = _tracked_chat(
        client,
        model=OPENAI_POLISH_MODEL,
        system_prompt=load_prompt("asr_transcript_polisher"),
        user_prompt=prompt,
        max_tokens=800,
        temperature=0.1,
        usage_context=usage_context,
        feature_meta={"operation": "asr_polish", "language": _language(language)},
    )
    polished = str(response.choices[0].message.content or "").strip()
    return polished or raw


def score_live_answer(
    client,
    *,
    question: str,
    raw_transcript: str,
    polished_transcript: str,
    language: str,
    position: str = "",
    scoring_dimensions: list[str] | None = None,
    usage_context: UsageContext | None = None,
) -> dict[str, Any]:
    prompt = f"""Language: {_language(language)}
Position: {position or "Software Developer"}
Scoring dimensions: {json.dumps(scoring_dimensions or [], ensure_ascii=False)}

Question from interviewer:
{question}

Raw ASR transcript:
{raw_transcript}

Polished candidate answer:
{polished_transcript}

Return only the JSON object."""
    raw = _tracked_chat_text(
        client,
        model=OPENAI_MODEL,
        system_prompt=load_prompt("live_answer_scorer"),
        user_prompt=prompt,
        max_tokens=1200,
        temperature=0.2,
        usage_context=usage_context,
        feature_meta={"operation": "live_answer_score", "language": _language(language)},
    )
    parsed = _parse_json_object(raw)
    turn_type = str(parsed.get("turn_type") or "answer").strip().lower()
    if turn_type not in {"answer", "clarification", "repeat_request", "skip", "off_topic"}:
        turn_type = "answer"
    raw_counts_as_answer = parsed.get("counts_as_answer")
    counts_as_answer = (
        raw_counts_as_answer
        if isinstance(raw_counts_as_answer, bool)
        else turn_type in {"answer", "skip"}
    )
    return {
        "turn_type": turn_type,
        "counts_as_answer": counts_as_answer,
        "score": _score(parsed.get("score")),
        "feedback": str(parsed.get("feedback") or "").strip(),
        "strengths": _string_list(parsed.get("strengths")),
        "weaknesses": _string_list(parsed.get("weaknesses")),
        "dimensions": parsed.get("dimensions") if isinstance(parsed.get("dimensions"), dict) else {},
    }


def evaluate_code_interview(
    client,
    *,
    challenge: dict[str, Any],
    code: str,
    language: str,
    response_language: str = "en",
    usage_context: UsageContext | None = None,
) -> dict[str, Any]:
    feedback_language = "Vietnamese" if response_language == "vi" else "English"
    prompt = f"""Programming language: {language}
Feedback language: {feedback_language}

Coding challenge:
{json.dumps(challenge, ensure_ascii=False, indent=2)}

Candidate submission:
```{language}
{code}
```

Return only the JSON object."""
    raw = _tracked_chat_text(
        client,
        model=OPENAI_MODEL,
        system_prompt=load_prompt("code_interview_evaluator"),
        user_prompt=prompt,
        max_tokens=2500,
        temperature=0.2,
        usage_context=usage_context,
        feature_meta={
            "operation": "code_interview_evaluation",
            "language": language,
            "response_language": response_language,
        },
    )
    parsed = _parse_json_object(raw)
    return _normalize_code_evaluation(parsed)


def combine_final_report(
    client,
    *,
    live_interview: dict[str, Any],
    code_interview: dict[str, Any] | None,
    baseline_dimensions: dict[str, Any] | None = None,
    usage_context: UsageContext | None = None,
) -> dict[str, Any]:
    prompt = f"""Baseline dimensions:
{json.dumps(baseline_dimensions or {}, ensure_ascii=False, indent=2)}

Live interview:
{json.dumps(live_interview, ensure_ascii=False, indent=2)}

Code interview:
{json.dumps(code_interview or {}, ensure_ascii=False, indent=2)}

Return only the JSON object."""
    raw = _tracked_chat_text(
        client,
        model=OPENAI_MODEL,
        system_prompt=load_prompt("final_report_combiner"),
        user_prompt=prompt,
        max_tokens=2500,
        temperature=0.2,
        usage_context=usage_context,
        feature_meta={"operation": "final_report_combiner"},
    )
    return _parse_json_object(raw)


def save_code_interview_evaluation(
    *,
    external_auth_id: str,
    email: str,
    account_full_name: str,
    challenge: dict[str, Any],
    code: str,
    language: str,
    evaluation: dict[str, Any],
    avatar_url: str | None = None,
    auth_provider: str = "clerk",
    cv_jd_analysis_id: str | None = None,
    live_session_id: str | None = None,
    elapsed_seconds: int | None = None,
) -> dict[str, Any]:
    user = upsert_auth_user(
        external_auth_id=external_auth_id,
        email=email,
        full_name=account_full_name,
        avatar_url=avatar_url,
        auth_provider=auth_provider,
        identity_verified=True,
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            _ensure_interview_report_schema(cur)
            _ensure_code_interview_schema(cur)
            cur.execute(
                """
                insert into coding_interview_submissions (
                  user_id,
                  interview_session_id,
                  cv_jd_analysis_id,
                  challenge_json,
                  language,
                  submitted_code,
                  elapsed_seconds,
                  score,
                  summary,
                  feedback_json
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning *
                """,
                (
                    user["id"],
                    live_session_id or None,
                    cv_jd_analysis_id or None,
                    Json(challenge),
                    language,
                    code,
                    elapsed_seconds,
                    _score(evaluation.get("score")),
                    str(evaluation.get("summary") or ""),
                    Json(evaluation),
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return row


def _ensure_code_interview_schema(cur) -> None:
    cur.execute(
        """
        create table if not exists coding_interview_submissions (
          id uuid primary key default gen_random_uuid(),
          user_id uuid not null references users(id) on delete cascade,
          interview_session_id uuid null references interview_sessions(id) on delete set null,
          cv_jd_analysis_id uuid null references cv_jd_analyses(id) on delete set null,
          challenge_json jsonb not null default '{}'::jsonb,
          language varchar(40) not null,
          submitted_code text not null,
          elapsed_seconds int null,
          score int null check (score between 0 and 100),
          summary text null,
          feedback_json jsonb null,
          created_at timestamptz not null default now()
        )
        """
    )
    cur.execute(
        """
        create index if not exists idx_coding_submissions_user_created
        on coding_interview_submissions (user_id, created_at desc)
        """
    )


def _tracked_chat_text(
    client,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
    usage_context: UsageContext | None,
    feature_meta: dict[str, Any] | None = None,
) -> str:
    response = _tracked_chat(
        client,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        usage_context=usage_context,
        feature_meta=feature_meta,
    )
    return str(response.choices[0].message.content or "")


def _tracked_chat(
    client,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
    usage_context: UsageContext | None,
    feature_meta: dict[str, Any] | None = None,
):
    started_at = time.perf_counter()
    try:
        response = _chat_completion(
            client,
            model,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens,
            temperature,
        )
    except Exception as exc:
        track_openai_usage_async(
            None,
            usage_context,
            status="error",
            model=model,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            error_code=exc.__class__.__name__,
            meta=feature_meta,
        )
        raise HTTPException(status_code=503, detail="AI service is unavailable") from exc

    track_openai_usage_async(
        response,
        usage_context,
        model=model,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        meta=feature_meta,
    )
    return response


def _parse_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        raise HTTPException(status_code=502, detail="AI returned an empty response")
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise HTTPException(status_code=502, detail="AI response did not contain JSON")
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=502, detail="AI response must be a JSON object")
    return parsed


def _normalize_code_evaluation(parsed: dict[str, Any]) -> dict[str, Any]:
    subscores = parsed.get("subscores") if isinstance(parsed.get("subscores"), dict) else {}
    return {
        "score": _score(parsed.get("score")),
        "summary": str(parsed.get("summary") or "").strip(),
        "feedback": str(parsed.get("feedback") or "").strip(),
        "subscores": {
            "correctness": _score(subscores.get("correctness")),
            "complexity": _score(subscores.get("complexity")),
            "code_style": _score(subscores.get("code_style")),
            "edge_cases": _score(subscores.get("edge_cases")),
        },
        "complexity": parsed.get("complexity") if isinstance(parsed.get("complexity"), dict) else {},
        "strengths": _string_list(parsed.get("strengths")),
        "issues": _string_list(parsed.get("issues")),
        "suggestions": _string_list(parsed.get("suggestions")),
    }


def _compact_base64(value: str) -> str:
    raw = str(value or "").strip()
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    raw = re.sub(r"\s+", "", raw)
    try:
        base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Audio payload must be valid base64") from exc
    return raw


def _openai_audio_file(audio_base64: str, mime_type: str):
    audio_bytes = base64.b64decode(audio_base64)
    mime = str(mime_type or "").lower()
    if "audio/pcm" in mime or "pcm" in mime:
        sample_rate = _sample_rate_from_mime(mime) or 24000
        audio_bytes = _pcm16_to_wav(audio_bytes, sample_rate)
        filename = "push-to-talk.wav"
    elif "webm" in mime:
        filename = "push-to-talk.webm"
    elif "mpeg" in mime or "mp3" in mime:
        filename = "push-to-talk.mp3"
    elif "mp4" in mime:
        filename = "push-to-talk.mp4"
    elif "m4a" in mime:
        filename = "push-to-talk.m4a"
    else:
        filename = "push-to-talk.wav"

    file_obj = io.BytesIO(audio_bytes)
    file_obj.name = filename
    return file_obj


def _pcm16_to_wav(pcm_bytes: bytes, sample_rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm_bytes)
    return output.getvalue()


def _sample_rate_from_mime(mime_type: str) -> int | None:
    match = re.search(r"rate=(\d+)", mime_type)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _language(value: str) -> str:
    return "vi" if str(value or "").lower().startswith("vi") else "en"


def _score(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value or 0)))))
    except (TypeError, ValueError):
        return 0


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]
