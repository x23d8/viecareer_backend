"""Search, lightweight recommendation, and persistence for the job marketplace."""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from psycopg2.extras import Json

from core.database import get_connection
from schemas.jobs import JobIngestionPayload

logger = logging.getLogger("viecareer")

_TOKEN_RE = re.compile(r"[a-z0-9+#.]{2,}")
_SOURCE_PRIORITY = {"viecareer": 0, "itviec": 1, "topcv": 2, "linkedin": 3}
_QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "data analyst": ("data analysis", "bi analyst", "business intelligence", "phan tich du lieu"),
    "business analyst": ("business analysis", "ba", "phan tich nghiep vu"),
    "ai engineer": ("artificial intelligence engineer", "machine learning engineer", "ky su ai"),
    "data scientist": ("data science", "machine learning scientist", "khoa hoc du lieu"),
    "software engineer": ("software developer", "lap trinh vien", "ky su phan mem"),
    "backend": ("back end", "backend developer", "backend engineer"),
    "frontend": ("front end", "frontend developer", "frontend engineer"),
    "marketing": ("digital marketing", "growth marketing", "tiep thi"),
}
_SKILL_ALIASES: dict[str, str] = {
    "amazon web services": "aws",
    "aws cloud": "aws",
    "google cloud platform": "gcp",
    "google cloud": "gcp",
    "microsoft azure": "azure",
    "javascript": "javascript",
    "js": "javascript",
    "typescript": "typescript",
    "ts": "typescript",
    "reactjs": "react",
    "react.js": "react",
    "nextjs": "next.js",
    "nodejs": "node.js",
    "node": "node.js",
    "vuejs": "vue",
    "vue.js": "vue",
    "postgres": "postgresql",
    "mongo": "mongodb",
    "k8s": "kubernetes",
    "machine learning": "machine learning",
    "ml": "machine learning",
    "artificial intelligence": "ai",
    "generative ai": "genai",
    "large language model": "llm",
    "large language models": "llm",
    "natural language processing": "nlp",
    "continuous integration": "ci/cd",
    "continuous delivery": "ci/cd",
}
_EXPERIENCE_LEVEL_ORDER = {
    "internship": 0,
    "fresher": 1,
    "junior": 2,
    "middle": 3,
    "mid": 3,
    "senior": 4,
    "lead": 5,
    "manager": 6,
}

_JOB_SELECT = """
select
  j.*,
  coalesce(
    (
      select jsonb_agg(
        jsonb_build_object(
          'source', js.source,
          'source_url', js.source_url,
          'apply_url', coalesce(js.apply_url, js.source_url),
          'last_seen_at', js.last_seen_at
        )
        order by js.last_seen_at desc
      )
      from job_sources js
      where js.job_id = j.id and js.is_active = true
    ),
    '[]'::jsonb
  ) as sources
from jobs j
"""


