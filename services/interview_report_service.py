"""Persistence helpers for completed interview reports."""

from typing import Any

from fastapi import HTTPException, status
from psycopg2.extras import Json

from core.database import get_connection
from services.user_service import upsert_auth_user


def save_interview_report(
    *,
    external_auth_id: str,
    email: str,
    account_full_name: str,
    position: str,
    duration_seconds: int,
    overall_score: int,
    session_score: int,
    transcript_events: list[Any],
    scored_qa: list[dict[str, Any]],
    interview_dimensions: dict[str, Any],
    final_dimensions: dict[str, Any],
    phase2_signals: list[dict[str, Any]],
    avatar_url: str | None = None,
    auth_provider: str = "clerk",
    cv_jd_analysis_id: str | None = None,
) -> dict[str, Any]:
    user = upsert_auth_user(
        external_auth_id=external_auth_id,
        email=email,
        full_name=account_full_name,
        avatar_url=avatar_url,
        auth_provider=auth_provider,
    )

    final = _normalize_dimensions(final_dimensions or interview_dimensions)
    interview = _normalize_dimensions(interview_dimensions)

    try:
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
                      ended_at,
                      duration_seconds,
                      overall_score
                    )
                    values (%s, %s, %s, 'mixed', 'completed', now(), now(), %s, %s)
                    returning *
                    """,
                    (
                        user["id"],
                        cv_jd_analysis_id,
                        position,
                        max(0, int(duration_seconds or 0)),
                        _score(overall_score),
                    ),
                )
                session = cur.fetchone()
                session_id = session["id"]

                _insert_transcript_events(cur, session_id, transcript_events)
                qa_rows = scored_qa or _build_qa_from_transcript_events(transcript_events)
                _insert_qa_pairs(cur, session_id, qa_rows)
                cur.execute(
                    """
                    insert into interview_scores (
                      session_id,
                      d1_technical,
                      d2_communication,
                      d3_problem_solving,
                      d4_attitude,
                      d5_cultural_fit,
                      phase2_signals_json,
                      final_dimensions_json
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (session_id) do update set
                      d1_technical = excluded.d1_technical,
                      d2_communication = excluded.d2_communication,
                      d3_problem_solving = excluded.d3_problem_solving,
                      d4_attitude = excluded.d4_attitude,
                      d5_cultural_fit = excluded.d5_cultural_fit,
                      phase2_signals_json = excluded.phase2_signals_json,
                      final_dimensions_json = excluded.final_dimensions_json
                    """,
                    (
                        session_id,
                        final["D1_technical"],
                        final["D2_communication"],
                        final["D3_problem_solving"],
                        final["D4_attitude"],
                        final["D5_cultural_fit"],
                        Json(
                            {
                                "sessionScore": _score(session_score),
                                "interviewDimensions": interview,
                                "signals": phase2_signals or [],
                            }
                        ),
                        Json(final),
                    ),
                )

            conn.commit()
            return session
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


def _ensure_interview_report_schema(cur) -> None:
    cur.execute(
        "alter table interview_sessions add column if not exists cv_jd_analysis_id uuid null references cv_jd_analyses(id) on delete set null"
    )
    cur.execute("alter table interview_qa_pairs add column if not exists transcript_events_json jsonb null")
    cur.execute("alter table interview_scores add column if not exists phase2_signals_json jsonb null")
    cur.execute("alter table interview_scores add column if not exists final_dimensions_json jsonb null")
    cur.execute(
        """
        create table if not exists interview_transcript_events (
          id bigserial primary key,
          session_id uuid null references interview_sessions(id) on delete cascade,
          event_order int not null,
          role varchar(20) not null,
          text text not null,
          source varchar(50) not null default 'gpt-realtime',
          timestamp_sec int null,
          question_order int null,
          created_at timestamptz not null default now()
        )
        """
    )


def _insert_transcript_events(cur, session_id: str, events: list[Any]) -> None:
    for index, event in enumerate(events or [], start=1):
        role = getattr(event, "role", None) or (event.get("role") if isinstance(event, dict) else "user")
        text = getattr(event, "text", None) or (event.get("text") if isinstance(event, dict) else "")
        source = getattr(event, "source", None) or (event.get("source") if isinstance(event, dict) else "gpt-realtime")
        timestamp_sec = getattr(event, "timestamp_sec", None) or (
            event.get("timestamp_sec") if isinstance(event, dict) else None
        )
        if not str(text or "").strip():
            continue
        cur.execute(
            """
            insert into interview_transcript_events (
              session_id,
              event_order,
              role,
              text,
              source,
              timestamp_sec
            )
            values (%s, %s, %s, %s, %s, %s)
            """,
            (session_id, index, _role(role), str(text).strip(), source or "gpt-realtime", timestamp_sec),
        )


def _insert_qa_pairs(cur, session_id: str, scored_qa: list[dict[str, Any]]) -> None:
    for index, qa in enumerate(scored_qa or [], start=1):
        cur.execute(
            """
            insert into interview_qa_pairs (
              session_id,
              question_order,
              question,
              answer,
              score,
              transcript_events_json
            )
            values (%s, %s, %s, %s, %s, %s)
            on conflict (session_id, question_order) do update set
              question = excluded.question,
              answer = excluded.answer,
              score = excluded.score,
              transcript_events_json = excluded.transcript_events_json
            """,
            (
                session_id,
                index,
                qa.get("question") or "",
                qa.get("answer") or "",
                _score(qa.get("score")),
                Json(qa.get("transcriptEvents") or []),
            ),
        )


def _build_qa_from_transcript_events(events: list[Any]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    pending_question: dict[str, Any] | None = None

    for event in events or []:
        role = getattr(event, "role", None) or (event.get("role") if isinstance(event, dict) else None)
        text = getattr(event, "text", None) or (event.get("text") if isinstance(event, dict) else "")
        timestamp_sec = getattr(event, "timestamp_sec", None) or (
            event.get("timestamp_sec") if isinstance(event, dict) else 0
        )
        text = str(text or "").strip()
        if not text:
            continue

        if role == "assistant":
            pending_question = {"text": text, "timestamp_sec": int(timestamp_sec or 0)}
            continue

        if role == "user" and pending_question:
            timestamp = _format_timestamp(pending_question.get("timestamp_sec", 0))
            pairs.append(
                {
                    "question": pending_question["text"],
                    "answer": text,
                    "timestamp": timestamp,
                    "score": 0,
                }
            )
            pending_question = None

    return pairs


def _normalize_dimensions(raw: dict[str, Any]) -> dict[str, int]:
    return {
        "D1_technical": _score(raw.get("D1_technical")),
        "D2_communication": _score(raw.get("D2_communication")),
        "D3_problem_solving": _score(raw.get("D3_problem_solving")),
        "D4_attitude": _score(raw.get("D4_attitude")),
        "D5_cultural_fit": _score(raw.get("D5_cultural_fit")),
    }


def _score(value: Any) -> int:
    try:
        return max(0, min(100, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _role(value: Any) -> str:
    role = str(value or "user").strip().lower()
    return role if role in {"user", "assistant", "system"} else "user"


def _format_timestamp(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
