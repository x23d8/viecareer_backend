"""Personalized roadmap persistence keyed by one user's exact CV/JD pair."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import HTTPException, status
from psycopg2.extras import Json

from core.database import get_connection
from services.cv_jd_analysis_service import cv_jd_input_fingerprint


def list_roadmaps(*, external_auth_id: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            user_id = _require_user_id(cur, external_auth_id)
            cur.execute(
                """
                select
                  roadmap.*,
                  job.title as job_title,
                  job.company_name,
                  count(task.id)::int as total_tasks,
                  count(task.id) filter (where task.status = 'completed')::int as completed_tasks
                from learning_roadmaps roadmap
                left join jobs job on job.id = roadmap.job_id
                left join roadmap_tasks task on task.roadmap_id = roadmap.id
                where roadmap.user_id = %s
                group by roadmap.id, job.id
                order by roadmap.updated_at desc, roadmap.created_at desc
                """,
                (user_id,),
            )
            return [_roadmap_summary(dict(row)) for row in cur.fetchall()]


def get_or_create_roadmap(
    *,
    external_auth_id: str,
    cv_jd_analysis_id: str,
    language: str = "vi",
) -> dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            user_id = _require_user_id(cur, external_auth_id)
            analysis = _load_analysis(cur, user_id, cv_jd_analysis_id)
            fingerprint = _analysis_fingerprint(cur, analysis)

            cur.execute(
                """
                select id
                from learning_roadmaps
                where user_id = %s and input_fingerprint = %s
                order by created_at desc
                limit 1
                """,
                (user_id, fingerprint),
            )
            existing = cur.fetchone()
            if existing:
                conn.commit()
                return {
                    "roadmap": _load_roadmap_detail(cur, user_id, str(existing["id"])),
                    "created": False,
                }

            title, summary = _roadmap_copy(analysis, language)
            cur.execute(
                """
                insert into learning_roadmaps (
                  user_id, cv_jd_analysis_id, job_id, input_fingerprint,
                  target_role, title, summary, language, match_percent, status
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
                returning id
                """,
                (
                    user_id,
                    analysis["id"],
                    analysis.get("job_id"),
                    fingerprint,
                    _target_role(analysis),
                    title,
                    summary,
                    language,
                    analysis.get("ats_score") or analysis.get("jri_baseline"),
                ),
            )
            roadmap_id = str(cur.fetchone()["id"])
            tasks = build_personalized_tasks(analysis, language=language)
            _insert_tasks(cur, roadmap_id, tasks)
        conn.commit()

        with conn.cursor() as cur:
            return {
                "roadmap": _load_roadmap_detail(cur, user_id, roadmap_id),
                "created": True,
            }


def get_roadmap(*, external_auth_id: str, roadmap_id: str) -> dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            user_id = _require_user_id(cur, external_auth_id)
            return _load_roadmap_detail(cur, user_id, roadmap_id)


def add_roadmap_task(
    *,
    external_auth_id: str,
    roadmap_id: str,
    task: dict[str, Any],
) -> dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            user_id = _require_user_id(cur, external_auth_id)
            _require_roadmap(cur, user_id, roadmap_id)
            cur.execute(
                """
                select id
                from roadmap_tasks
                where roadmap_id = %s and client_key = %s
                """,
                (roadmap_id, task["client_key"]),
            )
            existing = cur.fetchone()
            if existing:
                return {
                    "roadmap": _load_roadmap_detail(cur, user_id, roadmap_id),
                    "added": False,
                }

            cur.execute(
                "select coalesce(max(display_order), 0) + 1 as next_order from roadmap_tasks where roadmap_id = %s",
                (roadmap_id,),
            )
            display_order = int(cur.fetchone()["next_order"])
            cur.execute(
                """
                insert into roadmap_tasks (
                  roadmap_id, phase_name, task_title, skill_key, description,
                  task_type, duration_minutes, expected_boost, client_key,
                  source_gap_json, status, display_order
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'planned', %s)
                """,
                (
                    roadmap_id,
                    task.get("phase_name"),
                    task["title"],
                    task["skill_key"],
                    task["description"],
                    task["task_type"],
                    task["duration_minutes"],
                    task["expected_boost"],
                    task["client_key"],
                    Json({"source": "analytics_recommendation"}),
                    display_order,
                ),
            )
            cur.execute(
                "update learning_roadmaps set completed_at = null, updated_at = now() where id = %s",
                (roadmap_id,),
            )
        conn.commit()
        with conn.cursor() as cur:
            return {
                "roadmap": _load_roadmap_detail(cur, user_id, roadmap_id),
                "added": True,
            }


def update_roadmap_task(
    *,
    external_auth_id: str,
    roadmap_id: str,
    task_id: int,
    task_status: str,
    completion_note: str | None = None,
) -> dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            user_id = _require_user_id(cur, external_auth_id)
            _require_roadmap(cur, user_id, roadmap_id)
            cur.execute(
                """
                update roadmap_tasks
                set
                  status = %s,
                  completion_note = %s,
                  completed_at = case when %s = 'completed' then coalesce(completed_at, now()) else null end,
                  updated_at = now()
                where id = %s and roadmap_id = %s
                returning id
                """,
                (task_status, completion_note, task_status, task_id, roadmap_id),
            )
            if not cur.fetchone():
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Roadmap task not found.")
            cur.execute(
                """
                update learning_roadmaps roadmap
                set
                  completed_at = case
                    when exists (
                      select 1 from roadmap_tasks task
                      where task.roadmap_id = roadmap.id
                    )
                    and not exists (
                      select 1 from roadmap_tasks task
                      where task.roadmap_id = roadmap.id and task.status <> 'completed'
                    )
                    then coalesce(roadmap.completed_at, now())
                    else null
                  end,
                  updated_at = now()
                where roadmap.id = %s
                """,
                (roadmap_id,),
            )
        conn.commit()
        with conn.cursor() as cur:
            return _load_roadmap_detail(cur, user_id, roadmap_id)


def build_personalized_tasks(
    analysis: dict[str, Any],
    *,
    language: str = "vi",
) -> list[dict[str, Any]]:
    """Build deterministic tasks from the exact job requirements and CV evidence."""
    role = _target_role(analysis)
    company = str(analysis.get("company_name") or "nhà tuyển dụng").strip()
    gaps = [dict(item) for item in analysis.get("gap_items") or []]
    gaps.sort(
        key=lambda item: (
            0 if str(item.get("status") or "").lower() == "missing" else 1,
            int(item.get("score") or 0),
            str(item.get("skill") or ""),
        )
    )
    fingerprint = str(analysis.get("input_fingerprint") or analysis.get("id"))
    tasks: list[dict[str, Any]] = []
    used_skills: set[str] = set()

    for gap in gaps[:3]:
        skill = _clean(gap.get("skill"), "năng lực trọng yếu")
        requirement = _clean(gap.get("jd_requirement"), _jd_excerpt(analysis))
        evidence = _clean(gap.get("cv_evidence"), "CV chưa có bằng chứng định lượng rõ ràng")
        category = _clean(gap.get("category"), "Năng lực")
        used_skills.add(skill.casefold())
        if language == "en":
            title = f"Close the {skill} gap for {role}"
            description = (
                f"Build one role-specific work sample for {company}. Address this JD requirement: "
                f"“{requirement}”. Current CV evidence: “{evidence}”. Add a measurable result and "
                f"prepare a 2-minute explanation connecting it to the {role} role."
            )
        else:
            title = f"Bù khoảng trống {skill} cho vị trí {role}"
            description = (
                f"Tạo một work sample riêng cho {company}, xử lý đúng yêu cầu JD: “{requirement}”. "
                f"Bằng chứng hiện có trong CV: “{evidence}”. Bổ sung kết quả đo được và chuẩn bị phần "
                f"trình bày 2 phút liên hệ trực tiếp với vị trí {role}."
            )
        tasks.append(
            _task(
                fingerprint,
                f"gap:{category}:{skill}:{requirement}",
                phase=category,
                title=title,
                skill=skill,
                description=description,
                task_type="technical" if "technical" in category.lower() else "performance",
                minutes=90,
                boost=8 if str(gap.get("status")).lower() == "missing" else 5,
                source=gap,
            )
        )

    for skill in _as_list(analysis.get("job_skills")):
        skill_name = _clean(skill)
        if not skill_name or skill_name.casefold() in used_skills:
            continue
        used_skills.add(skill_name.casefold())
        if language == "en":
            title = f"Prove {skill_name} in a {company} scenario"
            description = (
                f"Create a concrete {skill_name} artifact for the {role} role using a scenario from "
                f"this JD: “{_jd_excerpt(analysis)}”. Document the decision, trade-off, and outcome."
            )
        else:
            title = f"Chứng minh {skill_name} trong bối cảnh {company}"
            description = (
                f"Tạo sản phẩm minh chứng cụ thể về {skill_name} cho vị trí {role}, dùng một tình "
                f"huống trong JD này: “{_jd_excerpt(analysis)}”. Ghi rõ quyết định, đánh đổi và kết quả."
            )
        tasks.append(
            _task(
                fingerprint,
                f"skill:{skill_name}:{_jd_excerpt(analysis)}",
                phase="Job-specific proof",
                title=title,
                skill=skill_name,
                description=description,
                task_type="technical",
                minutes=75,
                boost=4,
                source={"job_skill": skill_name},
            )
        )
        if len(tasks) >= 5:
            break

    responsibility = _first_text(analysis.get("key_responsibilities")) or _jd_excerpt(analysis)
    if language == "en":
        star_title = f"Build a {company}-specific STAR story"
        star_description = (
            f"Write and rehearse a STAR story for “{responsibility}”. Use only evidence from this CV, "
            f"quantify the result, and end with why it transfers to the {role} role at {company}."
        )
        mock_title = f"Re-interview checkpoint: {role} at {company}"
        mock_description = (
            f"Run a focused mock interview using this JD and CV. Cover the weakest gaps above, "
            f"the responsibility “{responsibility}”, and record three answers to improve before retrying."
        )
    else:
        star_title = f"Xây STAR story riêng cho {company}"
        star_description = (
            f"Viết và luyện một STAR story cho yêu cầu “{responsibility}”. Chỉ dùng bằng chứng có "
            f"trong CV này, lượng hóa kết quả và kết nối với vị trí {role} tại {company}."
        )
        mock_title = f"Checkpoint phỏng vấn lại: {role} tại {company}"
        mock_description = (
            f"Thực hiện mock interview tập trung đúng CV/JD này: kiểm tra các gap yếu nhất ở trên, "
            f"yêu cầu “{responsibility}”, và ghi lại ba câu trả lời cần cải thiện trước lần phỏng vấn lại."
        )
    tasks.extend(
        [
            _task(
                fingerprint,
                f"star:{responsibility}",
                phase="Interview evidence",
                title=star_title,
                skill="STAR storytelling",
                description=star_description,
                task_type="behavioral",
                minutes=45,
                boost=4,
                source={"responsibility": responsibility},
            ),
            _task(
                fingerprint,
                f"mock:{role}:{company}:{responsibility}",
                phase="Re-interview readiness",
                title=mock_title,
                skill="Interview readiness",
                description=mock_description,
                task_type="performance",
                minutes=45,
                boost=5,
                source={"responsibility": responsibility, "company": company},
            ),
        ]
    )
    return tasks


def _task(
    fingerprint: str,
    basis: str,
    *,
    phase: str,
    title: str,
    skill: str,
    description: str,
    task_type: str,
    minutes: int,
    boost: int,
    source: dict[str, Any],
) -> dict[str, Any]:
    client_key = hashlib.sha256(f"{fingerprint}\x1f{basis}".encode("utf-8")).hexdigest()
    return {
        "phase_name": phase[:255],
        "title": title[:255],
        "skill_key": skill[:255],
        "description": description[:4000],
        "task_type": task_type,
        "duration_minutes": minutes,
        "expected_boost": boost,
        "client_key": client_key,
        "source_gap_json": source,
    }


def _insert_tasks(cur, roadmap_id: str, tasks: list[dict[str, Any]]) -> None:
    for order, task in enumerate(tasks, start=1):
        cur.execute(
            """
            insert into roadmap_tasks (
              roadmap_id, phase_name, task_title, skill_key, description,
              task_type, duration_minutes, expected_boost, client_key,
              source_gap_json, status, display_order
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'planned', %s)
            """,
            (
                roadmap_id,
                task["phase_name"],
                task["title"],
                task["skill_key"],
                task["description"],
                task["task_type"],
                task["duration_minutes"],
                task["expected_boost"],
                task["client_key"],
                Json(task["source_gap_json"]),
                order,
            ),
        )


def _load_analysis(cur, user_id: str, analysis_id: str) -> dict[str, Any]:
    cur.execute(
        """
        select
          analysis.*,
          job.title as job_title,
          job.company_name,
          job.location as job_location,
          job.experience_level,
          job.skills_json as job_skills,
          coalesce(job.description, analysis.jd_text_snapshot) as job_description,
          source.source,
          source.source_url
        from cv_jd_analyses analysis
        left join jobs job on job.id = analysis.job_id
        left join lateral (
          select job_source.source, job_source.source_url
          from job_sources job_source
          where job_source.job_id = job.id
          order by job_source.last_seen_at desc
          limit 1
        ) source on true
        where analysis.id = %s and analysis.user_id = %s
        """,
        (analysis_id, user_id),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CV/JD analysis not found.")
    analysis = dict(row)
    cur.execute(
        """
        select category, skill, status, cv_evidence, jd_requirement, score
        from cv_jd_gap_items
        where analysis_id = %s
        order by id
        """,
        (analysis_id,),
    )
    analysis["gap_items"] = [dict(item) for item in cur.fetchall()]
    return analysis


def _analysis_fingerprint(cur, analysis: dict[str, Any]) -> str:
    fingerprint = analysis.get("input_fingerprint")
    if fingerprint:
        return str(fingerprint)
    raw = analysis.get("llm_raw_json") or {}
    cv_text = analysis.get("cv_text_snapshot") or raw.get("cvText")
    jd_text = (
        analysis.get("jd_text_snapshot")
        or analysis.get("job_description")
        or raw.get("jdText")
    )
    fingerprint = cv_jd_input_fingerprint(cv_text, jd_text)
    if not fingerprint:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This analysis has no complete CV/JD snapshot. Run the CV/JD analysis again.",
        )
    cur.execute(
        """
        update cv_jd_analyses
        set cv_text_snapshot = coalesce(cv_text_snapshot, %s),
            jd_text_snapshot = coalesce(jd_text_snapshot, %s),
            input_fingerprint = %s
        where id = %s
        """,
        (cv_text, jd_text, fingerprint, analysis["id"]),
    )
    analysis["input_fingerprint"] = fingerprint
    return fingerprint


def _load_roadmap_detail(cur, user_id: str, roadmap_id: str) -> dict[str, Any]:
    cur.execute(
        """
        select
          roadmap.*,
          job.title as job_title,
          job.company_name,
          job.location as job_location,
          job.experience_level,
          job.skills_json as job_skills,
          coalesce(job.description, analysis.jd_text_snapshot) as job_description,
          source.source,
          source.source_url
        from learning_roadmaps roadmap
        left join cv_jd_analyses analysis on analysis.id = roadmap.cv_jd_analysis_id
        left join jobs job on job.id = roadmap.job_id
        left join lateral (
          select job_source.source, job_source.source_url
          from job_sources job_source
          where job_source.job_id = job.id
          order by job_source.last_seen_at desc
          limit 1
        ) source on true
        where roadmap.id = %s and roadmap.user_id = %s
        """,
        (roadmap_id, user_id),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Roadmap not found.")
    roadmap = dict(row)
    cur.execute(
        """
        select *
        from roadmap_tasks
        where roadmap_id = %s
        order by display_order, id
        """,
        (roadmap_id,),
    )
    tasks = [dict(task) for task in cur.fetchall()]
    completed = sum(1 for task in tasks if task["status"] == "completed")
    total = len(tasks)
    roadmap["tasks"] = tasks
    roadmap["total_tasks"] = total
    roadmap["completed_tasks"] = completed
    roadmap["progress_percent"] = round(completed * 100 / total) if total else 0
    roadmap["offer_reinterview"] = total > 0 and completed == total
    roadmap["job"] = {
        "id": str(roadmap.get("job_id") or roadmap["cv_jd_analysis_id"]),
        "title": roadmap.get("job_title") or roadmap["target_role"],
        "companyName": roadmap.get("company_name") or "Custom JD",
        "location": roadmap.get("job_location") or "",
        "experienceLevel": roadmap.get("experience_level") or "",
        "skills": _as_list(roadmap.get("job_skills")),
        "jobDescription": roadmap.get("job_description") or "",
        "source": roadmap.get("source") or "analysis_snapshot",
        "sourceUrl": roadmap.get("source_url"),
    }
    return roadmap


def _roadmap_summary(row: dict[str, Any]) -> dict[str, Any]:
    total = int(row.get("total_tasks") or 0)
    completed = int(row.get("completed_tasks") or 0)
    return {
        **row,
        "progress_percent": round(completed * 100 / total) if total else 0,
        "offer_reinterview": total > 0 and completed == total,
    }


def _require_user_id(cur, external_auth_id: str) -> str:
    cur.execute("select id from users where external_auth_id = %s", (external_auth_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")
    return str(row["id"])


def _require_roadmap(cur, user_id: str, roadmap_id: str) -> None:
    cur.execute(
        "select id from learning_roadmaps where id = %s and user_id = %s",
        (roadmap_id, user_id),
    )
    if not cur.fetchone():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Roadmap not found.")


def _roadmap_copy(analysis: dict[str, Any], language: str) -> tuple[str, str]:
    role = _target_role(analysis)
    company = _clean(analysis.get("company_name"), "nhà tuyển dụng")
    if language == "en":
        return (
            f"{role} at {company}",
            f"A roadmap generated only for this CV and the real {company} job description.",
        )
    return (
        f"{role} tại {company}",
        f"Lộ trình riêng cho CV này và JD thực tế của {company}.",
    )


def _target_role(analysis: dict[str, Any]) -> str:
    return _clean(
        analysis.get("job_title") or analysis.get("target_position"),
        "Vị trí mục tiêu",
    )


def _jd_excerpt(analysis: dict[str, Any]) -> str:
    text = _clean(
        analysis.get("jd_text_snapshot") or analysis.get("job_description"),
        "yêu cầu trọng tâm của JD",
    )
    return text[:280].rstrip()


def _first_text(value: Any) -> str:
    for item in _as_list(value):
        if isinstance(item, dict):
            candidate = item.get("responsibility") or item.get("text") or item.get("title")
        else:
            candidate = item
        cleaned = _clean(candidate)
        if cleaned:
            return cleaned[:500]
    return ""


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean(value: Any, fallback: str = "") -> str:
    return " ".join(str(value or fallback).split()).strip()
