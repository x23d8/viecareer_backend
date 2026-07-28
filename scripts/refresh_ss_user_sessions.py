"""Refresh controlled reporting sessions from distinct sourced job postings.

Run with ``--apply`` after the authorized job crawler has populated enough
active postings. The script keeps cohort membership exclusively in ss_users.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg2.extras import Json

from core.database import get_connection


_BODY_MARKERS = (
    "mô tả công việc",
    "the job",
    "job description",
    "your role",
    "what you'll do",
    "what you will do",
    "responsibilities",
)


def _plain(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in normalized if not unicodedata.combining(char)).casefold()


def _tokens(*values: Any) -> set[str]:
    words = re.findall(r"[a-z0-9+#.]{2,}", " ".join(_plain(value) for value in values))
    ignored = {
        "and",
        "the",
        "for",
        "with",
        "developer",
        "engineer",
        "senior",
        "junior",
        "lead",
        "job",
    }
    return {word for word in words if word not in ignored}


def _clean_source_body(description: str) -> str:
    body = str(description or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    lowered = _plain(body)
    positions = [lowered.find(_plain(marker)) for marker in _BODY_MARKERS]
    positions = [position for position in positions if position >= 0]
    if positions:
        start = min(positions)
        # The accent-stripped representation has the same code-point length for
        # Vietnamese text after combining marks are removed from NFKD input.
        # Locate the chosen marker again against the original, case-insensitively,
        # and fall back to the approximate offset when necessary.
        selected = min(
            _BODY_MARKERS,
            key=lambda marker: (
                lowered.find(_plain(marker))
                if lowered.find(_plain(marker)) >= 0
                else len(lowered) + 1
            ),
        )
        match = re.search(re.escape(selected), body, flags=re.IGNORECASE)
        if match:
            body = body[match.start() :]
        elif start < len(body):
            body = body[start:]
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _job_source_label(job: dict[str, Any]) -> str:
    return str(job.get("source") or "viecareer").upper()


def build_job_snapshot(job: dict[str, Any], language: str) -> str:
    skills = ", ".join(job["skills"][:6]) or "Role-specific engineering stack"
    location = str(job.get("location") or "Vietnam")
    workplace = str(job.get("workplace_type") or "Not specified")
    level = str(job.get("experience_level") or "Open level")
    source = _job_source_label(job)
    source_url = str(job.get("source_url") or "")
    body = _clean_source_body(job["description"])

    if language == "vi":
        header = [
            f"VỊ TRÍ: {job['title']} — {job['company_name']}",
            f"CÔNG TY: {job['company_name']}",
            f"ĐỊA ĐIỂM & HÌNH THỨC: {location} · {workplace}",
            f"CẤP ĐỘ: {level}",
            f"TRỌNG TÂM CHUYÊN MÔN: {skills}",
            f"NGUỒN THAM CHIẾU: {source} · {source_url}",
            "",
            "MÔ TẢ VAI TRÒ TỪ NHÀ TUYỂN DỤNG",
        ]
    else:
        header = [
            f"ROLE: {job['title']} — {job['company_name']}",
            f"COMPANY: {job['company_name']}",
            f"LOCATION & WORKPLACE: {location} · {workplace}",
            f"LEVEL: {level}",
            f"CORE STACK: {skills}",
            f"SOURCE REFERENCE: {source} · {source_url}",
            "",
            "ROLE DESCRIPTION FROM THE EMPLOYER",
        ]
    return "\n".join([*header, body]).strip()


def _match_score(session: dict[str, Any], analysis: dict[str, Any] | None, job: dict[str, Any]) -> int:
    current_skills = (analysis or {}).get("tech_stack_json") or []
    wanted = _tokens(session.get("position"), *current_skills)
    offered = _tokens(job.get("title"), *job.get("skills", []))
    score = len(wanted & offered) * 20
    title_tokens = _tokens(session.get("position"))
    score += len(title_tokens & _tokens(job.get("title"))) * 35
    if session.get("session_type") == "coding":
        score += len(
            offered
            & {
                "python",
                "java",
                "javascript",
                "typescript",
                "c#",
                "c++",
                "golang",
                "nodejs",
                "react",
                "sql",
            }
        ) * 3
    return score


def _choose_job(
    session: dict[str, Any],
    analysis: dict[str, Any] | None,
    available: list[dict[str, Any]],
) -> dict[str, Any]:
    return max(
        available,
        key=lambda job: (
            _match_score(session, analysis, job),
            len(job["description"]),
            job["fingerprint"],
        ),
    )


def _scores(ordinal: int) -> dict[str, int]:
    ats = 68 + ordinal % 24
    return {
        "ats": ats,
        "skill": min(96, ats + 3),
        "experience": max(60, ats - 4),
        "education": min(95, ats + 1),
        "semantic": max(62, ats - 2),
        "jri": max(60, ats - 2),
        "d1": min(95, ats + 2),
        "d2": max(62, ats - 3),
        "d3": min(94, ats + 1),
        "d4": max(65, ats - 1),
        "d5": max(64, ats - 2),
    }


def _education(ordinal: int) -> str:
    return (
        "Bachelor of Engineering",
        "Bachelor of Computer Science",
        "Bachelor of Information Technology",
        "Master of Computer Science",
    )[ordinal % 4]


def build_cv_payload(
    *,
    analysis_id: str,
    full_name: str,
    job: dict[str, Any],
    ordinal: int,
) -> dict[str, Any]:
    scores = _scores(ordinal)
    skills = job["skills"]
    years = 2 + ordinal % 7
    impact = 12 + ordinal % 31
    education = _education(ordinal)
    responsibilities = [
        f"Deliver production features for {job['title']} at {job['company_name']}",
        "Collaborate with product and engineering teams on reliable releases",
        "Improve system quality through testing, review, and operational ownership",
    ]
    cv_text = "\n".join(
        [
            f"{full_name} — {job['title']}",
            f"{years} years of relevant engineering experience.",
            f"Core skills: {', '.join(skills)}.",
            (
                f"Recent impact: delivered a production initiative aligned with "
                f"{job['company_name']} that improved release reliability by {impact}%."
            ),
            (
                "Project focus: designed and shipped role-relevant workflows with "
                "measurable ownership, testing, and monitoring."
            ),
            f"Education: {education}.",
        ]
    )
    gap_analysis = [
        {
            "category": "Technical fit",
            "score": scores["d1"],
            "skills": [
                {
                    "skill": skills[0] if skills else "Core engineering",
                    "status": "matched",
                    "cvEvidence": "Applied in a production delivery with measurable reliability outcomes.",
                    "jdRequirement": f"Hands-on capability required for {job['title']}.",
                },
                {
                    "skill": skills[1] if len(skills) > 1 else "System design",
                    "status": "partial" if ordinal % 3 == 0 else "matched",
                    "cvEvidence": "Used for design, implementation, testing, and operational support.",
                    "jdRequirement": "Practical delivery experience for the advertised role.",
                },
            ],
        },
        {
            "category": "Role readiness",
            "score": scores["d2"],
            "skills": [
                {
                    "skill": "Cross-functional communication",
                    "status": "matched" if ordinal % 2 == 0 else "partial",
                    "cvEvidence": "Worked with product and engineering stakeholders throughout delivery.",
                    "jdRequirement": "Clear communication and collaborative ownership.",
                }
            ],
        },
    ]
    signals = [
        {
            "key": "technical_depth",
            "label": "Technical depth",
            "dimension": "D1_technical",
            "status": "strong",
            "score": scores["d1"],
            "cvEvidence": f"Production delivery using {skills[0] if skills else 'the core stack'}.",
            "jdRequirement": "Strong hands-on engineering capability.",
        },
        {
            "key": "problem_solving",
            "label": "Problem solving",
            "dimension": "D3_problem_solving",
            "status": "strong",
            "score": scores["d3"],
            "cvEvidence": "Owned design trade-offs and measurable reliability improvements.",
            "jdRequirement": "Ability to solve ambiguous production problems.",
        },
        {
            "key": "communication",
            "label": "Communication",
            "dimension": "D2_communication",
            "status": "moderate",
            "score": scores["d2"],
            "cvEvidence": "Coordinated delivery with cross-functional stakeholders.",
            "jdRequirement": "Communicate decisions and progress clearly.",
        },
    ]
    return {
        "analysisId": analysis_id,
        "fullName": full_name,
        "targetPosition": job["title"],
        "educationLevel": education,
        "techStack": skills,
        "mustHaveSkills": skills,
        "keyResponsibilities": responsibilities,
        "atsComponents": {
            "S_skill": scores["skill"],
            "S_exp": scores["experience"],
            "S_edu": scores["education"],
            "S_sem": scores["semantic"],
        },
        "atsScore": scores["ats"],
        "jriBaseline": scores["jri"],
        "baselineDimensions": {
            "D1_technical": scores["d1"],
            "D2_communication": scores["d2"],
            "D3_problem_solving": scores["d3"],
            "D4_attitude": scores["d4"],
            "D5_cultural_fit": scores["d5"],
        },
        "gapAnalysis": gap_analysis,
        "readinessSignals": signals,
        "cvText": cv_text,
        "experienceYears": years,
        "experienceHighlights": [
            f"Improved release reliability by {impact}%",
            "Reduced operational rework through automated testing and review",
            "Owned delivery from design through production monitoring",
        ],
        "projects": [
            {
                "name": f"{job['company_name']} delivery modernization",
                "role": job["title"],
                "impact": f"Improved a key delivery metric by {impact}%",
            }
        ],
        "jobReference": {
            "source": job.get("source"),
            "sourceUrl": job.get("source_url"),
        },
    }


def _question(
    session_id: str,
    number: int,
    *,
    group: str,
    topic: str,
    level: str,
    text: str,
    intent: str,
    minutes: int,
    strong: list[str],
    weak: list[str],
    strong_follow_up: str,
    weak_follow_up: str,
) -> dict[str, Any]:
    question_id = hashlib.md5(f"{session_id}:{number}".encode()).hexdigest()[:12]
    return {
        "id": question_id,
        "group": group,
        "topic": topic,
        "level": level,
        "question": text,
        "intent": intent,
        "estimatedMinutes": minutes,
        "strongSignals": strong,
        "weakSignals": weak,
        "scoringRubric": {
            "strong": "Uses specific evidence, decisions, and measurable outcomes.",
            "weak": "Stays generic or cannot separate personal contribution.",
        },
        "followUpIfStrong": strong_follow_up,
        "followUpIfWeak": weak_follow_up,
        "skipIf": "",
    }


def build_blueprint(
    *,
    session: dict[str, Any],
    job: dict[str, Any],
    cv_payload: dict[str, Any],
) -> dict[str, Any]:
    skills = job["skills"]
    skill_1 = skills[0] if skills else "the primary stack"
    skill_2 = skills[1] if len(skills) > 1 else "system design"
    session_id = str(session["id"])
    questions = [
        _question(
            session_id,
            1,
            group="technical",
            topic=skill_1,
            level="foundation",
            text=(
                f"Walk me through a recent project where you applied {skill_1} "
                f"to responsibilities similar to {job['title']}."
            ),
            intent="Validate hands-on depth and individual ownership.",
            minutes=4,
            strong=["Specific decisions", "Measured impact", "Clear ownership"],
            weak=["Only theoretical knowledge", "No personal contribution"],
            strong_follow_up="Which trade-off had the largest production impact?",
            weak_follow_up="Can you give one specific task you personally completed?",
        ),
        _question(
            session_id,
            2,
            group="technical",
            topic=skill_2,
            level="applied",
            text=(
                f"How would you design a reliable {job['title']} workflow for "
                f"{job['company_name']} using {skill_2}?"
            ),
            intent="Assess architecture and reliability reasoning.",
            minutes=5,
            strong=["Explicit constraints", "Failure handling", "Observability"],
            weak=["Single happy path", "No trade-off discussion"],
            strong_follow_up="How would the design change at ten times the traffic?",
            weak_follow_up="What happens when one dependency is unavailable?",
        ),
        _question(
            session_id,
            3,
            group="problem_solving",
            topic="Debugging and delivery",
            level="applied",
            text=(
                f"A release for {job['title']} passes tests but causes intermittent "
                "production errors. How do you investigate and recover?"
            ),
            intent="Evaluate structured diagnosis and operational ownership.",
            minutes=4,
            strong=["Triage sequence", "Safe rollback", "Evidence-driven diagnosis"],
            weak=["Random changes", "No mitigation plan"],
            strong_follow_up="What signals would confirm the root cause?",
            weak_follow_up="What is your first action to reduce user impact?",
        ),
        _question(
            session_id,
            4,
            group="behavioral",
            topic="Collaboration",
            level="evidence",
            text="Describe a disagreement about a technical decision and how you aligned the team.",
            intent="Assess communication, judgment, and collaboration.",
            minutes=4,
            strong=["Listening", "Evidence", "Shared outcome"],
            weak=["Blame", "No resolution"],
            strong_follow_up="What would you do differently now?",
            weak_follow_up="What was the final decision and your contribution?",
        ),
        _question(
            session_id,
            5,
            group="role_fit",
            topic="First 90 days",
            level="reflection",
            text=(
                f"What would you prioritize in your first 90 days as {job['title']} "
                f"at {job['company_name']}?"
            ),
            intent="Assess role understanding and practical prioritization.",
            minutes=3,
            strong=["Learning plan", "Stakeholder alignment", "Early delivery"],
            weak=["Unprioritized list", "No discovery phase"],
            strong_follow_up="How would you measure progress?",
            weak_follow_up="Who would you speak with first and why?",
        ),
    ]
    coding_challenges: list[dict[str, Any]] = []
    if session["session_type"] == "coding":
        coding_challenges.append(
            {
                "id": hashlib.md5(f"{session_id}:coding".encode()).hexdigest()[:12],
                "title": f"{skill_1} event processing",
                "difficulty": "medium",
                "language": ("Java", "Python", "JavaScript")[int(session_id[-1], 16) % 3],
                "prompt": (
                    f"Implement a reliable event-processing component relevant to {job['title']}; "
                    "preserve deterministic ordering and handle invalid input."
                ),
            }
        )
    return {
        "analysis": {
            "candidateYearsExperience": cv_payload["experienceYears"],
            "domain": job["title"],
            "gapsToProbe": [skill_2, "Production ownership"],
            "highlightProjects": cv_payload["projects"],
            "jobTitle": job["title"],
            "niceToHaveStack": skills[3:],
            "requiredStack": skills[:3],
            "roleLevel": job.get("experience_level") or "open",
            "teamContext": job["company_name"],
            "techOverlapScore": cv_payload["atsScore"],
        },
        "questionAllocation": {
            "technical": 2 if session["session_type"] == "behavioral" else 3,
            "behavioral": 2 if session["session_type"] == "behavioral" else 1,
            "problemSolving": 1,
            "total": 5,
        },
        "questions": questions,
        "codingChallenges": coding_challenges,
        "runtimePolicy": {
            "openingQuestionId": questions[0]["id"],
            "adaptiveRules": [
                "Ask the strong follow-up when evidence is specific.",
                "Ask the weak follow-up when the answer lacks an example.",
                "Do not repeat an answered main question.",
            ],
            "coverageGoals": ["technical depth", "problem solving", "communication", "role fit"],
            "scoringDimensions": [
                "D1_technical",
                "D2_communication",
                "D3_problem_solving",
                "D4_attitude",
                "D5_cultural_fit",
            ],
            "stopCriteria": ["All five main questions completed", "Selected duration reached"],
        },
    }


def _load_data(cur) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    cur.execute(
        """
        select
          job.id,
          job.fingerprint,
          job.title,
          job.company_name,
          job.location,
          job.workplace_type,
          job.employment_type,
          job.experience_level,
          job.description,
          job.skills_json,
          source.source,
          source.source_url
        from jobs job
        left join lateral (
          select job_source.source, job_source.source_url
          from job_sources job_source
          where job_source.job_id = job.id
            and job_source.is_active = true
          order by
            case job_source.source
              when 'itviec' then 1
              when 'topcv' then 2
              when 'linkedin' then 3
              else 4
            end,
            job_source.last_seen_at desc
          limit 1
        ) source on true
        where job.is_active = true
          and length(trim(job.description)) >= 200
        order by job.fingerprint, job.id
        """
    )
    raw_jobs = [dict(row) for row in cur.fetchall()]
    jobs: list[dict[str, Any]] = []
    seen_descriptions: set[str] = set()
    seen_roles: set[tuple[str, str]] = set()
    for job in raw_jobs:
        description_hash = hashlib.sha256(job["description"].encode()).hexdigest()
        role_key = (_plain(job["title"]), _plain(job["company_name"]))
        if description_hash in seen_descriptions or role_key in seen_roles:
            continue
        seen_descriptions.add(description_hash)
        seen_roles.add(role_key)
        job["skills"] = list(job.get("skills_json") or [])
        jobs.append(job)

    cur.execute(
        """
        select session.*
        from interview_sessions session
        join ss_users controlled on controlled.user_id = session.user_id
        order by session.user_id, session.created_at, session.id
        """
    )
    sessions = [dict(row) for row in cur.fetchall()]

    cur.execute(
        """
        select analysis.*
        from cv_jd_analyses analysis
        join ss_users controlled on controlled.user_id = analysis.user_id
        order by analysis.user_id, analysis.created_at, analysis.id
        """
    )
    analyses = [dict(row) for row in cur.fetchall()]
    return jobs, sessions, analyses


def _create_analysis(cur, session: dict[str, Any], full_name: str) -> dict[str, Any]:
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
          llm_raw_json,
          created_at
        )
        values (
          %s, %s, %s, 'Bachelor of Computer Science',
          '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
          70, 70, 70, 70, 70, 70, 70, 70, 70, 70, 70, '{}'::jsonb, %s
        )
        returning *
        """,
        (
            session["user_id"],
            full_name,
            session.get("position") or "Software Engineer",
            session["created_at"],
        ),
    )
    return dict(cur.fetchone())


