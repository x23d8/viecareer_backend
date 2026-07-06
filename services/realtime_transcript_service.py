"""Persistence helpers for server-owned realtime interview transcripts."""

from typing import Any

from psycopg2.extras import Json

from core.database import get_connection
from services.interview_report_service import _ensure_interview_report_schema
from services.user_service import upsert_auth_user


def create_live_interview_session(metadata: dict[str, Any]) -> str | None:
    """Create an in-progress interview session when authenticated user metadata is present."""
    external_auth_id = str(metadata.get("external_auth_id") or "").strip()
    email = str(metadata.get("email") or "").strip()
    account_full_name = str(metadata.get("account_full_name") or "").strip() or email
    if not external_auth_id or not email or not account_full_name:
        return None

    user = upsert_auth_user(
        external_auth_id=external_auth_id,
        email=email,
        full_name=account_full_name,
        avatar_url=metadata.get("avatar_url"),
        auth_provider=str(metadata.get("auth_provider") or "clerk"),
    )
    candidate = metadata.get("candidate") if isinstance(metadata.get("candidate"), dict) else {}
    position = str(metadata.get("position") or "Software Developer")
    job_description = str(metadata.get("job_description") or metadata.get("jd_summary") or "")
    language = str(metadata.get("language") or "en")
    voice = str(metadata.get("voice") or "shimmer")
    voice_mode = str(metadata.get("voice_mode") or "realtime_conversation")
    show_transcript = bool(metadata.get("show_transcript", True))
    blueprint = metadata.get("blueprint") if isinstance(metadata.get("blueprint"), dict) else metadata.get("interview_blueprint")
    blueprint = blueprint if isinstance(blueprint, dict) else {}
    interview_style = str(metadata.get("interview_style") or "mixed")
    context_snapshot = {
        "candidate": candidate,
        "position": position,
        "job_description": job_description,
        "language": language,
        "voice": voice,
        "voice_mode": voice_mode,
        "show_transcript": show_transcript,
        "blueprint": blueprint,
        "interview_style": interview_style,
        "cv_jd_analysis_id": metadata.get("cv_jd_analysis_id"),
    }

    with get_connection() as conn:
        with conn.cursor() as cur:
            _ensure_interview_report_schema(cur)
            cur.execute(
                """
                insert into interview_sessions (
                  user_id,
                  cv_jd_analysis_id,
                  position,
                  session_type,
                  status,
                  started_at,
                  candidate_json,
                  job_description,
                  language,
                  voice,
                  blueprint_json,
                  context_json
                )
                values (%s, %s, %s, %s, 'in_progress', now(), %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    user["id"],
                    metadata.get("cv_jd_analysis_id") or None,
                    position,
                    interview_style,
                    Json(candidate),
                    job_description,
                    language,
                    voice,
                    Json(blueprint),
                    Json(context_snapshot),
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return str(row["id"]) if row else None


def save_realtime_transcript_event(
    *,
    session_id: str | None,
    event_order: int,
    role: str,
    text: str,
    source: str = "gpt-realtime-ws",
    timestamp_sec: int | None = None,
    question_order: int | None = None,
    **_extra: Any,
) -> None:
    clean = str(text or "").strip()
    if not clean:
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            _ensure_interview_report_schema(cur)
            cur.execute(
                """
                insert into interview_transcript_events (
                  session_id,
                  event_order,
                  role,
                  text,
                  source,
                  timestamp_sec,
                  question_order
                )
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    event_order,
                    _role(role),
                    clean,
                    source,
                    timestamp_sec,
                    question_order,
                ),
            )
        conn.commit()


def save_realtime_qa_pair(
    *,
    session_id: str | None,
    question_order: int,
    question: str,
    answer: str,
    transcript_events: list[dict[str, Any]],
    raw_transcript: str | None = None,
    polished_transcript: str | None = None,
    score: int | None = None,
    feedback: str | None = None,
    score_details: dict[str, Any] | None = None,
) -> None:
    if not session_id:
        return
    if not str(question or "").strip() or not str(answer or "").strip():
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            _ensure_interview_report_schema(cur)
            cur.execute(
                "alter table interview_qa_pairs add column if not exists raw_transcript text null"
            )
            cur.execute(
                "alter table interview_qa_pairs add column if not exists polished_transcript text null"
            )
            cur.execute("alter table interview_qa_pairs add column if not exists feedback text null")
            cur.execute("alter table interview_qa_pairs add column if not exists score_json jsonb null")
            cur.execute(
                """
                insert into interview_qa_pairs (
                  session_id,
                  question_order,
                  question,
                  answer,
                  raw_transcript,
                  polished_transcript,
                  score,
                  feedback,
                  score_json,
                  transcript_events_json
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (session_id, question_order) do update set
                  question = excluded.question,
                  answer = excluded.answer,
                  raw_transcript = excluded.raw_transcript,
                  polished_transcript = excluded.polished_transcript,
                  score = excluded.score,
                  feedback = excluded.feedback,
                  score_json = excluded.score_json,
                  transcript_events_json = excluded.transcript_events_json
                """,
                (
                    session_id,
                    question_order,
                    str(question).strip(),
                    str(answer).strip(),
                    str(raw_transcript or "").strip() or None,
                    str(polished_transcript or answer).strip(),
                    _score(score),
                    str(feedback or "").strip() or None,
                    Json(score_details or {}),
                    Json(transcript_events),
                ),
            )
        conn.commit()


def complete_live_interview_session(session_id: str | None, duration_seconds: int | None = None) -> None:
    if not session_id:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update interview_sessions s
                set
                  status = case
                    when exists (
                      select 1 from interview_qa_pairs q where q.session_id = s.id
                    ) then 'completed'
                    else 'cancelled'
                  end,
                  ended_at = now(),
                  duration_seconds = coalesce(%s, duration_seconds),
                  updated_at = now()
                where s.id = %s
                  and s.status not in ('completed', 'cancelled')
                """,
                (duration_seconds, session_id),
            )
        conn.commit()


def complete_live_interview_session_for_user(
    session_id: str | None,
    external_auth_id: str,
    duration_seconds: int | None = None,
) -> bool:
    if not session_id or not external_auth_id:
        return False

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update interview_sessions s
                set
                  status = case
                    when exists (
                      select 1 from interview_qa_pairs q where q.session_id = s.id
                    ) then 'completed'
                    else 'cancelled'
                  end,
                  ended_at = now(),
                  duration_seconds = coalesce(%s, duration_seconds),
                  updated_at = now()
                from users u
                where s.id = %s
                  and s.user_id = u.id
                  and u.external_auth_id = %s
                  and s.status not in ('completed', 'cancelled')
                """,
                (duration_seconds, session_id, external_auth_id),
            )
            updated = cur.rowcount > 0
            if not updated:
                cur.execute(
                    """
                    select 1
                    from interview_sessions s
                    join users u on u.id = s.user_id
                    where s.id = %s
                      and u.external_auth_id = %s
                    limit 1
                    """,
                    (session_id, external_auth_id),
                )
                updated = cur.fetchone() is not None
        conn.commit()
    return updated


def _role(value: str) -> str:
    role = str(value or "user").strip().lower()
    return role if role in {"user", "assistant", "system"} else "user"


def _score(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return None
