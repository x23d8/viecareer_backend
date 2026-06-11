"""Analysis endpoints — CV/JD extraction and interview scoring."""

from fastapi import APIRouter, Depends, Request

from core.clerk_auth import ClerkUser, require_auth
from core.openai_client import get_openai_client
from core.rate_limit import (
    ai_concurrency_limit,
    check_daily_quota,
    check_user_limit_ai,
    check_user_limit_score,
    limiter,
)
from schemas.analysis import (
    ExtractCVJDRequest,
    SaveCVJDAnalysisRequest,
    SaveInterviewReportRequest,
    ScoreInterviewRequest,
)
from services.analysis_service import extract_cvjd, score_interview
from services.cv_jd_analysis_service import save_cv_jd_analysis
from services.interview_report_service import save_interview_report

router = APIRouter(tags=["analysis"])


@router.post("/extract-cvjd")
@limiter.limit("10/minute")
async def extract_cvjd_route(
    request: Request,
    req: ExtractCVJDRequest,
    current_user: ClerkUser = Depends(require_auth),
    client=Depends(get_openai_client),
):
    """Analyze CV and JD to extract structured profile, ATS score, and gap analysis."""
    await check_daily_quota()
    await check_user_limit_ai(current_user.user_id)
    async with ai_concurrency_limit():
        return {"reply": extract_cvjd(client, req)}


@router.post("/cv-jd-analyses")
@limiter.limit("20/minute")
async def save_cv_jd_analysis_route(
    request: Request,
    req: SaveCVJDAnalysisRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    """Persist an already-extracted CV/JD analysis for the authenticated user."""
    # Ensure users can only save their own analyses
    if current_user.user_id != req.external_auth_id:
        from fastapi import HTTPException, status
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot save analysis for another user.")
    row = save_cv_jd_analysis(
        external_auth_id=req.external_auth_id,
        email=str(req.email),
        account_full_name=req.account_full_name,
        avatar_url=req.avatar_url,
        auth_provider=req.auth_provider,
        analysis=req.analysis,
    )
    return {"data": row}


@router.post("/score-interview")
@limiter.limit("10/minute")
async def score_interview_route(
    request: Request,
    req: ScoreInterviewRequest,
    current_user: ClerkUser = Depends(require_auth),
    client=Depends(get_openai_client),
):
    """Score interview Q&A pairs across 5 dimensions and return detailed analysis."""
    await check_daily_quota()
    await check_user_limit_score(current_user.user_id)
    async with ai_concurrency_limit():
        return {"reply": score_interview(client, req)}


@router.post("/interview-reports")
@limiter.limit("20/minute")
async def save_interview_report_route(
    request: Request,
    req: SaveInterviewReportRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    """Persist a completed interview report, including transcript events and final dimensions."""
    # Ensure users can only save their own reports
    if current_user.user_id != req.external_auth_id:
        from fastapi import HTTPException, status
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot save report for another user.")
    await check_user_limit_score(current_user.user_id)
    row = save_interview_report(
        external_auth_id=req.external_auth_id,
        email=str(req.email),
        account_full_name=req.account_full_name,
        avatar_url=req.avatar_url,
        auth_provider=req.auth_provider,
        cv_jd_analysis_id=req.cv_jd_analysis_id,
        position=req.position,
        duration_seconds=req.duration_seconds,
        overall_score=req.overall_score,
        session_score=req.session_score,
        transcript_events=req.transcript_events,
        scored_qa=req.scored_qa,
        interview_dimensions=req.interview_dimensions,
        final_dimensions=req.final_dimensions,
        phase2_signals=req.phase2_signals,
    )
    return {"data": row}
