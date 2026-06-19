"""Interview simulation endpoints independent from any frontend workflow."""

import asyncio
import time

from fastapi import APIRouter, Depends, Request

from core.api_usage import track_user_activity_async, usage_context
from core.clerk_auth import ClerkUser, require_auth
from core.openai_client import get_openai_client
from core.rate_limit import (
    ai_concurrency_limit,
    check_daily_quota,
    check_user_limit_ai,
    limiter,
)
from schemas.interview_simulation import (
    GenerateCodingPlanRequest,
    GenerateInterviewBlueprintRequest,
    SelectNextQuestionRequest,
)
from services.interview_simulation_service import InterviewSimulationService
from services.plan_quota_service import FEATURE_CODING_PLAN, METRIC_GENERATION_COUNT, check_plan_quota

router = APIRouter(prefix="/interview-simulation", tags=["interview-simulation"])


@router.post("/blueprint")
@limiter.limit("5/minute")
async def generate_interview_blueprint(
    request: Request,
    req: GenerateInterviewBlueprintRequest,
    current_user: ClerkUser = Depends(require_auth),
    client=Depends(get_openai_client),
):
    """Generate structured CV/JD analysis, adaptive question bank, and runtime policy."""
    started_at = time.perf_counter()
    await check_daily_quota()
    await check_user_limit_ai(current_user.user_id)
    async with ai_concurrency_limit():
        service = InterviewSimulationService(
            client,
            usage_context=usage_context(
                feature="interview_blueprint",
                external_auth_id=current_user.user_id,
                meta={
                    "endpoint": "/interview-simulation/blueprint",
                    "target_question_count": req.target_question_count,
                    "language": req.language,
                    "interview_style": req.interview_style,
                },
            ),
        )
        blueprint = await asyncio.to_thread(service.generate_blueprint, req)
        questions = blueprint.get("questions") if isinstance(blueprint, dict) else []
        track_user_activity_async(
            "action_plan_generated",
            external_auth_id=current_user.user_id,
            object_type="action_plan",
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            meta={
                "endpoint": "/interview-simulation/blueprint",
                "target_question_count": req.target_question_count,
                "generated_question_count": len(questions) if isinstance(questions, list) else 0,
                "language": req.language,
                "interview_style": req.interview_style,
            },
        )
        return {"data": blueprint}


@router.post("/coding-plan")
@limiter.limit("5/minute")
async def generate_coding_plan(
    request: Request,
    req: GenerateCodingPlanRequest,
    current_user: ClerkUser = Depends(require_auth),
    client=Depends(get_openai_client),
):
    """Generate role-specific practical coding challenges."""
    started_at = time.perf_counter()
    await check_user_limit_ai(current_user.user_id)
    await check_plan_quota(
        external_auth_id=current_user.user_id,
        feature_code=FEATURE_CODING_PLAN,
        metric=METRIC_GENERATION_COUNT,
    )
    await check_daily_quota()
    async with ai_concurrency_limit():
        service = InterviewSimulationService(
            client,
            usage_context=usage_context(
                feature="coding_plan",
                external_auth_id=current_user.user_id,
                meta={
                    "endpoint": "/interview-simulation/coding-plan",
                    "challenge_count": req.challenge_count,
                    "language": req.language,
                    "role_level": req.role_level,
                },
            ),
        )
        coding_plan = await asyncio.to_thread(service.generate_coding_plan, req)
        challenges = coding_plan.get("challenges") if isinstance(coding_plan, dict) else []
        track_user_activity_async(
            "coding_plan_generated",
            external_auth_id=current_user.user_id,
            object_type="coding_interview",
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            meta={
                "endpoint": "/interview-simulation/coding-plan",
                "requested_challenge_count": req.challenge_count,
                "generated_challenge_count": len(challenges) if isinstance(challenges, list) else 0,
                "language": req.language,
                "role_level": req.role_level,
            },
        )
        return {"data": coding_plan}


@router.post("/next-question")
@limiter.limit("30/minute")
async def select_next_question(
    request: Request,
    req: SelectNextQuestionRequest,
    current_user: ClerkUser = Depends(require_auth),
    client=Depends(get_openai_client),
):
    """Select the next adaptive question from a generated blueprint and transcript."""
    await check_daily_quota()
    async with ai_concurrency_limit():
        service = InterviewSimulationService(
            client,
            usage_context=usage_context(
                feature="next_question",
                external_auth_id=current_user.user_id,
                meta={
                    "endpoint": "/interview-simulation/next-question",
                    "answered_questions": len(req.answered_question_ids or []),
                    "transcript_turns": len(req.transcript or []),
                },
            ),
        )
        return {"data": await asyncio.to_thread(service.select_next_question, req)}