def search_jobs(
    *,
    external_auth_id: str | None,
    query: str | None,
    location: str | None,
    workplace_types: list[str],
    employment_types: list[str],
    experience_levels: list[str],
    experience_years_min: int | None,
    experience_years_max: int | None,
    sources: list[str],
    posted_within_days: int | None,
    sort: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                user_context = _load_user_context(cur, external_auth_id) if external_auth_id else None
                clauses = [
                    "j.is_active = true",
                    "(j.expires_at is null or j.expires_at > now())",
                    "exists (select 1 from job_sources active_js where active_js.job_id = j.id and active_js.is_active = true)",
                ]
                params: list[Any] = []
                if user_context:
                    clauses.append(
                        "not exists (select 1 from job_interactions dismissed where dismissed.user_id = %s and dismissed.job_id = j.id and dismissed.event_type = 'dismiss')"
                    )
                    params.append(user_context["user_id"])

                normalized_query = " ".join((query or "").split())
                normalized_location = " ".join((location or "").split())
                if normalized_query:
                    normalized_search = _normalize_text(normalized_query)
                    tsquery = _related_tsquery(normalized_search)
                    clauses.append(
                        "(to_tsvector('simple', j.search_text) @@ to_tsquery('simple', %s) "
                        "or similarity(j.normalized_title, %s) >= 0.24 "
                        "or j.search_text like %s)"
                    )
                    params.extend([tsquery, normalized_search, f"%{normalized_search}%"])
                if normalized_location:
                    clauses.append("lower(unaccent(coalesce(j.location, ''))) like %s")
                    params.append(f"%{_normalize_location(normalized_location)}%")
                if workplace_types:
                    clauses.append("j.workplace_type = any(%s)")
                    params.append(workplace_types)
                if employment_types:
                    clauses.append("j.employment_type = any(%s)")
                    params.append(employment_types)
                if experience_levels:
                    clauses.append("j.experience_level = any(%s)")
                    params.append(experience_levels)
                if experience_years_min is not None:
                    clauses.append("j.experience_years_min >= %s")
                    params.append(experience_years_min)
                if experience_years_max is not None:
                    clauses.append("j.experience_years_min <= %s")
                    params.append(experience_years_max)
                if sources:
                    clauses.append(
                        "exists (select 1 from job_sources source_js where source_js.job_id = j.id and source_js.is_active = true and source_js.source = any(%s))"
                    )
                    params.append(sources)
                if posted_within_days:
                    clauses.append("coalesce(j.posted_at, j.first_seen_at) >= %s")
                    params.append(datetime.now(timezone.utc) - timedelta(days=posted_within_days))

                where_sql = " and ".join(clauses)
                cur.execute(f"select count(*)::int as total from jobs j where {where_sql}", params)
                total = int((cur.fetchone() or {}).get("total") or 0)

                offset = (page - 1) * page_size
                if sort == "newest":
                    page_clause = "limit %s offset %s"
                    select_params = [*params, page_size, offset]
                else:
                    page_clause = "limit 500"
                    select_params = params
                cur.execute(
                    f"""
                    {_JOB_SELECT}
                    where {where_sql}
                    order by coalesce(j.posted_at, j.first_seen_at) desc, j.updated_at desc
                    {page_clause}
                    """,
                    select_params,
                )
                rows = cur.fetchall()

                states = _load_interaction_states(cur, user_context["user_id"], rows) if user_context else {}
                ranked: list[dict[str, Any]] = []
                for row in rows:
                    state = states.get(str(row["id"]), {})
                    if state.get("dismissed"):
                        continue
                    score, reasons = _recommendation_score(
                        row,
                        user_context=user_context,
                        query=normalized_query,
                        location=normalized_location,
                    )
                    ranked.append(
                        _job_payload(
                            row,
                            saved=bool(state.get("saved")),
                            match_score=score,
                            match_reasons=reasons,
                        )
                    )

                if sort == "recommended":
                    ranked.sort(
                        key=lambda item: (item["match_score"], item.get("posted_at") or item.get("first_seen_at") or ""),
                        reverse=True,
                    )

                if sort == "recommended":
                    items = ranked[offset : offset + page_size]
                    effective_total = min(total, 500)
                else:
                    items = ranked
                    effective_total = total

                if user_context and (
                    normalized_query
                    or normalized_location
                    or workplace_types
                    or employment_types
                    or experience_levels
                    or experience_years_min is not None
                    or experience_years_max is not None
                    or sources
                ):
                    _record_search(
                        cur,
                        user_id=user_context["user_id"],
                        query=normalized_query,
                        location=normalized_location,
                        filters={
                            "workplace_types": workplace_types,
                            "employment_types": employment_types,
                            "experience_levels": experience_levels,
                            "experience_years_min": experience_years_min,
                            "experience_years_max": experience_years_max,
                            "sources": sources,
                            "posted_within_days": posted_within_days,
                        },
                    )
                    conn.commit()

        return {
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": effective_total,
                "has_more": offset + page_size < effective_total,
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not search jobs")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load jobs. Apply the job-market migration and try again.",
        ) from exc


def get_job(*, job_id: str, external_auth_id: str | None) -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"{_JOB_SELECT} where j.id = %s and j.is_active = true and (j.expires_at is null or j.expires_at > now())",
                    (job_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Job not found.")
                user_context = _load_user_context(cur, external_auth_id) if external_auth_id else None
                states = _load_interaction_states(cur, user_context["user_id"], [row]) if user_context else {}
                state = states.get(str(row["id"]), {})
                score, reasons = _recommendation_score(row, user_context=user_context, query="", location="")
                return _job_payload(
                    row,
                    saved=bool(state.get("saved")),
                    match_score=score,
                    match_reasons=reasons,
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not load job=%s", job_id)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not load this job.") from exc


def find_cv_job_matches(
    *,
    external_auth_id: str,
    target_position: str | None,
    candidate_skills: list[str],
    cv_text: str | None,
    limit: int,
) -> dict[str, Any]:
    """Return explainable CV/JD matches, keeping fit and evidence confidence separate."""

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                stored_context = _load_user_context(cur, external_auth_id) or {}
                skills = _dedupe_skills([*candidate_skills, *stored_context.get("skills", [])])
                target = (target_position or stored_context.get("target_position") or "").strip()
                candidate_years = _infer_candidate_experience_years(cv_text or "")
                candidate_level = _infer_candidate_experience_level("", cv_text or "")
                profile_ready = bool(skills or target or candidate_years is not None or candidate_level)

                cur.execute(
                    f"""
                    {_JOB_SELECT}
                    where j.is_active = true
                      and (j.expires_at is null or j.expires_at > now())
                      and exists (
                        select 1 from job_sources active_js
                        where active_js.job_id = j.id and active_js.is_active = true
                      )
                    order by coalesce(j.posted_at, j.first_seen_at) desc, j.updated_at desc
                    limit 500
                    """
                )
                rows = cur.fetchall()
                user_id = stored_context.get("user_id")
                states = _load_interaction_states(cur, user_id, rows) if user_id else {}

                ranked: list[dict[str, Any]] = []
                for row in rows:
                    state = states.get(str(row["id"]), {})
                    if state.get("dismissed"):
                        continue
                    match = _score_cv_job_match(
                        row,
                        candidate_skills=skills,
                        target_position=target,
                        candidate_years=candidate_years,
                        candidate_level=candidate_level,
                    )
                    job = _job_payload(
                        row,
                        saved=bool(state.get("saved")),
                        match_score=match["fit_score"],
                        match_reasons=_cv_match_reasons(match),
                    )
                    ranked.append({**job, "cv_match": match})

                ranked.sort(
                    key=lambda item: (
                        item["cv_match"]["ranking_score"],
                        item["cv_match"]["confidence_score"],
                        item.get("posted_at") or item.get("first_seen_at") or "",
                    ),
                    reverse=True,
                )

        profile_completeness = round(
            min(55, len(skills) * 8)
            + (25 if target else 0)
            + (20 if candidate_years is not None or candidate_level else 0)
        )
        return {
            "profile": {
                "ready": profile_ready,
                "source": "current_cv" if candidate_skills or cv_text or target_position else "saved_profile",
                "target_position": target or None,
                "skills": skills[:30],
                "skills_count": len(skills),
                "experience_years": candidate_years,
                "experience_level": candidate_level,
                "completeness_score": min(100, profile_completeness),
            },
            "matches": ranked[:limit] if profile_ready else [],
            "evaluated_jobs": len(ranked),
            "method": {
                "fit": "Kỹ năng 60% · kinh nghiệm 25% · vị trí 15% (tự phân bổ khi JD thiếu dữ liệu)",
                "confidence": "Độ đầy đủ của bằng chứng CV và JD; không phải xác suất được tuyển",
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not rank CV job matches user=%s", external_auth_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not analyze the CV against active jobs.",
        ) from exc


def get_job_practice_context(*, job_id: str, external_auth_id: str) -> dict[str, Any]:
    """Return the authoritative JD snapshot used to start a job-specific interview flow."""

    job = get_job(job_id=job_id, external_auth_id=external_auth_id)
    description = "\n".join(
        line.strip() for line in str(job.get("description") or "").splitlines() if line.strip()
    )
    if len(description) < 120:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This job does not contain enough JD content for interview practice.",
        )

    primary_source = str(job.get("primary_source") or "viecareer")
    source_link = next(
        (
            source
            for source in job.get("sources") or []
            if str(source.get("source") or "") == primary_source
        ),
        (job.get("sources") or [{}])[0] if job.get("sources") else {},
    )
    return {
        "job_id": job["id"],
        "title": job["title"],
        "company_name": job["company_name"],
        "location": job.get("location"),
        "experience_level": job.get("experience_level"),
        "skills": [str(skill) for skill in (job.get("skills") or [])][:100],
        "job_description": description[:50_000],
        "source": primary_source,
        "source_url": source_link.get("source_url") or job.get("apply_url"),
        "last_seen_at": job.get("last_seen_at"),
    }


def get_job_match_context(*, job_id: str, external_auth_id: str) -> dict[str, Any]:
    """Load only the profile/job signals needed for a short on-demand AI explanation."""

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"{_JOB_SELECT} where j.id = %s and j.is_active = true and (j.expires_at is null or j.expires_at > now())",
                    (job_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Job not found.")
                user_context = _load_user_context(cur, external_auth_id)
                if not user_context:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND,
                        detail="Your VieCareer profile has not been synced yet.",
                    )

                score, reasons = _recommendation_score(row, user_context=user_context, query="", location="")
                job_skills = [str(skill) for skill in (row.get("skills_json") or [])]
                user_skills = [str(skill) for skill in user_context.get("skills", [])]
                user_skill_keys = {skill.casefold() for skill in user_skills}
                matched_skills = [skill for skill in job_skills if skill.casefold() in user_skill_keys]
                missing_skills = [skill for skill in job_skills if skill.casefold() not in user_skill_keys]

                return {
                    "job_id": str(row["id"]),
                    "title": row["title"],
                    "company_name": row["company_name"],
                    "location": row.get("location"),
                    "workplace_type": row.get("workplace_type"),
                    "employment_type": row.get("employment_type"),
                    "experience_level": row.get("experience_level"),
                    "job_skills": job_skills[:30],
                    "job_description": (row.get("description") or "")[:10_000],
                    "match_score": score,
                    "match_reasons": reasons,
                    "profile": {
                        "target_position": user_context.get("target_position"),
                        "desired_titles": user_context.get("desired_titles", [])[:10],
                        "skills": user_skills[:40],
                        "locations": user_context.get("locations", [])[:10],
                        "workplace_types": user_context.get("workplace_types", []),
                        "employment_types": user_context.get("employment_types", []),
                    },
                    "matched_skills": matched_skills[:10],
                    "missing_skills": missing_skills[:10],
                }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not build match context job=%s user=%s", job_id, external_auth_id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not prepare the job match analysis.",
        ) from exc


def record_job_interaction(
    *,
    external_auth_id: str,
    job_id: str,
    event_type: str,
    context: str | None,
    source: str | None,
) -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                user_id = _require_user_id(cur, external_auth_id)
                cur.execute(
                    """
                    select id, title, company_name
                    from jobs
                    where id = %s and is_active = true and (expires_at is null or expires_at > now())
                    """,
                    (job_id,),
                )
                job = cur.fetchone()
                if not job:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Job not found.")
                cur.execute(
                    """
                    insert into job_interactions (user_id, job_id, event_type, context, source)
                    values (%s, %s, %s, %s, %s)
                    returning id, event_type, created_at
                    """,
                    (user_id, job_id, event_type, context, source),
                )
                row = cur.fetchone()
                if event_type == "apply":
                    cur.execute(
                        """
                        insert into job_applications (
                          user_id, job_id, job_title, company, status, applied_at, source, apply_url, updated_at
                        )
                        values (
                          %s, %s, %s, %s, 'applied', now(), %s,
                          (select coalesce(apply_url, source_url) from job_sources where job_id = %s and is_active order by last_seen_at desc limit 1),
                          now()
                        )
                        on conflict (user_id, job_id) where job_id is not null do update set
                          status = 'applied',
                          applied_at = coalesce(job_applications.applied_at, now()),
                          source = coalesce(excluded.source, job_applications.source),
                          apply_url = coalesce(excluded.apply_url, job_applications.apply_url),
                          updated_at = now()
                        """,
                        (user_id, job_id, job["title"], job["company_name"], source, job_id),
                    )
            conn.commit()
        return {
            "id": row["id"],
            "job_id": job_id,
            "event_type": row["event_type"],
            "created_at": _iso(row["created_at"]),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not record job interaction user=%s job=%s", external_auth_id, job_id)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not update this job.") from exc


def list_saved_jobs(*, external_auth_id: str, limit: int) -> list[dict[str, Any]]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                user_id = _require_user_id(cur, external_auth_id)
                cur.execute(
                    f"""
                    {_JOB_SELECT}
                    join lateral (
                      select ji.event_type
                      from job_interactions ji
                      where ji.user_id = %s
                        and ji.job_id = j.id
                        and ji.event_type in ('save', 'unsave')
                      order by ji.created_at desc, ji.id desc
                      limit 1
                    ) saved_state on saved_state.event_type = 'save'
                    where j.is_active = true and (j.expires_at is null or j.expires_at > now())
                    order by j.updated_at desc
                    limit %s
                    """,
                    (user_id, limit),
                )
                rows = cur.fetchall()
                user_context = _load_user_context(cur, external_auth_id)
                items = []
                for row in rows:
                    score, reasons = _recommendation_score(row, user_context=user_context, query="", location="")
                    items.append(_job_payload(row, saved=True, match_score=score, match_reasons=reasons))
                return items
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not list saved jobs user=%s", external_auth_id)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not load saved jobs.") from exc


