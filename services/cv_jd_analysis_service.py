"""Persistence helpers for CV/JD analysis results."""

from typing import Any

from fastapi import HTTPException, status
from psycopg2.extras import Json

from core.database import get_connection
from services.user_service import upsert_auth_user


def save_cv_jd_analysis(
    *,
    external_auth_id: str,
    email: str,
    account_full_name: str,
    analysis: dict[str, Any],
    avatar_url: str | None = None,
    auth_provider: str = "clerk",
) -> dict[str, Any]:
    user = upsert_auth_user(
        external_auth_id=external_auth_id,
        email=email,
        full_name=account_full_name,
        avatar_url=avatar_url,
        auth_provider="clerk",
        identity_verified=True,
    )

    ats_components = analysis.get("atsComponents") or {}
    normalized_gap_analysis = _normalize_gap_analysis(analysis.get("gapAnalysis") or [])
    normalized_signals = _normalize_readiness_signals(analysis.get("readinessSignals") or [])
    baseline_dimensions = _normalize_baseline_dimensions(
        analysis.get("baselineDimensions"),
        normalized_gap_analysis,
        normalized_signals,
    )
    analysis = {
        **analysis,
        "gapAnalysis": normalized_gap_analysis,
        "readinessSignals": normalized_signals,
        "baselineDimensions": baseline_dimensions,
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_cv_jd_analysis_columns(cur)
                cur.execute(
                    """
                    insert into cv_jd_analyses (
                      user_id,
                      full_name,
                      target_position,
                      education_level,
                      tech_stack_json,
                      must_have_skills_json,
                      key_responsibilities_json,
                      s_skill,
                      s_exp,
                      s_edu,
                      s_sem,
                      ats_score,
                      jri_baseline,
                      d1_baseline,
                      d2_baseline,
                      d3_baseline,
                      d4_baseline,
                      d5_baseline,
                      llm_raw_json
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    returning *
                    """,
                    (
                        user["id"],
                        analysis.get("fullName"),
                        analysis.get("targetPosition"),
                        analysis.get("educationLevel"),
                        Json(analysis.get("techStack") or []),
                        Json(analysis.get("mustHaveSkills") or []),
                        Json(analysis.get("keyResponsibilities") or []),
                        int(ats_components.get("S_skill") or 0),
                        int(ats_components.get("S_exp") or 0),
                        int(ats_components.get("S_edu") or 0),
                        int(ats_components.get("S_sem") or 0),
                        int(analysis.get("atsScore") or 0),
                        int(analysis.get("jriBaseline") or analysis.get("atsScore") or 0),
                        int(baseline_dimensions["D1_technical"]),
                        int(baseline_dimensions["D2_communication"]),
                        int(baseline_dimensions["D3_problem_solving"]),
                        int(baseline_dimensions["D4_attitude"]),
                        int(baseline_dimensions["D5_cultural_fit"]),
                        Json(analysis),
                    ),
                )
                row = cur.fetchone()
                _insert_gap_items(cur, row["id"], normalized_gap_analysis)
                _insert_readiness_signals(cur, row["id"], normalized_signals)

            conn.commit()
            return row
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database operation failed.") from exc


def _ensure_cv_jd_analysis_columns(cur) -> None:
    cur.execute("alter table cv_jd_analyses add column if not exists ats_score int null")
    cur.execute("alter table cv_jd_analyses add column if not exists jri_baseline int null")
    cur.execute("alter table cv_jd_analyses add column if not exists d1_baseline int null")
    cur.execute("alter table cv_jd_analyses add column if not exists d2_baseline int null")
    cur.execute("alter table cv_jd_analyses add column if not exists d3_baseline int null")
    cur.execute("alter table cv_jd_analyses add column if not exists d4_baseline int null")
    cur.execute("alter table cv_jd_analyses add column if not exists d5_baseline int null")
    cur.execute(
        """
        create table if not exists cv_jd_readiness_signals (
          id bigserial primary key,
          analysis_id uuid not null references cv_jd_analyses(id) on delete cascade,
          source_phase varchar(20) not null default 'cv_jd',
          signal_key varchar(100) not null,
          signal_label varchar(150) not null,
          dimension varchar(100) not null,
          status varchar(20) null,
          score int null,
          cv_evidence text null,
          jd_requirement text null,
          created_at timestamptz not null default now()
        )
        """
    )


def _insert_gap_items(cur, analysis_id: str, gap_analysis: list[dict[str, Any]]) -> None:
    for category in gap_analysis:
        category_name = str(category.get("category") or "").strip()
        category_score = _to_int(category.get("score"), 0)
        for skill in category.get("skills") or []:
            cur.execute(
                """
                insert into cv_jd_gap_items (
                  analysis_id,
                  category,
                  skill,
                  status,
                  cv_evidence,
                  jd_requirement,
                  score
                )
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    analysis_id,
                    category_name,
                    skill.get("skill"),
                    _normalize_status(skill.get("status")),
                    skill.get("cvEvidence"),
                    skill.get("jdRequirement"),
                    category_score,
                ),
            )


def _insert_readiness_signals(cur, analysis_id: str, signals: list[dict[str, Any]]) -> None:
    for signal in signals:
        cur.execute(
            """
            insert into cv_jd_readiness_signals (
              analysis_id,
              source_phase,
              signal_key,
              signal_label,
              dimension,
              status,
              score,
              cv_evidence,
              jd_requirement
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                analysis_id,
                "cv_jd",
                signal["key"],
                signal["label"],
                signal["dimension"],
                signal["status"],
                signal["score"],
                signal["cvEvidence"],
                signal["jdRequirement"],
            ),
        )


def _normalize_gap_analysis(raw_gap_analysis: list[Any]) -> list[dict[str, Any]]:
    target_categories = {
        "Domain & Technical": [],
        "Communication": [],
        "Structured Thinking": [],
        "Initiative & Ownership": [],
        "Team Collaboration": [],
    }
    scores: dict[str, list[int]] = {key: [] for key in target_categories}

    for raw_category in raw_gap_analysis:
        if not isinstance(raw_category, dict):
            continue
        category_name = _normalize_category(raw_category.get("category"))
        if not category_name:
            continue
        score = _to_int(raw_category.get("score"), 0)
        scores[category_name].append(score)

        for skill in raw_category.get("skills") or []:
            if isinstance(skill, dict):
                target_categories[category_name].append(
                    {
                        "skill": skill.get("skill") or category_name,
                        "status": _normalize_status(skill.get("status")),
                        "cvEvidence": skill.get("cvEvidence") or "",
                        "jdRequirement": skill.get("jdRequirement") or "",
                    }
                )

    normalized = []
    for category_name, skills in target_categories.items():
        category_scores = scores[category_name]
        normalized.append(
            {
                "category": category_name,
                "score": round(sum(category_scores) / len(category_scores)) if category_scores else 0,
                "skills": skills,
            }
        )
    return normalized


def _normalize_category(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if "domain" in raw or "technical" in raw or "experience" in raw:
        return "Domain & Technical"
    if "communication" in raw:
        return "Communication"
    if "problem" in raw or "solving" in raw:
        return "Structured Thinking"
    if "structured" in raw or "thinking" in raw:
        return "Structured Thinking"
    if "attitude" in raw or "professional" in raw or "initiative" in raw or "ownership" in raw:
        return "Initiative & Ownership"
    if "culture" in raw or "fit" in raw or "collaboration" in raw or "team" in raw:
        return "Team Collaboration"
    if "skill" in raw and "soft" not in raw:
        return "Domain & Technical"
    if "soft" in raw:
        return "Communication"
    return None


def _normalize_readiness_signals(raw_signals: list[Any]) -> list[dict[str, Any]]:
    default_signals = [
        ("domain_knowledge", "Domain Knowledge", "Domain & Technical"),
        ("technical_stack_match", "Technical Stack Match", "Domain & Technical"),
        ("experience_relevance", "Experience Relevance", "Domain & Technical"),
        ("seniority_signal", "Seniority Signal", "Domain & Technical"),
        ("communication_exposure", "Communication Exposure", "Communication"),
        ("structured_output_signal", "Structured Output Signal", "Structured Thinking"),
        ("initiative_signal", "Initiative Signal", "Initiative & Ownership"),
        ("collaboration_signal", "Collaboration Signal", "Team Collaboration"),
    ]
    by_key: dict[str, dict[str, Any]] = {
        key: {
            "key": key,
            "label": label,
            "dimension": dimension,
            "score": 0,
            "status": "missing",
            "cvEvidence": "",
            "jdRequirement": "",
        }
        for key, label, dimension in default_signals
    }

    for raw_signal in raw_signals:
        if not isinstance(raw_signal, dict):
            continue
        key = _normalize_signal_key(raw_signal.get("key") or raw_signal.get("label"))
        if key not in by_key:
            continue
        signal = by_key[key]
        signal.update(
            {
                "score": _to_int(raw_signal.get("score"), 0),
                "status": _normalize_signal_status(raw_signal.get("status")),
                "cvEvidence": str(raw_signal.get("cvEvidence") or raw_signal.get("evidence") or ""),
                "jdRequirement": str(raw_signal.get("jdRequirement") or ""),
            }
        )

    return list(by_key.values())


def _normalize_baseline_dimensions(
    raw_dimensions: Any,
    gap_analysis: list[dict[str, Any]],
    signals: list[dict[str, Any]],
) -> dict[str, int]:
    if isinstance(raw_dimensions, dict):
        return {
            "D1_technical": _to_int(raw_dimensions.get("D1_technical"), 0),
            "D2_communication": _to_int(raw_dimensions.get("D2_communication"), 0),
            "D3_problem_solving": _to_int(raw_dimensions.get("D3_problem_solving"), 0),
            "D4_attitude": _to_int(raw_dimensions.get("D4_attitude"), 0),
            "D5_cultural_fit": _to_int(raw_dimensions.get("D5_cultural_fit"), 0),
        }

    scores_by_dimension: dict[str, list[int]] = {}
    for signal in signals:
        scores_by_dimension.setdefault(signal["dimension"], []).append(_to_int(signal.get("score"), 0))
    for gap in gap_analysis:
        scores_by_dimension.setdefault(str(gap.get("category") or ""), []).append(_to_int(gap.get("score"), 0))

    return {
        "D1_technical": _mean_score(scores_by_dimension.get("Domain & Technical")),
        "D2_communication": _mean_score(scores_by_dimension.get("Communication")),
        "D3_problem_solving": _mean_score(scores_by_dimension.get("Structured Thinking")),
        "D4_attitude": _mean_score(scores_by_dimension.get("Initiative & Ownership")),
        "D5_cultural_fit": _mean_score(scores_by_dimension.get("Team Collaboration")),
    }


def _normalize_signal_key(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "domain": "domain_knowledge",
        "domain_knowledge": "domain_knowledge",
        "technical_stack": "technical_stack_match",
        "technical_stack_match": "technical_stack_match",
        "experience": "experience_relevance",
        "experience_relevance": "experience_relevance",
        "seniority": "seniority_signal",
        "seniority_signal": "seniority_signal",
        "communication": "communication_exposure",
        "communication_exposure": "communication_exposure",
        "structured_output": "structured_output_signal",
        "structured_output_signal": "structured_output_signal",
        "structured_thinking": "structured_output_signal",
        "initiative": "initiative_signal",
        "initiative_signal": "initiative_signal",
        "ownership": "initiative_signal",
        "collaboration": "collaboration_signal",
        "collaboration_signal": "collaboration_signal",
        "team_collaboration": "collaboration_signal",
    }
    return aliases.get(raw, raw)


def _normalize_signal_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"strong", "moderate", "weak", "missing"}:
        return raw
    return "missing"


def _mean_score(values: list[int] | None) -> int:
    clean = [value for value in values or [] if value > 0]
    return round(sum(clean) / len(clean)) if clean else 0


def _normalize_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"matched", "partial", "missing"}:
        return raw
    return "partial"


def _to_int(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default
