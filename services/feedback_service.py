"""Persistence helpers for session-scoped user feedback."""

import json
import logging
from typing import Any

from fastapi import HTTPException, status

from core.database import get_connection

logger = logging.getLogger("viecareer")

NEGATIVE_EMOTIONS = ("very_bad", "bad")


def submit_session_feedback(
    *,
    external_auth_id: str,
    email: str | None,
    session_id: str,
    source: str,
    emotion: str,
    rating: int,
    comment: str | None,
    tags: list[str],
) -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_feedback_schema(cur)
                user = _get_user(cur, external_auth_id=external_auth_id, email=email)
                if not user:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND,
                        detail="User profile has not been synced yet.",
                    )

                cur.execute(
                    """
                    select id, session_type
                    from interview_sessions
                    where id = %s and user_id = %s
                    """,
                    (session_id, user["id"]),
                )
                session = cur.fetchone()
                if not session:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND,
                        detail="Session was not found for the current user.",
                    )
                if source == "coding" and session["session_type"] != "coding":
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        detail="Coding feedback must reference a coding session.",
                    )

                cur.execute(
                    """
                    insert into session_feedback (
                      session_id,
                      user_id,
                      source,
                      emotion,
                      rating,
                      comment,
                      tags_json,
                      updated_at
                    )
                    values (%s, %s, %s, %s, %s, %s, %s::jsonb, now())
                    on conflict (session_id, user_id, source) do update set
                      emotion = excluded.emotion,
                      rating = excluded.rating,
                      comment = excluded.comment,
                      tags_json = excluded.tags_json,
                      updated_at = now()
                    returning *
                    """,
                    (
                        session_id,
                        user["id"],
                        source,
                        emotion,
                        rating,
                        comment,
                        json.dumps(tags),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return _feedback_payload(row)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not save feedback for session=%s", session_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not save feedback. Please try again.",
        ) from exc


def create_completed_coding_session(
    *,
    external_auth_id: str,
    email: str | None,
    position: str,
    company: str | None,
    duration_seconds: int | None,
    overall_score: int | None,
) -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                user = _get_user(cur, external_auth_id=external_auth_id, email=email)
                if not user:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND,
                        detail="User profile has not been synced yet.",
                    )
                cur.execute(
                    """
                    insert into interview_sessions (
                      user_id,
                      position,
                      company,
                      session_type,
                      status,
                      started_at,
                      ended_at,
                      duration_seconds,
                      overall_score,
                      created_at,
                      updated_at
                    )
                    values (%s, %s, %s, 'coding', 'completed', now(), now(), %s, %s, now(), now())
                    returning id, position, company, session_type, status, duration_seconds, overall_score, created_at
                    """,
                    (
                        user["id"],
                        position,
                        company,
                        duration_seconds,
                        overall_score,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return _coding_session_payload(row)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not create coding session for user=%s", external_auth_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not create coding session. Please try again.",
        ) from exc


def list_admin_feedback(
    *,
    limit: int,
    offset: int,
    search: str | None,
    negative_only: bool,
    source: str | None,
    emotion: str | None,
    rating_max: int | None,
    tag: str | None,
) -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_feedback_schema(cur)
                clauses = ["true"]
                params: list[Any] = []
                if negative_only:
                    clauses.append("(f.emotion in ('very_bad', 'bad') or f.rating <= 2)")
                if source:
                    clauses.append("f.source = %s")
                    params.append(source)
                if emotion:
                    clauses.append("f.emotion = %s")
                    params.append(emotion)
                if rating_max is not None:
                    clauses.append("f.rating <= %s")
                    params.append(rating_max)
                if tag:
                    clauses.append("f.tags_json @> %s::jsonb")
                    params.append(json.dumps([tag]))
                if search:
                    term = f"%{search.strip()}%"
                    clauses.append(
                        """
                        (
                          coalesce(u.email, '') ilike %s
                          or coalesce(u.full_name, '') ilike %s
                          or coalesce(s.position, '') ilike %s
                          or coalesce(f.comment, '') ilike %s
                          or f.session_id::text ilike %s
                        )
                        """
                    )
                    params.extend([term, term, term, term, term])
                where_sql = " and ".join(clauses)

                cur.execute(
                    f"""
                    select count(*)::int as total
                    from session_feedback f
                    join interview_sessions s on s.id = f.session_id
                    join users u on u.id = f.user_id
                    where {where_sql}
                    """,
                    params,
                )
                total = int((cur.fetchone() or {}).get("total") or 0)

                cur.execute(
                    f"""
                    select
                      f.*,
                      s.position,
                      s.company,
                      s.session_type,
                      u.email,
                      u.full_name
                    from session_feedback f
                    join interview_sessions s on s.id = f.session_id
                    join users u on u.id = f.user_id
                    where {where_sql}
                    order by f.updated_at desc, f.created_at desc
                    limit %s offset %s
                    """,
                    [*params, limit, offset],
                )
                items = [_admin_feedback_payload(row) for row in cur.fetchall()]
        return {
            "items": items,
            "pagination": {"limit": limit, "offset": offset, "total": total},
        }
    except Exception as exc:
        logger.exception("Could not list admin feedback")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load feedback.",
        ) from exc


def _get_user(cur, *, external_auth_id: str, email: str | None) -> dict[str, Any] | None:
    cur.execute(
        """
        select id, external_auth_id, email
        from users
        where external_auth_id = %s
        order by case when external_auth_id = %s then 0 else 1 end
        limit 1
        """,
        (external_auth_id, external_auth_id),
    )
    return cur.fetchone()


def _ensure_feedback_schema(cur) -> None:
    cur.execute(
        """
        create table if not exists session_feedback (
          id uuid primary key default gen_random_uuid(),
          session_id uuid not null references interview_sessions(id) on delete cascade,
          user_id uuid not null references users(id) on delete cascade,
          source varchar(20) not null check (source in ('interview', 'coding', 'report')),
          emotion varchar(20) not null check (emotion in ('very_bad', 'bad', 'neutral', 'good', 'great')),
          rating smallint not null check (rating between 1 and 5),
          comment text null,
          tags_json jsonb not null default '[]'::jsonb,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          unique (session_id, user_id, source)
        )
        """
    )
    cur.execute(
        "create index if not exists idx_session_feedback_negative on session_feedback (rating, emotion, updated_at desc)"
    )
    cur.execute(
        "create index if not exists idx_session_feedback_session on session_feedback (session_id, updated_at desc)"
    )
    cur.execute(
        "create index if not exists idx_session_feedback_tags on session_feedback using gin (tags_json)"
    )


def _feedback_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "source": row["source"],
        "emotion": row["emotion"],
        "rating": int(row["rating"]),
        "comment": row.get("comment"),
        "tags": row.get("tags_json") or [],
        "is_negative": row["emotion"] in NEGATIVE_EMOTIONS or int(row["rating"]) <= 2,
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _admin_feedback_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = _feedback_payload(row)
    payload.update(
        {
            "user_id": str(row["user_id"]),
            "user_name": row.get("full_name") or row.get("email") or "Unknown user",
            "email": row.get("email"),
            "position": row.get("position"),
            "company": row.get("company"),
            "session_type": row.get("session_type"),
        }
    )
    return payload


def _coding_session_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "position": row.get("position"),
        "company": row.get("company"),
        "session_type": row.get("session_type"),
        "status": row.get("status"),
        "duration_seconds": row.get("duration_seconds"),
        "overall_score": row.get("overall_score"),
        "created_at": _iso(row.get("created_at")),
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)