def get_job_preferences(*, external_auth_id: str) -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                user_id = _require_user_id(cur, external_auth_id)
                cur.execute("select * from user_job_preferences where user_id = %s", (user_id,))
                row = cur.fetchone()
        return _preferences_payload(row)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not load job preferences user=%s", external_auth_id)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not load preferences.") from exc


def update_job_preferences(
    *,
    external_auth_id: str,
    desired_titles: list[str],
    locations: list[str],
    workplace_types: list[str],
    employment_types: list[str],
) -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                user_id = _require_user_id(cur, external_auth_id)
                cur.execute(
                    """
                    insert into user_job_preferences (
                      user_id, desired_titles_json, locations_json, workplace_types_json, employment_types_json, updated_at
                    )
                    values (%s, %s, %s, %s, %s, now())
                    on conflict (user_id) do update set
                      desired_titles_json = excluded.desired_titles_json,
                      locations_json = excluded.locations_json,
                      workplace_types_json = excluded.workplace_types_json,
                      employment_types_json = excluded.employment_types_json,
                      updated_at = now()
                    returning *
                    """,
                    (
                        user_id,
                        Json(desired_titles),
                        Json(locations),
                        Json(workplace_types),
                        Json(employment_types),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return _preferences_payload(row)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not update job preferences user=%s", external_auth_id)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not save preferences.") from exc


def list_recent_searches(*, external_auth_id: str, limit: int) -> list[dict[str, Any]]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                user_id = _require_user_id(cur, external_auth_id)
                cur.execute(
                    """
                    select id, query, location, filters_json, searched_at
                    from job_search_history
                    where user_id = %s
                    order by searched_at desc
                    limit %s
                    """,
                    (user_id, limit),
                )
                return [
                    {
                        "id": row["id"],
                        "query": row["query"],
                        "location": row.get("location"),
                        "filters": row.get("filters_json") or {},
                        "searched_at": _iso(row.get("searched_at")),
                    }
                    for row in cur.fetchall()
                ]
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not list recent job searches user=%s", external_auth_id)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not load recent searches.") from exc


