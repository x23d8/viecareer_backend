"""Backend-owned interview simulation design and adaptive sequencing."""

import json
import logging
import re
from typing import Any

from fastapi import HTTPException

from core.llm_json import json_completion
from core.prompts import load_prompt
from schemas.interview_simulation import (
    GenerateCodingPlanRequest,
    GenerateInterviewBlueprintRequest,
    SelectNextQuestionRequest,
)

logger = logging.getLogger("viecareer")


class InterviewSimulationService:
    """Coordinates the interview simulation use cases without frontend assumptions."""

    def __init__(self, client):
        self.client = client

    def generate_blueprint(self, req: GenerateInterviewBlueprintRequest) -> dict[str, Any]:
        prompt = _blueprint_prompt(req)
        raw = json_completion(
            self.client,
            load_prompt("interview_blueprint_designer"),
            prompt,
            max_tokens=5000,
        )
        blueprint = _parse_json_object(raw)
        return _normalize_blueprint(blueprint, req.target_question_count)

    def generate_coding_plan(self, req: GenerateCodingPlanRequest) -> dict[str, Any]:
        prompt = _coding_plan_prompt(req)
        raw = json_completion(
            self.client,
            load_prompt("coding_plan_designer"),
            prompt,
            max_tokens=3500,
        )
        return _normalize_coding_plan(_parse_json_object(raw), req.challenge_count)

    def select_next_question(self, req: SelectNextQuestionRequest) -> dict[str, Any]:
        available = _available_questions(
            req.blueprint.get("questions", []),
            req.answered_question_ids,
            req.covered_topics,
        )
        if not available:
            return {
                "selectedQuestionId": None,
                "selectedQuestion": None,
                "decision": {
                    "answerStrength": "unknown",
                    "escalate": False,
                    "reason": "No eligible questions remain in the blueprint.",
                    "coveredTopicsToAdd": [],
                    "signals": [],
                },
            }

        # Compute answered count by group for balance-aware selection
        all_questions = req.blueprint.get("questions", [])
        answered_set = set(req.answered_question_ids)
        group_counts: dict[str, int] = {}
        for q in all_questions:
            if not isinstance(q, dict):
                continue
            if str(q.get("id", "")) in answered_set:
                grp = str(q.get("group") or "unknown")
                group_counts[grp] = group_counts.get(grp, 0) + 1

        # Inject computed group counts into the request for the prompt builder
        req_with_groups = req.model_copy(update={"answered_count_by_group": group_counts})

        prompt = _sequencer_prompt(req_with_groups, available)
        raw = json_completion(
            self.client,
            load_prompt("adaptive_question_selector"),
            prompt,
            max_tokens=1800,
        )
        decision = _parse_json_object(raw)
        return _repair_selected_question(decision, available)


_STYLE_ALLOCATION = {
    "mixed":      "personal experience 20%, CV tech deep dive 25%, role-specific knowledge 35%, coding/problem solving 20%",
    "technical":  "personal experience 10%, CV tech deep dive 35%, role-specific knowledge 40%, coding/problem solving 15%",
    "behavioral": "personal experience 40%, CV tech deep dive 15%, role-specific knowledge 35%, coding/problem solving 10%",
}


def _blueprint_prompt(req: GenerateInterviewBlueprintRequest) -> str:
    allocation_guide = _STYLE_ALLOCATION.get(req.interview_style, _STYLE_ALLOCATION["mixed"])
    return f"""Language: {req.language}
Target question count: {req.target_question_count}
Interview style: {req.interview_style}
Question allocation for this style: {allocation_guide}

Use these existing extracted fields when present, but do not depend on any frontend shape:
{json.dumps(req.extracted_analysis or {}, ensure_ascii=False, indent=2)}

CV:
{req.cv_text}

JD:
{req.jd_text}

Return a complete interview blueprint."""


def _coding_plan_prompt(req: GenerateCodingPlanRequest) -> str:
    role_level = req.role_level or "Infer from JD and CV"
    return f"""Language: {req.language}
Requested role level: {role_level}
Requested challenge count: {req.challenge_count}

Existing extracted fields:
{json.dumps(req.extracted_analysis or {}, ensure_ascii=False, indent=2)}

CV:
{req.cv_text}

JD:
{req.jd_text}

Return a coding interview plan."""


def _sequencer_prompt(req: SelectNextQuestionRequest, available: list[dict[str, Any]]) -> str:
    transcript = [
        turn.model_dump(exclude_none=True)
        for turn in req.transcript[-12:]
    ]
    total_questions = len(req.blueprint.get("questions", []))
    answered_count = len(req.answered_question_ids)
    progress_pct = round(answered_count / max(total_questions, 1) * 100)
    phase = "opening" if progress_pct < 20 else "closing" if progress_pct >= 80 else "middle"

    allocation = req.blueprint.get("questionAllocation", {})
    group_counts = req.answered_count_by_group or {}

    return f"""Language: {req.language}
Progress: {answered_count}/{total_questions} questions answered ({progress_pct}%).
Phase: {phase}

Answered question ids:
{json.dumps(req.answered_question_ids, ensure_ascii=False)}

Group balance so far (answered per group):
{json.dumps(group_counts, ensure_ascii=False)}

Target allocation:
{json.dumps(allocation, ensure_ascii=False)}

Covered topics:
{json.dumps(req.covered_topics, ensure_ascii=False)}

Recent transcript:
{json.dumps(transcript, ensure_ascii=False, indent=2)}

Available questions:
{json.dumps(available, ensure_ascii=False, indent=2)}

Blueprint runtime policy:
{json.dumps(req.blueprint.get("runtimePolicy", {}), ensure_ascii=False, indent=2)}

Choose the next question."""


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
            logger.warning("AI response did not contain JSON: %s", raw[:500])
            raise HTTPException(status_code=502, detail="AI response did not contain JSON")
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise HTTPException(status_code=502, detail="AI response must be a JSON object")
    return parsed


