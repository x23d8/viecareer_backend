"""Analysis endpoints — CV/JD extraction and interview scoring."""

import time

from fastapi import APIRouter, Depends, HTTPException, Request, status

from core.api_usage import track_blocked_ai_usage_async, track_user_activity_async, usage_context
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
from services.cvjd_spam_guard import (
    CVJDSpamRejected,
    get_cached_cvjd_analysis,
    store_cvjd_analysis_cache,
    validate_cvjd_request,
)
from services.cv_jd_analysis_service import save_cv_jd_analysis
from services.interview_report_service import save_interview_report
from services.plan_quota_service import FEATURE_CV_JD_ANALYSIS, METRIC_COUNT, check_plan_quota

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
    started_at = time.perf_counter()
    usage = usage_context(
        feature="cvjd_extract",
        external_auth_id=current_user.user_id,
        meta={"endpoint": "/extract-cvjd"},
    )
    try:
        fingerprint = validate_cvjd_request(req.cv_text, req.jd_text)
    except CVJDSpamRejected as exc:
        track_blocked_ai_usage_async(
            usage,
            error_code=exc.code,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            meta={
                "endpoint": "/extract-cvjd",
                "reason": exc.code,
                **exc.meta,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": True,
                "code": exc.code,
                "message": exc.message,
                "retry": False,
                "meta": exc.meta,
            },
        ) from exc

    cached_reply = get_cached_cvjd_analysis(fingerprint.input_hash)
    if cached_reply is not None:
        track_user_activity_async(
            "cvjd_cache_hit",
            external_auth_id=current_user.user_id,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            meta={
                "endpoint": "/extract-cvjd",
                "input_hash_prefix": fingerprint.input_hash[:12],
                "cv_chars": fingerprint.cv_chars,
                "jd_chars": fingerprint.jd_chars,
            },
        )
        return {"reply": cached_reply, "cached": True}

    await check_user_limit_ai(current_user.user_id)
    await check_plan_quota(
        external_auth_id=current_user.user_id,
        feature_code=FEATURE_CV_JD_ANALYSIS,
        metric=METRIC_COUNT,
    )
    await check_daily_quota()
    async with ai_concurrency_limit():
        reply = extract_cvjd(
            client,
            req,
            usage_context=usage_context(
                feature="cvjd_extract",
                external_auth_id=current_user.user_id,
                meta={
                    "endpoint": "/extract-cvjd",
                    "cv_chars": fingerprint.cv_chars,
                    "jd_chars": fingerprint.jd_chars,
                    "input_hash_prefix": fingerprint.input_hash[:12],
                },
            ),
        )
        store_cvjd_analysis_cache(
            fingerprint.input_hash,
            reply,
            meta={
                "external_auth_id": current_user.user_id,
                "cv_chars": fingerprint.cv_chars,
                "jd_chars": fingerprint.jd_chars,
            },
        )
        return {
            "reply": reply,
            "cached": False,
        }


@router.post("/cv-jd-analyses")
@limiter.limit("20/minute")
async def save_cv_jd_analysis_route(
    request: Request,
    req: SaveCVJDAnalysisRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    """Persist an already-extracted CV/JD analysis for the authenticated user."""
    started_at = time.perf_counter()
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
    track_user_activity_async(
        "cvjd_saved",
        external_auth_id=current_user.user_id,
        object_type="cv_jd_analysis",
        object_id=str(row.get("id") or ""),
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        meta={
            "target_position": req.analysis.get("targetPosition"),
            "ats_score": req.analysis.get("atsScore"),
        },
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
        return {
            "reply": score_interview(
                client,
                req,
                usage_context=usage_context(
                    feature="interview_score",
                    external_auth_id=current_user.user_id,
                    related_type="cv_jd_analysis" if req.cv_jd_analysis_id else None,
                    related_id=req.cv_jd_analysis_id,
                    meta={
                        "endpoint": "/score-interview",
                        "qa_count": len(req.qa_pairs or []),
                        "transcript_events": len(req.transcript_events or []),
                        "position": req.position,
                    },
                ),
            )
        }


@router.post("/interview-reports")
@limiter.limit("20/minute")
async def save_interview_report_route(
    request: Request,
    req: SaveInterviewReportRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    """Persist a completed interview report, including transcript events and final dimensions."""
    started_at = time.perf_counter()
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
        live_session_id=req.live_session_id,
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
    track_user_activity_async(
        "report_saved",
        external_auth_id=current_user.user_id,
        object_type="interview_session",
        object_id=str(row.get("id") or ""),
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        meta={
            "position": req.position,
            "duration_seconds": req.duration_seconds,
            "overall_score": req.overall_score,
            "qa_count": len(req.scored_qa or []),
            "transcript_events": len(req.transcript_events or []),
        },
    )
    return {"data": row}