def clear_recent_searches(*, external_auth_id: str) -> None:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                user_id = _require_user_id(cur, external_auth_id)
                cur.execute("delete from job_search_history where user_id = %s", (user_id,))
            conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not clear recent job searches user=%s", external_auth_id)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not clear recent searches.") from exc


def get_market_insights() -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select
                      count(*) filter (where is_active and (expires_at is null or expires_at > now()))::int as active_jobs,
                      count(*) filter (
                        where is_active and (expires_at is null or expires_at > now())
                          and coalesce(posted_at, first_seen_at) >= now() - interval '7 days'
                      )::int as new_jobs_7d
                    from jobs
                    """
                )
                totals = cur.fetchone() or {}
                cur.execute(
                    """
                    select js.source, count(distinct js.job_id)::int as job_count
                    from job_sources js
                    join jobs j on j.id = js.job_id
                    where js.is_active and j.is_active and (j.expires_at is null or j.expires_at > now())
                    group by js.source
                    order by job_count desc
                    """
                )
                source_counts = cur.fetchall()
                cur.execute(
                    """
                    select skill.value as skill, count(*)::int as job_count
                    from jobs j
                    cross join lateral jsonb_array_elements_text(j.skills_json) as skill(value)
                    where j.is_active and (j.expires_at is null or j.expires_at > now())
                    group by skill.value
                    order by job_count desc, skill.value
                    limit 8
                    """
                )
                top_skills = cur.fetchall()
        return {
            "active_jobs": int(totals.get("active_jobs") or 0),
            "new_jobs_7d": int(totals.get("new_jobs_7d") or 0),
            "sources": source_counts,
            "top_skills": top_skills,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.exception("Could not load job market insights")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not load market insights.") from exc


def upsert_ingested_job(*, source: str, payload: JobIngestionPayload) -> str:
    normalized_title = _normalize_text(payload.title)
    search_text = _normalize_text(
        " ".join(
            [
                payload.title,
                payload.company_name,
                payload.location or "",
                payload.description,
                " ".join(payload.skills),
            ]
        )
    )
    fingerprint = _sha256(
        "|".join(
            [
                normalized_title,
                _normalize_text(payload.company_name),
                _normalize_location(payload.location or ""),
            ]
        )
    )
    content_hash = _sha256(
        "|".join(
            [
                payload.title,
                payload.company_name,
                payload.location or "",
                payload.description,
                ",".join(payload.skills),
            ]
        )
    )
    posted_at = _parse_datetime(payload.posted_at)
    expires_at = _parse_datetime(payload.expires_at)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into jobs (
                  fingerprint, title, normalized_title, company_name, company_logo_url, location,
                  workplace_type, employment_type, experience_level, experience_years_min,
                  experience_years_max, search_text, salary_min, salary_max, salary_currency,
                  salary_text, description, skills_json, posted_at, expires_at,
                  last_seen_at, is_active, updated_at
                )
                values (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  now(), true, now()
                )
                on conflict (fingerprint) do update set
                  title = excluded.title,
                  normalized_title = excluded.normalized_title,
                  company_name = excluded.company_name,
                  company_logo_url = coalesce(excluded.company_logo_url, jobs.company_logo_url),
                  location = coalesce(excluded.location, jobs.location),
                  workplace_type = coalesce(excluded.workplace_type, jobs.workplace_type),
                  employment_type = coalesce(excluded.employment_type, jobs.employment_type),
                  experience_level = coalesce(excluded.experience_level, jobs.experience_level),
                  experience_years_min = coalesce(excluded.experience_years_min, jobs.experience_years_min),
                  experience_years_max = coalesce(excluded.experience_years_max, jobs.experience_years_max),
                  search_text = excluded.search_text,
                  salary_min = coalesce(excluded.salary_min, jobs.salary_min),
                  salary_max = coalesce(excluded.salary_max, jobs.salary_max),
                  salary_currency = coalesce(excluded.salary_currency, jobs.salary_currency),
                  salary_text = coalesce(excluded.salary_text, jobs.salary_text),
                  description = case when length(excluded.description) > length(jobs.description) then excluded.description else jobs.description end,
                  skills_json = case when jsonb_array_length(excluded.skills_json) > 0 then excluded.skills_json else jobs.skills_json end,
                  posted_at = coalesce(excluded.posted_at, jobs.posted_at),
                  expires_at = coalesce(excluded.expires_at, jobs.expires_at),
                  last_seen_at = now(),
                  is_active = true,
                  updated_at = now()
                returning id
                """,
                (
                    fingerprint,
                    payload.title,
                    normalized_title,
                    payload.company_name,
                    payload.company_logo_url,
                    payload.location,
                    payload.workplace_type,
                    payload.employment_type,
                    payload.experience_level,
                    payload.experience_years_min,
                    payload.experience_years_max,
                    search_text,
                    payload.salary_min,
                    payload.salary_max,
                    payload.salary_currency,
                    payload.salary_text,
                    payload.description,
                    Json(payload.skills),
                    posted_at,
                    expires_at,
                ),
            )
            job_id = str(cur.fetchone()["id"])
            cur.execute(
                """
                insert into job_sources (
                  job_id, source, external_id, source_url, apply_url, content_hash,
                  metadata_json, last_seen_at, is_active, updated_at
                )
                values (%s, %s, %s, %s, %s, %s, %s, now(), true, now())
                on conflict (source, external_id) do update set
                  job_id = excluded.job_id,
                  source_url = excluded.source_url,
                  apply_url = excluded.apply_url,
                  content_hash = excluded.content_hash,
                  metadata_json = excluded.metadata_json,
                  last_seen_at = now(),
                  is_active = true,
                  updated_at = now()
                """,
                (
                    job_id,
                    source,
                    payload.external_id,
                    payload.source_url,
                    payload.apply_url,
                    content_hash,
                    Json(payload.metadata),
                ),
            )
        conn.commit()
    return job_id