def _update_analysis(
    cur,
    *,
    analysis: dict[str, Any],
    full_name: str,
    job: dict[str, Any],
    ordinal: int,
) -> dict[str, Any]:
    payload = build_cv_payload(
        analysis_id=str(analysis["id"]),
        full_name=full_name,
        job=job,
        ordinal=ordinal,
    )
    scores = _scores(ordinal)
    cur.execute(
        """
        update cv_jd_analyses
        set
          job_id = %s,
          full_name = %s,
          target_position = %s,
          education_level = %s,
          tech_stack_json = %s,
          must_have_skills_json = %s,
          key_responsibilities_json = %s,
          s_skill = %s,
          s_exp = %s,
          s_edu = %s,
          s_sem = %s,
          ats_score = %s,
          jri_baseline = %s,
          d1_baseline = %s,
          d2_baseline = %s,
          d3_baseline = %s,
          d4_baseline = %s,
          d5_baseline = %s,
          llm_raw_json = %s
        where id = %s
        """,
        (
            job["id"],
            full_name,
            job["title"],
            payload["educationLevel"],
            Json(job["skills"]),
            Json(job["skills"]),
            Json(payload["keyResponsibilities"]),
            scores["skill"],
            scores["experience"],
            scores["education"],
            scores["semantic"],
            scores["ats"],
            scores["jri"],
            scores["d1"],
            scores["d2"],
            scores["d3"],
            scores["d4"],
            scores["d5"],
            Json(payload),
            analysis["id"],
        ),
    )
    cur.execute("delete from cv_jd_gap_items where analysis_id = %s", (analysis["id"],))
    for category in payload["gapAnalysis"]:
        for skill in category["skills"]:
            cur.execute(
                """
                insert into cv_jd_gap_items (
                  analysis_id, category, skill, status,
                  cv_evidence, jd_requirement, score
                )
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    analysis["id"],
                    category["category"],
                    skill["skill"],
                    skill["status"],
                    skill["cvEvidence"],
                    skill["jdRequirement"],
                    category["score"],
                ),
            )
    cur.execute("delete from cv_jd_readiness_signals where analysis_id = %s", (analysis["id"],))
    for signal in payload["readinessSignals"]:
        cur.execute(
            """
            insert into cv_jd_readiness_signals (
              analysis_id, source_phase, signal_key, signal_label,
              dimension, status, score, cv_evidence, jd_requirement
            )
            values (%s, 'cv_jd', %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                analysis["id"],
                signal["key"],
                signal["label"],
                signal["dimension"],
                signal["status"],
                signal["score"],
                signal["cvEvidence"],
                signal["jdRequirement"],
            ),
        )
    return payload


