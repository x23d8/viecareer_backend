import asyncio
import hmac
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status

from config import JOB_SYNC_SECRET
from core.api_usage import usage_context
from core.clerk_auth import ClerkUser, optional_auth, require_auth
from core.openai_client import get_openai_client
from core.rate_limit import ai_concurrency_limit, check_daily_quota, check_user_limit_ai, limiter
from schemas.jobs import (
    EmploymentType,
    ExperienceLevel,
    JobIngestionPayload,
    JobInteractionRequest,
    JobPreferencesRequest,
    JobSource,
    WorkplaceType,
)
from schemas.chat import ChatRequest
from services.job_ingestion_service import sync_authorized_sources
from services.job_market_service import (
    clear_recent_searches,
    get_job,
    get_job_practice_context,
    get_job_match_context,
    get_job_preferences,
    get_market_insights,
    list_recent_searches,
    list_saved_jobs,
    record_job_interaction,
    search_jobs,
    update_job_preferences,
    upsert_ingested_job,
)
from services.job_match_analysis_service import analyze_job_match, chat_about_job

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
@limiter.limit("60/minute")
def list_jobs(
    request: Request,
    q: str | None = Query(default=None, max_length=300),
    location: str | None = Query(default=None, max_length=300),
    workplace_type: list[WorkplaceType] = Query(default=[]),
    employment_type: list[EmploymentType] = Query(default=[]),
    experience_level: list[ExperienceLevel] = Query(default=[]),
    experience_years_min: int | None = Query(default=None, ge=0, le=50),
    experience_years_max: int | None = Query(default=None, ge=0, le=50),
    source: list[JobSource] = Query(default=[]),
    posted_within_days: int | None = Query(default=None, ge=1, le=365),
    sort: Literal["recommended", "newest"] = Query(default="recommended"),
    page: int = Query(default=1, ge=1, le=1000),
    page_size: int = Query(default=20, ge=1, le=50),
    current_user: ClerkUser | None = Depends(optional_auth),
):
    del request
    return {
        "data": search_jobs(
            external_auth_id=current_user.user_id if current_user else None,
            query=q,
            location=location,
            workplace_types=list(workplace_type),
            employment_types=list(employment_type),
            experience_levels=list(experience_level),
            experience_years_min=experience_years_min,
            experience_years_max=experience_years_max,
            sources=list(source),
            posted_within_days=posted_within_days,
            sort=sort,
            page=page,
            page_size=page_size,
        )
    }


@router.get("/market-insights")
@limiter.limit("60/minute")
def market_insights(request: Request):
    del request
    return {"data": get_market_insights()}


@router.get("/saved")
@limiter.limit("60/minute")
def saved_jobs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    current_user: ClerkUser = Depends(require_auth),
):
    del request
    return {"data": list_saved_jobs(external_auth_id=current_user.user_id, limit=limit)}


@router.get("/preferences")
@limiter.limit("60/minute")
def job_preferences(request: Request, current_user: ClerkUser = Depends(require_auth)):
    del request
    return {"data": get_job_preferences(external_auth_id=current_user.user_id)}