def _normalize_blueprint(blueprint: dict[str, Any], target_count: int) -> dict[str, Any]:
    questions = blueprint.get("questions")
    if not isinstance(questions, list):
        blueprint["questions"] = []
        questions = blueprint["questions"]

    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            continue
        question.setdefault("id", f"question_{index:03d}")
        question.setdefault("level", 1)
        question.setdefault("skipIf", [])
        question.setdefault("followUpIfStrong", None)
        question.setdefault("followUpIfWeak", None)

    blueprint.setdefault("questionAllocation", _default_allocation(target_count))
    blueprint.setdefault("runtimePolicy", {})
    blueprint["runtimePolicy"].setdefault(
        "openingQuestionId",
        questions[0].get("id") if questions and isinstance(questions[0], dict) else None,
    )
    blueprint["runtimePolicy"].setdefault(
        "scoringDimensions",
        [
            "Domain & Technical",
            "Communication",
            "Structured Thinking",
            "Initiative & Ownership",
            "Team Collaboration",
        ],
    )
    return blueprint


def _display_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _normalize_coding_plan(plan: dict[str, Any], challenge_count: int) -> dict[str, Any]:
    challenges = plan.get("challenges")
    if not isinstance(challenges, list):
        challenges = []

    normalized: list[dict[str, Any]] = []
    for index, challenge in enumerate(challenges[:challenge_count], start=1):
        if not isinstance(challenge, dict):
            continue

        public_tests = []
        raw_tests = challenge.get("publicTests") or challenge.get("public_tests") or []
        if isinstance(raw_tests, list):
            for test in raw_tests:
                if not isinstance(test, dict):
                    continue
                public_tests.append(
                    {
                        "input": _display_text(test.get("input")),
                        "expected": _display_text(test.get("expected") or test.get("output")),
                    }
                )

        hidden_ideas = challenge.get("hiddenTestIdeas") or challenge.get("hidden_test_ideas") or []
        if not isinstance(hidden_ideas, list):
            hidden_ideas = [hidden_ideas]

        normalized.append(
            {
                "id": _display_text(challenge.get("id") or f"challenge_{index:03d}"),
                "title": _display_text(challenge.get("title") or "Role-specific Coding Challenge"),
                "difficulty": _display_text(challenge.get("difficulty") or "Medium"),
                "workContext": _display_text(challenge.get("workContext") or challenge.get("work_context")),
                "prompt": _display_text(challenge.get("prompt") or challenge.get("description")),
                "starterFunction": _display_text(challenge.get("starterFunction") or challenge.get("starter_function")),
                "expectedApproach": _display_text(challenge.get("expectedApproach") or challenge.get("expected_approach")),
                "publicTests": public_tests,
                "hiddenTestIdeas": [_display_text(item) for item in hidden_ideas if _display_text(item)],
                "followUp": _display_text(challenge.get("followUp") or challenge.get("follow_up")),
            }
        )

    plan["challengeCount"] = len(normalized) or challenge_count
    plan["challenges"] = normalized
    plan.setdefault("roleLevel", "")
    plan.setdefault("roleFamily", "")
    return plan


def _default_allocation(target_count: int) -> dict[str, int]:
    personal = max(1, round(target_count * 0.20))
    cv_deep_dive = max(1, round(target_count * 0.25))
    role_specific = max(1, round(target_count * 0.35))
    coding = max(1, target_count - personal - cv_deep_dive - role_specific)
    return {
        "personalExperience": personal,
        "cvTechDeepDive": cv_deep_dive,
        "roleSpecificKnowledge": role_specific,
        "codingProblemSolving": coding,
    }


def _available_questions(
    questions: list[Any],
    answered_question_ids: list[str],
    covered_topics: list[str],
) -> list[dict[str, Any]]:
    answered = set(answered_question_ids)
    covered = set(covered_topics)
    available: list[dict[str, Any]] = []

    for question in questions:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or "")
        if question_id in answered:
            continue
        skip_if = question.get("skipIf") or []
        if isinstance(skip_if, list) and covered.intersection(str(item) for item in skip_if):
            continue
        available.append(question)

    return available


def _repair_selected_question(
    decision: dict[str, Any],
    available: list[dict[str, Any]],
) -> dict[str, Any]:
    by_id = {str(question.get("id")): question for question in available}
    selected_id = decision.get("selectedQuestionId")
    selected = by_id.get(str(selected_id)) if selected_id is not None else None

    if selected is None:
        selected = available[0]
        selected_id = selected.get("id")

    decision["selectedQuestionId"] = selected_id
    decision["selectedQuestion"] = selected
    decision.setdefault(
        "decision",
        {
            "answerStrength": "unknown",
            "reason": "Selected first eligible question after repairing sequencer output.",
            "coveredTopicsToAdd": [],
            "signals": [],
        },
    )
    return decision