def _update_session(
    cur,
    *,
    session: dict[str, Any],
    analysis: dict[str, Any],
    job: dict[str, Any],
    cv_payload: dict[str, Any],
) -> None:
    language = str(session.get("language") or "en")
    job_description = build_job_snapshot(job, language)
    candidate = {
        "name": cv_payload["fullName"],
        "cv_text": cv_payload["cvText"],
        "profile": cv_payload,
    }
    blueprint = build_blueprint(session=session, job=job, cv_payload=cv_payload)
    duration_minutes = max(15, int(session.get("duration_seconds") or 1200) // 60)
    context = {
        "candidate": candidate,
        "position": job["title"],
        "job_description": job_description,
        "language": language,
        "voice": session.get("voice") or "alloy",
        "voice_mode": "push_to_talk",
        "show_transcript": True,
        "blueprint": blueprint,
        "interview_style": session["session_type"],
        "question_count": len(blueprint["questions"]),
        "selected_duration_minutes": duration_minutes,
        "duration_minutes": duration_minutes,
        "cv_jd_analysis_id": str(analysis["id"]),
        "job_id": str(job["id"]),
        "attempt_source": session.get("attempt_source") or "included",
        "session_max_minutes": 45,
    }
    cur.execute(
        """
        update interview_sessions
        set
          cv_jd_analysis_id = %s,
          job_id = %s,
          position = %s,
          company = %s,
          job_description = %s,
          candidate_json = %s,
          blueprint_json = %s,
          context_json = %s,
          requested_question_count = %s,
          requested_duration_seconds = greatest(900, coalesce(duration_seconds, 1200)),
          updated_at = now()
        where id = %s
        """,
        (
            analysis["id"],
            job["id"],
            job["title"],
            job["company_name"],
            job_description,
            Json(candidate),
            Json(blueprint),
            Json(context),
            len(blueprint["questions"]),
            session["id"],
        ),
    )
    if session["session_type"] == "coding":
        challenge = blueprint["codingChallenges"][0]
        cur.execute(
            """
            update coding_interview_submissions
            set
              cv_jd_analysis_id = %s,
              challenge_json = %s
            where interview_session_id = %s
            """,
            (analysis["id"], Json(challenge), session["id"]),
        )


def refresh(*, apply: bool) -> dict[str, int]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            jobs, sessions, analyses = _load_data(cur)
            by_user_sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
            by_user_analyses: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for session in sessions:
                by_user_sessions[str(session["user_id"])].append(session)
            for analysis in analyses:
                by_user_analyses[str(analysis["user_id"])].append(analysis)

            required_jobs = sum(
                max(len(by_user_sessions[user_id]), len(by_user_analyses[user_id]))
                for user_id in set(by_user_sessions) | set(by_user_analyses)
            )
            if len(jobs) < required_jobs:
                raise RuntimeError(
                    f"Need {required_jobs} distinct sourced jobs, but only {len(jobs)} are available."
                )

            cur.execute(
                """
                select controlled.user_id::text as user_id, app_user.full_name
                from ss_users controlled
                join users app_user on app_user.id = controlled.user_id
                """
            )
            names = {row["user_id"]: row["full_name"] for row in cur.fetchall()}

            available = list(jobs)
            assignments: list[
                tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]
            ] = []
            extras: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for user_id in sorted(set(by_user_sessions) | set(by_user_analyses)):
                user_sessions = by_user_sessions[user_id]
                user_analyses = by_user_analyses[user_id]
                for index, session in enumerate(user_sessions):
                    analysis = user_analyses[index] if index < len(user_analyses) else None
                    job = _choose_job(session, analysis, available)
                    available.remove(job)
                    assignments.append((analysis, session, job))
                for analysis in user_analyses[len(user_sessions) :]:
                    placeholder_session = {
                        "position": analysis.get("target_position"),
                        "session_type": "technical",
                    }
                    job = _choose_job(placeholder_session, analysis, available)
                    available.remove(job)
                    extras.append((analysis, job))

            if not apply:
                conn.rollback()
                return {
                    "sessions": len(sessions),
                    "analyses_before": len(analyses),
                    "required_jobs": required_jobs,
                    "available_jobs": len(jobs),
                    "new_analyses": sum(1 for analysis, _, _ in assignments if analysis is None),
                }

            updated_analyses: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
            ordinal = 0
            for analysis, session, job in assignments:
                ordinal += 1
                user_id = str(session["user_id"])
                if analysis is None:
                    analysis = _create_analysis(cur, session, names[user_id])
                cv_payload = _update_analysis(
                    cur,
                    analysis=analysis,
                    full_name=names[user_id],
                    job=job,
                    ordinal=ordinal,
                )
                updated_analyses[str(analysis["id"])] = (job, cv_payload)
                _update_session(
                    cur,
                    session=session,
                    analysis=analysis,
                    job=job,
                    cv_payload=cv_payload,
                )

            for analysis, job in extras:
                ordinal += 1
                user_id = str(analysis["user_id"])
                cv_payload = _update_analysis(
                    cur,
                    analysis=analysis,
                    full_name=names[user_id],
                    job=job,
                    ordinal=ordinal,
                )
                updated_analyses[str(analysis["id"])] = (job, cv_payload)

            cur.execute(
                """
                update learning_roadmaps roadmap
                set
                  target_role = latest.target_position,
                  match_percent = latest.ats_score,
                  updated_at = now()
                from (
                  select distinct on (analysis.user_id)
                    analysis.user_id,
                    analysis.target_position,
                    analysis.ats_score
                  from cv_jd_analyses analysis
                  join ss_users controlled on controlled.user_id = analysis.user_id
                  order by analysis.user_id, analysis.created_at desc, analysis.id desc
                ) latest
                where roadmap.user_id = latest.user_id
                """
            )
        conn.commit()

    return {
        "sessions": len(sessions),
        "analyses_after": len(updated_analyses),
        "distinct_jobs_used": len(updated_analyses),
        "new_analyses": sum(1 for analysis, _, _ in assignments if analysis is None),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(refresh(apply=args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
