"""Read-only cross-client state for authenticated VieCareer users."""

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from core.database import get_connection


def get_sync_state(*, external_auth_id: str, report_limit: int = 20) -> dict[str, Any]:
    """Return the latest analysis and completed reports without mutating schema or data."""
    limit = max(1, min(50, int(report_limit or 20)))
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("set local statement_timeout = '8000ms'")
                cur.execute(
                    """
                    select id
                    from users
                    where external_auth_id = %s
                    limit 1
                    """,
                    (external_auth_id,),
                )
                user = cur.fetchone()
                if not user:
                    return _empty_state()

                user_id = user["id"]
                cur.execute(
                    """
                    select
                      a.id::text as id,
                      a.created_at,
                      (extract(epoch from a.created_at) * 1000)::bigint as created_at_epoch_ms,
                      a.llm_raw_json as analysis
                    from cv_jd_analyses a
                    where a.user_id = %s
                    order by a.created_at desc
                    limit 1
                    """,
                    (user_id,),
                )
                latest_analysis = cur.fetchone()

                cur.execute(
                    """
                    select
                      s.id::text as id,
                      s.created_at,
                      (extract(epoch from s.created_at) * 1000)::bigint as created_at_epoch_ms,
                      s.position,
                      s.session_type,
                      coalesce(s.duration_seconds, 0)::int as duration_seconds,
                      coalesce(s.overall_score, 0)::int as overall_score,
                      coalesce(
                        case
                          when (sc.phase2_signals_json ->> 'sessionScore') ~ '^[0-9]{1,3}$'
                            then (sc.phase2_signals_json ->> 'sessionScore')::int
                          else null
                        end,
                        s.overall_score,
                        0
                      )::int as session_score,
                      coalesce(sc.final_dimensions_json, '{}'::jsonb) as dimensions,
                      coalesce(
                        (
                          select jsonb_agg(
                            jsonb_build_object(
                              'role', e.role,
                              'text', e.text,
                              'timestamp_sec', e.timestamp_sec,
                              'source', e.source
                            )
                            order by e.event_order
                          )
                          from (
                            select role, text, timestamp_sec, source, event_order
                            from interview_transcript_events
                            where session_id = s.id
                            order by event_order
                            limit 250
                          ) e
                        ),
                        '[]'::jsonb
                      ) as transcript_events,
                      coalesce(
                        (
                          select jsonb_agg(
                            jsonb_build_object(
                              'question', q.question,
                              'answer', q.answer,
                              'score', coalesce(q.score, 0),
                              'timestamp', '',
                              'feedback', coalesce(q.feedback, '')
                            )
                            order by q.question_order
                          )
                          from (
                            select
                              question,
                              answer,
                              score,
                              feedback,
                              question_order
                            from interview_qa_pairs
                            where session_id = s.id
                            order by question_order
                            limit 50
                          ) q
                        ),
                        '[]'::jsonb
                      ) as scored_qa
                    from interview_sessions s
                    left join interview_scores sc on sc.session_id = s.id
                    where s.user_id = %s and s.status = 'completed'
                    order by coalesce(s.ended_at, s.created_at) desc
                    limit %s
                    """,
                    (user_id, limit),
                )
                reports = list(cur.fetchall() or [])

        analysis_payload = _analysis_payload(latest_analysis)
        report_payloads = [_report_payload(row) for row in reports]
        latest_report_score = report_payloads[0]["overall_score"] if report_payloads else 0
        baseline_score = _score((analysis_payload or {}).get("analysis", {}).get("jriBaseline"))
        jri_score = latest_report_score or baseline_score
        return {
            "latest_analysis": analysis_payload,
            "reports": report_payloads,
            "dashboard": {
                "jri_score": jri_score,
                "baseline_score": baseline_score,
                "completed_interviews": len(report_payloads),
            },
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not load synchronized user state.",
        ) from exc


def _empty_state() -> dict[str, Any]:
    return {
        "latest_analysis": None,
        "reports": [],
        "dashboard": {
            "jri_score": 0,
            "baseline_score": 0,
            "completed_interviews": 0,
        },
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def _analysis_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    analysis = dict(row.get("analysis") or {})
    analysis["analysisId"] = str(row["id"])
    return {
        "id": str(row["id"]),
        "created_at": row.get("created_at"),
        "created_at_epoch_ms": int(row.get("created_at_epoch_ms") or 0),
        "analysis": analysis,
    }


def _report_payload(row: dict[str, Any]) -> dict[str, Any]:
    session_type = str(row.get("session_type") or "mixed").strip().lower()
    return {
        "id": str(row["id"]),
        "created_at": row.get("created_at"),
        "created_at_epoch_ms": int(row.get("created_at_epoch_ms") or 0),
        "position": str(row.get("position") or "Target position"),
        "type": session_type if session_type in {"technical", "behavioral", "mixed"} else "mixed",
        "duration_seconds": max(0, int(row.get("duration_seconds") or 0)),
        "overall_score": _score(row.get("overall_score")),
        "session_score": _score(row.get("session_score")),
        "dimensions": dict(row.get("dimensions") or {}),
        "transcript_events": list(row.get("transcript_events") or []),
        "scored_qa": list(row.get("scored_qa") or []),
    }


def _score(value: Any) -> int:
    try:
        return max(0, min(100, int(value or 0)))
    except (TypeError, ValueError):
        return 0