def _load_user_context(cur, external_auth_id: str | None) -> dict[str, Any] | None:
    if not external_auth_id:
        return None
    cur.execute(
        """
        select u.id as user_id, p.target_position, p.location,
               prefs.desired_titles_json, prefs.locations_json,
               prefs.workplace_types_json, prefs.employment_types_json
        from users u
        left join user_profiles p on p.user_id = u.id
        left join user_job_preferences prefs on prefs.user_id = u.id
        where u.external_auth_id = %s
        limit 1
        """,
        (external_auth_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cur.execute("select skill_name from user_skills where user_id = %s", (row["user_id"],))
    skills = [item["skill_name"] for item in cur.fetchall()]
    cur.execute(
        """
        select target_position, tech_stack_json
        from cv_jd_analyses
        where user_id = %s
          and coalesce(llm_raw_json->>'synthetic', 'false') <> 'true'
        order by created_at desc
        limit 1
        """,
        (row["user_id"],),
    )
    analysis = cur.fetchone() or {}
    return {
        "user_id": row["user_id"],
        "target_position": analysis.get("target_position") or row.get("target_position"),
        "desired_titles": row.get("desired_titles_json") or [],
        "locations": row.get("locations_json") or ([row["location"]] if row.get("location") else []),
        "workplace_types": row.get("workplace_types_json") or [],
        "employment_types": row.get("employment_types_json") or [],
        "skills": [*skills, *(analysis.get("tech_stack_json") or [])],
    }


def _load_interaction_states(cur, user_id: Any, rows: list[dict[str, Any]]) -> dict[str, dict[str, bool]]:
    job_ids = [row["id"] for row in rows]
    if not job_ids:
        return {}
    states: dict[str, dict[str, bool]] = {}
    cur.execute(
        """
        select distinct on (job_id) job_id, event_type
        from job_interactions
        where user_id = %s and job_id = any(%s::uuid[]) and event_type in ('save', 'unsave')
        order by job_id, created_at desc, id desc
        """,
        (user_id, job_ids),
    )
    for row in cur.fetchall():
        states.setdefault(str(row["job_id"]), {})["saved"] = row["event_type"] == "save"
    cur.execute(
        """
        select distinct job_id
        from job_interactions
        where user_id = %s and job_id = any(%s::uuid[]) and event_type = 'dismiss'
        """,
        (user_id, job_ids),
    )
    for row in cur.fetchall():
        states.setdefault(str(row["job_id"]), {})["dismissed"] = True
    return states


def _recommendation_score(
    row: dict[str, Any],
    *,
    user_context: dict[str, Any] | None,
    query: str,
    location: str,
) -> tuple[int, list[str]]:
    score = 20.0
    reasons: list[str] = []
    title_tokens = _tokens(row.get("normalized_title") or row.get("title") or "")
    job_skills = {skill.casefold() for skill in (row.get("skills_json") or [])}

    if query:
        normalized_query = _normalize_text(query)
        related_terms = _expanded_query_terms(normalized_query)
        exact_title_match = normalized_query in _normalize_text(row.get("title") or "")
        related_title_match = any(term in _normalize_text(row.get("title") or "") for term in related_terms)
        overlap = title_tokens & set().union(*(_tokens(term) for term in related_terms))
        if exact_title_match:
            score += 30
            reasons.append("Tiêu đề khớp chính xác với tìm kiếm")
        elif related_title_match:
            score += 22
            reasons.append("Vị trí liên quan gần với tìm kiếm")
        if overlap:
            score += min(18, 5 + len(overlap) * 4)
            if not exact_title_match and not related_title_match:
                reasons.append("Tiêu đề phù hợp với tìm kiếm của bạn")
    if location and location.casefold() in (row.get("location") or "").casefold():
        score += 10
        reasons.append("Đúng khu vực bạn đang tìm")

    if user_context:
        desired = [*user_context.get("desired_titles", [])]
        if user_context.get("target_position"):
            desired.append(user_context["target_position"])
        desired_tokens = _tokens(" ".join(desired))
        title_overlap = title_tokens & desired_tokens
        if title_overlap:
            score += min(28, 10 + len(title_overlap) * 6)
            reasons.append("Gần với vị trí nghề nghiệp mục tiêu")

        user_skills = {str(skill).casefold() for skill in user_context.get("skills", [])}
        matching_skills = sorted(job_skills & user_skills)
        if matching_skills:
            score += min(25, 8 + len(matching_skills) * 4)
            reasons.append(f"Khớp kỹ năng: {', '.join(matching_skills[:3])}")

        preferred_locations = user_context.get("locations", [])
        if any(value.casefold() in (row.get("location") or "").casefold() for value in preferred_locations):
            score += 9
            reasons.append("Phù hợp địa điểm ưu tiên")
        if row.get("workplace_type") in user_context.get("workplace_types", []):
            score += 5
            reasons.append("Phù hợp hình thức làm việc")
        if row.get("employment_type") in user_context.get("employment_types", []):
            score += 5
            reasons.append("Phù hợp loại hợp đồng")

    posted_at = row.get("posted_at") or row.get("first_seen_at")
    if isinstance(posted_at, datetime):
        age_days = max(0, (datetime.now(timezone.utc) - posted_at.astimezone(timezone.utc)).days)
        freshness = max(0, 12 - age_days * 0.6)
        score += freshness
        if age_days <= 3:
            reasons.append("Tin mới cập nhật")

    if not reasons:
        reasons.append("Tin tuyển dụng đang hoạt động")
    return min(99, max(1, round(score))), reasons[:3]


def _score_cv_job_match(
    row: dict[str, Any],
    *,
    candidate_skills: list[str],
    target_position: str,
    candidate_years: int | None,
    candidate_level: str | None,
) -> dict[str, Any]:
    job_skills = [str(value).strip() for value in (row.get("skills_json") or []) if str(value).strip()]
    skill_score, matched_skills, partial_skills, missing_skills = _skill_fit(candidate_skills, job_skills)
    role_score = _role_fit(target_position, str(row.get("title") or ""))
    experience_score, experience_fit = _experience_fit(
        candidate_years=candidate_years,
        candidate_level=candidate_level,
        job_min=row.get("experience_years_min"),
        job_max=row.get("experience_years_max"),
        job_level=row.get("experience_level"),
    )

    dimensions: list[tuple[float, float]] = []
    if skill_score is not None:
        dimensions.append((skill_score, 0.60))
    if experience_score is not None:
        dimensions.append((experience_score, 0.25))
    if role_score is not None:
        dimensions.append((role_score, 0.15))
    weight_total = sum(weight for _, weight in dimensions)
    fit_score = round(sum(score * weight for score, weight in dimensions) / weight_total) if weight_total else 0

    candidate_skill_coverage = min(1.0, len(candidate_skills) / 6)
    job_skill_coverage = min(1.0, len(job_skills) / 5)
    skill_evidence = candidate_skill_coverage * job_skill_coverage
    role_evidence = 1.0 if target_position and row.get("title") else 0.0
    has_candidate_experience = candidate_years is not None or candidate_level is not None
    has_job_experience = (
        row.get("experience_years_min") is not None
        or row.get("experience_years_max") is not None
        or bool(row.get("experience_level"))
    )
    experience_evidence = 1.0 if has_candidate_experience and has_job_experience else 0.0
    confidence_score = round(65 * skill_evidence + 20 * experience_evidence + 15 * role_evidence)
    confidence_level = "high" if confidence_score >= 75 else "medium" if confidence_score >= 50 else "low"
    ranking_score = round(fit_score * (0.65 + 0.35 * confidence_score / 100))

    return {
        "fit_score": min(100, max(0, fit_score)),
        "ranking_score": min(100, max(0, ranking_score)),
        "confidence_score": min(100, max(0, confidence_score)),
        "confidence_level": confidence_level,
        "skill_score": skill_score,
        "experience_score": experience_score,
        "role_score": role_score,
        "experience_fit": experience_fit,
        "matched_skills": matched_skills[:8],
        "partial_skills": partial_skills[:5],
        "missing_skills": missing_skills[:8],
        "evidence": {
            "candidate_skills_count": len(candidate_skills),
            "job_skills_count": len(job_skills),
            "candidate_experience_years": candidate_years,
            "job_experience_years_min": row.get("experience_years_min"),
            "job_experience_years_max": row.get("experience_years_max"),
        },
    }


def _skill_fit(
    candidate_skills: list[str],
    job_skills: list[str],
) -> tuple[int | None, list[str], list[str], list[str]]:
    if not job_skills:
        return None, [], [], []
    candidate_canonical = {_canonical_skill(skill): skill for skill in candidate_skills if _canonical_skill(skill)}
    matched: list[str] = []
    partial: list[str] = []
    missing: list[str] = []
    earned = 0.0
    for job_skill in job_skills:
        canonical = _canonical_skill(job_skill)
        if canonical in candidate_canonical:
            matched.append(job_skill)
            earned += 1
            continue
        job_tokens = _tokens(canonical)
        best_similarity = max(
            (_token_similarity(job_tokens, _tokens(candidate)) for candidate in candidate_canonical),
            default=0.0,
        )
        if best_similarity >= 0.72:
            partial.append(job_skill)
            earned += 0.65
        else:
            missing.append(job_skill)
    return round(100 * earned / len(job_skills)), matched, partial, missing


def _role_fit(target_position: str, job_title: str) -> int | None:
    if not target_position or not job_title:
        return None
    target = _normalize_text(target_position)
    title = _normalize_text(job_title)
    if target in title or title in target:
        return 100
    expanded = _expanded_query_terms(target)
    if any(term in title or title in term for term in expanded):
        return 88
    target_tokens = set().union(*(_tokens(term) for term in expanded))
    title_tokens = _tokens(title)
    if not target_tokens or not title_tokens:
        return 0
    overlap = len(target_tokens & title_tokens)
    return round(100 * overlap / max(1, min(len(target_tokens), len(title_tokens))))


def _experience_fit(
    *,
    candidate_years: int | None,
    candidate_level: str | None,
    job_min: int | None,
    job_max: int | None,
    job_level: str | None,
) -> tuple[int | None, str]:
    if candidate_years is not None and (job_min is not None or job_max is not None):
        minimum = int(job_min or 0)
        maximum = int(job_max) if job_max is not None else None
        if candidate_years < minimum:
            gap = minimum - candidate_years
            return (70 if gap == 1 else 40 if gap == 2 else 15), "below"
        if maximum is not None and candidate_years > maximum + 4:
            return 80, "above"
        return 100, "matched"
    candidate_rank = _EXPERIENCE_LEVEL_ORDER.get(str(candidate_level or "").casefold())
    job_rank = _EXPERIENCE_LEVEL_ORDER.get(str(job_level or "").casefold())
    if candidate_rank is None or job_rank is None:
        return None, "unknown"
    gap = candidate_rank - job_rank
    if gap >= 0:
        return (100 if gap <= 2 else 82), "matched" if gap <= 2 else "above"
    return (70 if gap == -1 else 40 if gap == -2 else 15), "below"


def _canonical_skill(value: str) -> str:
    normalized = _normalize_text(value).strip(" .")
    return _SKILL_ALIASES.get(normalized, normalized)


def _token_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    if left <= right or right <= left:
        return len(left & right) / max(1, min(len(left), len(right)))
    return len(left & right) / len(left | right)


def _dedupe_skills(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        skill = " ".join(str(value or "").split())[:100]
        key = _canonical_skill(skill)
        if skill and key and key not in seen:
            result.append(skill)
            seen.add(key)
    return result[:100]


def _infer_candidate_experience_years(cv_text: str) -> int | None:
    normalized = _normalize_text(cv_text)
    values = [
        int(value)
        for value in re.findall(r"\b(\d{1,2})\+?\s*(?:years?|yrs?|nam)\b", normalized)
        if 0 <= int(value) <= 50
    ]
    return max(values) if values else None


def _infer_candidate_experience_level(target_position: str, cv_text: str) -> str | None:
    sample = _normalize_text(f"{target_position} {cv_text[:1500]}")
    patterns = (
        ("manager", r"\bmanager\b|quan ly"),
        ("lead", r"\bteam lead\b|\btech lead\b|truong nhom"),
        ("senior", r"\bsenior\b"),
        ("middle", r"\bmiddle\b|\bmid[ -]?level\b"),
        ("junior", r"\bjunior\b"),
        ("fresher", r"\bfresher\b|entry[ -]?level"),
        ("internship", r"\bintern(?:ship)?\b|thuc tap"),
    )
    for level, pattern in patterns:
        if re.search(pattern, sample):
            return level
    return None


def _cv_match_reasons(match: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if match["matched_skills"]:
        reasons.append(f"Khớp kỹ năng: {', '.join(match['matched_skills'][:3])}")
    if match["experience_fit"] == "matched":
        reasons.append("Kinh nghiệm phù hợp yêu cầu")
    elif match["experience_fit"] == "below":
        reasons.append("Kinh nghiệm hiện thấp hơn yêu cầu")
    if match["role_score"] is not None and match["role_score"] >= 70:
        reasons.append("Vị trí phù hợp mục tiêu nghề nghiệp")
    return reasons[:3] or ["Mức phù hợp dựa trên dữ liệu CV và JD hiện có"]


def _job_payload(
    row: dict[str, Any],
    *,
    saved: bool,
    match_score: int,
    match_reasons: list[str],
) -> dict[str, Any]:
    sources = sorted(
        row.get("sources") or [],
        key=lambda item: _SOURCE_PRIORITY.get(str(item.get("source")), 99),
    )
    primary_source = sources[0] if sources else {}
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "company_name": row["company_name"],
        "company_logo_url": row.get("company_logo_url"),
        "location": row.get("location"),
        "workplace_type": row.get("workplace_type"),
        "employment_type": row.get("employment_type"),
        "experience_level": row.get("experience_level"),
        "experience_years_min": row.get("experience_years_min"),
        "experience_years_max": row.get("experience_years_max"),
        "salary_min": row.get("salary_min"),
        "salary_max": row.get("salary_max"),
        "salary_currency": row.get("salary_currency"),
        "salary_text": row.get("salary_text"),
        "description": row.get("description") or "",
        "skills": row.get("skills_json") or [],
        "posted_at": _iso(row.get("posted_at")),
        "expires_at": _iso(row.get("expires_at")),
        "first_seen_at": _iso(row.get("first_seen_at")),
        "last_seen_at": _iso(row.get("last_seen_at")),
        "sources": sources,
        "primary_source": primary_source.get("source"),
        "apply_url": primary_source.get("apply_url") or primary_source.get("source_url"),
        "saved": saved,
        "match_score": match_score,
        "match_reasons": match_reasons,
    }


def _record_search(
    cur,
    *,
    user_id: Any,
    query: str,
    location: str,
    filters: dict[str, Any],
) -> None:
    cur.execute(
        """
        insert into job_search_history (user_id, query, location, filters_json)
        values (%s, %s, %s, %s)
        """,
        (user_id, query or "Tất cả công việc", location or None, Json(filters)),
    )
    cur.execute(
        """
        delete from job_search_history
        where user_id = %s
          and id not in (
            select id from job_search_history where user_id = %s order by searched_at desc limit 20
          )
        """,
        (user_id, user_id),
    )


def _require_user_id(cur, external_auth_id: str) -> Any:
    cur.execute("select id from users where external_auth_id = %s limit 1", (external_auth_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User profile has not been synced yet.")
    return row["id"]


def _preferences_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "desired_titles": (row or {}).get("desired_titles_json") or [],
        "locations": (row or {}).get("locations_json") or [],
        "workplace_types": (row or {}).get("workplace_types_json") or [],
        "employment_types": (row or {}).get("employment_types_json") or [],
        "updated_at": _iso((row or {}).get("updated_at")),
    }


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9+#.]+", " ", ascii_text).split())


def _normalize_location(value: str) -> str:
    normalized = _normalize_text(value)
    city_aliases = {
        "ho chi minh": ("ho chi minh", "hcm", "saigon", "sai gon"),
        "ha noi": ("ha noi", "hanoi"),
        "da nang": ("da nang", "danang"),
    }
    for canonical, aliases in city_aliases.items():
        if any(alias in normalized for alias in aliases):
            return canonical
    return normalized


def _expanded_query_terms(query: str) -> list[str]:
    normalized = _normalize_text(query)
    terms = [normalized]
    for canonical, aliases in _QUERY_ALIASES.items():
        if canonical in normalized or normalized in (canonical, *aliases):
            terms.extend([canonical, *aliases])
    return list(dict.fromkeys(term for term in terms if term))[:12]


def _related_tsquery(query: str) -> str:
    groups: list[str] = []
    for term in _expanded_query_terms(query):
        tokens = list(_tokens(term))[:8]
        if tokens:
            groups.append(" & ".join(sorted(tokens)))
    return " | ".join(f"({group})" for group in groups) or "viecareer_no_match"


def _tokens(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(_normalize_text(value)))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)
