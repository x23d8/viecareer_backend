"""Interview simulation endpoints independent from any frontend workflow."""

from fastapi import APIRouter, Depends, Request

from core.api_usage import usage_context
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
        return {"data": service.generate_blueprint(req)}


@router.post("/coding-plan")
@limiter.limit("5/minute")
async def generate_coding_plan(
    request: Request,
    req: GenerateCodingPlanRequest,
    current_user: ClerkUser = Depends(require_auth),
    client=Depends(get_openai_client),
):
    """Generate role-specific practical coding challenges."""
    await check_daily_quota()
    await check_user_limit_ai(current_user.user_id)
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
        return {"data": service.generate_coding_plan(req)}


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
        return {"data": service.select_next_question(req)}