@router.put("/preferences")
@limiter.limit("30/minute")
def save_job_preferences(
    request: Request,
    payload: JobPreferencesRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    del request
    return {
        "data": update_job_preferences(
            external_auth_id=current_user.user_id,
            desired_titles=payload.desired_titles,
            locations=payload.locations,
            workplace_types=list(payload.workplace_types),
            employment_types=list(payload.employment_types),
        )
    }


@router.get("/recent-searches")
@limiter.limit("60/minute")
def recent_searches(
    request: Request,
    limit: int = Query(default=8, ge=1, le=20),
    current_user: ClerkUser = Depends(require_auth),
):
    del request
    return {"data": list_recent_searches(external_auth_id=current_user.user_id, limit=limit)}


@router.delete("/recent-searches", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/minute")
def delete_recent_searches(request: Request, current_user: ClerkUser = Depends(require_auth)):
    del request
    clear_recent_searches(external_auth_id=current_user.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sync")
@limiter.limit("5/minute")
async def sync_jobs(
    request: Request,
    source: Literal["linkedin", "topcv", "itviec"] | None = Query(default=None),
    x_job_sync_secret: str | None = Header(default=None),
):
    del request
    _require_sync_secret(x_job_sync_secret)
    return {"data": await asyncio.to_thread(sync_authorized_sources, source)}


@router.post("/ingestion/{source}")
@limiter.limit("10/minute")
async def ingest_authorized_feed(
    request: Request,
    source: JobSource,
    payloads: list[JobIngestionPayload],
    x_job_sync_secret: str | None = Header(default=None),
):
    """Accept normalized records from a licensed feed or operator-owned posting source."""
    del request
    _require_sync_secret(x_job_sync_secret)
    if len(payloads) > 500:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="At most 500 jobs per request.")
    job_ids = [
        await asyncio.to_thread(upsert_ingested_job, source=source, payload=payload)
        for payload in payloads
    ]
    return {"data": {"source": source, "upserted_count": len(job_ids), "job_ids": job_ids}}


@router.get("/{job_id}")
@limiter.limit("120/minute")
def job_detail(
    request: Request,
    job_id: str,
    current_user: ClerkUser | None = Depends(optional_auth),
):
    del request
    return {
        "data": get_job(
            job_id=job_id,
            external_auth_id=current_user.user_id if current_user else None,
        )
    }


@router.get("/{job_id}/practice-context")
@limiter.limit("30/minute")
def job_practice_context(
    request: Request,
    job_id: str,
    current_user: ClerkUser = Depends(require_auth),
):
    del request
    return {
        "data": get_job_practice_context(
            job_id=job_id,
            external_auth_id=current_user.user_id,
        )
    }


@router.post("/{job_id}/match-analysis")
@limiter.limit("10/minute")
async def job_match_analysis(
    request: Request,
    job_id: str,
    language: Literal["vi", "en"] = Query(default="vi"),
    current_user: ClerkUser = Depends(require_auth),
    client=Depends(get_openai_client),
):
    del request
    await check_user_limit_ai(current_user.user_id)
    context = await asyncio.to_thread(
        get_job_match_context,
        job_id=job_id,
        external_auth_id=current_user.user_id,
    )
    await check_daily_quota()
    async with ai_concurrency_limit():
        analysis = await asyncio.to_thread(
            analyze_job_match,
            client,
            context=context,
            language=language,
            usage_context=usage_context(
                feature="job_match_analysis",
                external_auth_id=current_user.user_id,
                related_type="job",
                related_id=job_id,
                meta={"endpoint": "/api/jobs/{job_id}/match-analysis", "language": language},
            ),
        )
    return {"data": analysis}


@router.post("/{job_id}/match-chat")
@limiter.limit("20/minute")
async def job_match_chat(
    request: Request,
    job_id: str,
    payload: ChatRequest,
    language: Literal["vi", "en"] = Query(default="vi"),
    current_user: ClerkUser = Depends(require_auth),
    client=Depends(get_openai_client),
):
    del request
    await check_user_limit_ai(current_user.user_id)
    context = await asyncio.to_thread(
        get_job_match_context,
        job_id=job_id,
        external_auth_id=current_user.user_id,
    )
    await check_daily_quota()
    async with ai_concurrency_limit():
        reply = await asyncio.to_thread(
            chat_about_job,
            client,
            context=context,
            language=language,
            message=payload.message,
            history=payload.history,
            usage_context=usage_context(
                feature="job_match_chat",
                external_auth_id=current_user.user_id,
                related_type="job",
                related_id=job_id,
                meta={"endpoint": "/api/jobs/{job_id}/match-chat", "language": language},
            ),
        )
    return {"data": {"reply": reply}}


@router.post("/{job_id}/events", status_code=status.HTTP_201_CREATED)
@limiter.limit("120/minute")
def job_event(
    request: Request,
    job_id: str,
    payload: JobInteractionRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    del request
    return {
        "data": record_job_interaction(
            external_auth_id=current_user.user_id,
            job_id=job_id,
            event_type=payload.event_type,
            context=payload.context,
            source=payload.source,
        )
    }


def _require_sync_secret(provided: str | None) -> None:
    if not JOB_SYNC_SECRET:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JOB_SYNC_SECRET is not configured.",
        )
    if not provided or not hmac.compare_digest(provided, JOB_SYNC_SECRET):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Invalid job sync secret.")
