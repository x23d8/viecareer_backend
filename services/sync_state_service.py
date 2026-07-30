"""Read-only cross-client state for authenticated VieCareer users."""

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from core.database import get_connection


def get_sync_state(*, external_auth_id: str, report_limit: int = 20) -> dict[str, Any]:
    """Return profile, progress, uploaded CVs and interview history for one user."""
    limit = max(1, min(50, int(report_limit or 20)))
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("set local statement_timeout = '8000ms'")
                cur.execute(
                    """
                    select
                      u.id::text as id,
                      u.email,
                      u.full_name,
                      u.avatar_url,
                      u.created_at,
                      p.birth_year,
                      p.university,
                      p.target_position,
                      p.bio,
                      p.location,
                      p.github_url,
                      p.linkedin_url,
                      p.updated_at as profile_updated_at
                    from users u
                    left join user_profiles p on p.user_id = u.id
                    where u.external_auth_id = %s
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
                      a.jri_baseline,
                      a.llm_raw_json as analysis
                    from cv_jd_analyses a
                    where a.user_id = %s
                      and coalesce(a.llm_raw_json->>'synthetic', 'false') <> 'true'
                    order by a.created_at desc
                    limit 1
                    """,
                    (user_id,),
                )
                latest_analysis = cur.fetchone()

                cur.execute(
                    """
                    select exists (
                      select 1
                      from information_schema.columns
                      where table_schema = current_schema()
                        and table_name = 'uploaded_files'
                        and column_name = 'file_purpose'
                    ) as supported
                    """
                )
                file_purpose_supported = bool((cur.fetchone() or {}).get("supported"))
                purpose_column = "f.file_purpose" if file_purpose_supported else "'cv'::text"
                purpose_filter = "and f.file_purpose = 'cv'" if file_purpose_supported else ""

                cur.execute(
                    f"""
                    select
                      f.id::text as id,
                      f.file_name,
                      f.file_type,
                      f.mime_type,
                      f.file_size_bytes,
                      f.storage_url,
                      left(coalesce(f.extracted_text, ''), 60000) as extracted_text,
                      (length(coalesce(f.extracted_text, '')) > 60000) as text_truncated,
                      {purpose_column} as file_purpose,
                      f.created_at,
                      (extract(epoch from f.created_at) * 1000)::bigint as created_at_epoch_ms,
                      exists (
                        select 1
                        from cv_jd_analyses a
                        where a.user_id = %s and a.cv_file_id = f.id
                      ) as linked_to_analysis
                    from uploaded_files f
                    where f.user_id = %s {purpose_filter}
                    order by f.created_at desc
                    limit %s
                    """,
                    (user_id, user_id, limit),
                )
                uploaded_cvs = list(cur.fetchall() or [])

                cur.execute(
                    """
                    select
                      id::text as id,
                      skill_name,
                      coalesce(progress_percent, 0)::int as progress_percent,
                      stage,
                      trend,
                      updated_at
                    from user_skills
                    where user_id = %s
                    order by coalesce(progress_percent, 0) desc, skill_name
                    """,
                    (user_id,),
                )
                skills = list(cur.fetchall() or [])

                cur.execute(
                    """
                    select
                      id::text as id,
                      target_role,
                      coalesce(match_percent, 0)::int as match_percent,
                      status,
                      created_at,
                      updated_at
                    from learning_roadmaps
                    where user_id = %s
                    order by
                      case when status = 'active' then 0 else 1 end,
                      updated_at desc
                    """,
                    (user_id,),
                )
                roadmaps = list(cur.fetchall() or [])

                cur.execute(
                    """
                    select
                      t.id::text as id,
                      t.roadmap_id::text as roadmap_id,
                      t.phase_name,
                      t.task_title,
                      t.task_type,
                      t.duration_minutes,
                      t.status,
                      t.display_order,
                      t.updated_at
                    from roadmap_tasks t
                    join learning_roadmaps r on r.id = t.roadmap_id
                    where r.user_id = %s
                    order by r.updated_at desc, t.display_order, t.id
                    """,
                    (user_id,),
                )
                roadmap_tasks = list(cur.fetchall() or [])

                cur.execute(
                    f"""
                    select
                      (
                        select count(*)::int
                        from uploaded_files f
                        where f.user_id = %s {purpose_filter}
                      ) as uploaded_cvs,
                      (
                        select count(*)::int
                        from cv_jd_analyses a
                        where a.user_id = %s
                          and coalesce(a.llm_raw_json->>'synthetic', 'false') <> 'true'
                      ) as cv_analyses,
                      (
                        select count(*)::int
                        from interview_sessions s
                        where s.user_id = %s
                      ) as interviews,
                      (
                        select count(*)::int
                        from interview_sessions s
                        where s.user_id = %s and s.status = 'completed'
                      ) as completed_interviews,
                      (
                        select count(*)::int
                        from interview_sessions s
                        where s.user_id = %s and s.status = 'cancelled'
                      ) as cancelled_interviews,
                      (
                        select coalesce(sum(s.duration_seconds), 0)::int
                        from interview_sessions s
                        where s.user_id = %s and s.status = 'completed'
                      ) as interview_seconds,
                      (
                        select coalesce(round(avg(s.overall_score)), 0)::int
                        from interview_sessions s
                        where s.user_id = %s
                          and s.status = 'completed'
                          and s.overall_score is not null
                      ) as average_interview_score
                    """,
                    (user_id, user_id, user_id, user_id, user_id, user_id, user_id),
                )
                metrics = dict(cur.fetchone() or {})

                cur.execute(
                    """
                    select
                      s.id::text as id,
                      s.created_at,
                      (extract(epoch from s.created_at) * 1000)::bigint as created_at_epoch_ms,
                      s.position,
                      s.company,
                      s.session_type,
                      s.status,
                      s.ended_at,
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
        latest_report_score = (
            report_payloads[0]["overall_score"] or report_payloads[0]["session_score"]
            if report_payloads
            else 0
        )
        baseline_score = _score((analysis_payload or {}).get("analysis", {}).get("jriBaseline"))
        jri_score = latest_report_score or baseline_score
        profile_payload = _profile_payload(user)
        if analysis_payload:
            analysis_profile = analysis_payload["analysis"]
            if not profile_payload.get("target_position"):
                profile_payload["target_position"] = analysis_profile.get("targetPosition")
        progress_payload = _progress_payload(
            profile=profile_payload,
            skills=skills,
            roadmaps=roadmaps,
            roadmap_tasks=roadmap_tasks,
            metrics=metrics,
        )
        return {
            "profile": profile_payload,
            "latest_analysis": analysis_payload,
            "uploaded_cvs": [_uploaded_cv_payload(row) for row in uploaded_cvs],
            "reports": report_payloads,
            "progress": progress_payload,
            "dashboard": {
                "jri_score": jri_score,
                "baseline_score": baseline_score,
                "completed_interviews": int(metrics.get("completed_interviews") or 0),
                "uploaded_cvs": int(metrics.get("uploaded_cvs") or 0),
                "cv_analyses": int(metrics.get("cv_analyses") or 0),
                "interview_minutes": int(metrics.get("interview_seconds") or 0) // 60,
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
        "profile": None,
        "latest_analysis": None,
        "uploaded_cvs": [],
        "reports": [],
        "progress": {
            "skills": [],
            "roadmaps": [],
            "summary": {
                "profile_completion_percent": 0,
                "roadmap_completion_percent": 0,
                "completed_roadmap_tasks": 0,
                "total_roadmap_tasks": 0,
                "average_skill_progress": 0,
                "uploaded_cvs": 0,
                "cv_analyses": 0,
                "interviews": 0,
                "completed_interviews": 0,
                "cancelled_interviews": 0,
                "interview_minutes": 0,
                "average_interview_score": 0,
            },
        },
        "dashboard": {
            "jri_score": 0,
            "baseline_score": 0,
            "completed_interviews": 0,
            "uploaded_cvs": 0,
            "cv_analyses": 0,
            "interview_minutes": 0,
        },
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def _analysis_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    analysis = dict(row.get("analysis") or {})
    analysis["analysisId"] = str(row["id"])
    if not analysis.get("jriBaseline") and row.get("jri_baseline") is not None:
        analysis["jriBaseline"] = _score(row.get("jri_baseline"))
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
        "company": str(row.get("company") or ""),
        "type": session_type if session_type in {"technical", "behavioral", "mixed"} else "mixed",
        "status": str(row.get("status") or "completed"),
        "ended_at": row.get("ended_at"),
        "duration_seconds": max(0, int(row.get("duration_seconds") or 0)),
        "overall_score": _score(row.get("overall_score")),
        "session_score": _score(row.get("session_score")),
        "dimensions": dict(row.get("dimensions") or {}),
        "transcript_events": list(row.get("transcript_events") or []),
        "scored_qa": list(row.get("scored_qa") or []),
    }


def _profile_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "email": str(row.get("email") or ""),
        "full_name": str(row.get("full_name") or ""),
        "avatar_url": row.get("avatar_url"),
        "birth_year": row.get("birth_year"),
        "university": row.get("university"),
        "target_position": row.get("target_position"),
        "bio": row.get("bio"),
        "location": row.get("location"),
        "github_url": row.get("github_url"),
        "linkedin_url": row.get("linkedin_url"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("profile_updated_at"),
    }


def _uploaded_cv_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "file_name": str(row.get("file_name") or ""),
        "file_type": str(row.get("file_type") or ""),
        "mime_type": str(row.get("mime_type") or ""),
        "file_size_bytes": max(0, int(row.get("file_size_bytes") or 0)),
        "storage_url": str(row.get("storage_url") or ""),
        "extracted_text": str(row.get("extracted_text") or ""),
        "text_truncated": bool(row.get("text_truncated")),
        "purpose": str(row.get("file_purpose") or "cv"),
        "linked_to_analysis": bool(row.get("linked_to_analysis")),
        "created_at": row.get("created_at"),
        "created_at_epoch_ms": int(row.get("created_at_epoch_ms") or 0),
    }


def _progress_payload(
    *,
    profile: dict[str, Any],
    skills: list[dict[str, Any]],
    roadmaps: list[dict[str, Any]],
    roadmap_tasks: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    tasks_by_roadmap: dict[str, list[dict[str, Any]]] = {}
    for task in roadmap_tasks:
        roadmap_id = str(task.get("roadmap_id") or "")
        tasks_by_roadmap.setdefault(roadmap_id, []).append(
            {
                "id": str(task["id"]),
                "phase_name": task.get("phase_name"),
                "title": str(task.get("task_title") or ""),
                "type": task.get("task_type"),
                "duration_minutes": max(0, int(task.get("duration_minutes") or 0)),
                "status": str(task.get("status") or "planned"),
                "display_order": int(task.get("display_order") or 0),
                "updated_at": task.get("updated_at"),
            }
        )

    roadmap_payloads = []
    for roadmap in roadmaps:
        roadmap_id = str(roadmap["id"])
        tasks = tasks_by_roadmap.get(roadmap_id, [])
        completed = sum(1 for task in tasks if task["status"] == "completed")
        roadmap_payloads.append(
            {
                "id": roadmap_id,
                "target_role": str(roadmap.get("target_role") or ""),
                "match_percent": _score(roadmap.get("match_percent")),
                "status": str(roadmap.get("status") or "active"),
                "tasks": tasks,
                "completed_tasks": completed,
                "total_tasks": len(tasks),
                "completion_percent": round(completed * 100 / len(tasks)) if tasks else 0,
                "created_at": roadmap.get("created_at"),
                "updated_at": roadmap.get("updated_at"),
            }
        )

    skill_payloads = [
        {
            "id": str(skill["id"]),
            "name": str(skill.get("skill_name") or ""),
            "progress_percent": _score(skill.get("progress_percent")),
            "stage": skill.get("stage"),
            "trend": skill.get("trend"),
            "updated_at": skill.get("updated_at"),
        }
        for skill in skills
    ]
    total_tasks = len(roadmap_tasks)
    completed_tasks = sum(1 for task in roadmap_tasks if task.get("status") == "completed")
    average_skill_progress = (
        round(sum(skill["progress_percent"] for skill in skill_payloads) / len(skill_payloads))
        if skill_payloads
        else 0
    )
    return {
        "skills": skill_payloads,
        "roadmaps": roadmap_payloads,
        "summary": {
            "profile_completion_percent": _profile_completion(profile),
            "roadmap_completion_percent": (
                round(completed_tasks * 100 / total_tasks) if total_tasks else 0
            ),
            "completed_roadmap_tasks": completed_tasks,
            "total_roadmap_tasks": total_tasks,
            "average_skill_progress": average_skill_progress,
            "uploaded_cvs": int(metrics.get("uploaded_cvs") or 0),
            "cv_analyses": int(metrics.get("cv_analyses") or 0),
            "interviews": int(metrics.get("interviews") or 0),
            "completed_interviews": int(metrics.get("completed_interviews") or 0),
            "cancelled_interviews": int(metrics.get("cancelled_interviews") or 0),
            "interview_minutes": int(metrics.get("interview_seconds") or 0) // 60,
            "average_interview_score": _score(metrics.get("average_interview_score")),
        },
    }


def _profile_completion(profile: dict[str, Any]) -> int:
    fields = (
        "full_name",
        "email",
        "avatar_url",
        "university",
        "target_position",
        "bio",
        "location",
        "github_url",
        "linkedin_url",
    )
    completed = sum(1 for field in fields if profile.get(field))
    return round(completed * 100 / len(fields))


def _score(value: Any) -> int:
    try:
        return max(0, min(100, int(value or 0)))
    except (TypeError, ValueError):
        return 0
