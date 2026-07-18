import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from core.clerk_auth import AdminUser, require_admin
from core.rate_limit import limiter
from schemas.admin_jobs import (
    AdminJobCrawlCommitRequest,
    AdminJobCrawlPreviewRequest,
    AdminManualJobCreateRequest,
)
from services.admin_job_crawler_service import (
    commit_crawl_preview,
    create_crawl_preview,
    get_crawler_sources,
)
from services.admin_job_service import create_manual_job

router = APIRouter(prefix="/api/admin", tags=["admin-jobs"])
logger = logging.getLogger("viecareer")


@router.get("/job-crawler/sources")
@limiter.limit("30/minute")
def list_crawler_sources(
    request: Request,
    current_admin: AdminUser = Depends(require_admin),
):
    del request, current_admin
    return {"data": get_crawler_sources()}


@router.post("/job-crawler/preview")
@limiter.limit("6/minute")
async def preview_jobs(
    request: Request,
    payload: AdminJobCrawlPreviewRequest,
    current_admin: AdminUser = Depends(require_admin),
):
    del request
    result = await asyncio.to_thread(
        create_crawl_preview,
        admin_id=current_admin.internal_user_id,
        sources=payload.sources,
        job_type=payload.job_type,
        level=payload.level,
        max_results=payload.max_results,
    )
    return {"data": result}


@router.post("/job-crawler/commit")
@limiter.limit("10/minute")
async def commit_jobs(
    request: Request,
    payload: AdminJobCrawlCommitRequest,
    current_admin: AdminUser = Depends(require_admin),
):
    del request
    try:
        result = await asyncio.to_thread(
            commit_crawl_preview,
            admin_id=current_admin.internal_user_id,
            preview_id=str(payload.preview_id),
            quantity=payload.quantity,
            selected_keys=payload.selected_keys,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"data": result}


@router.post("/jobs/manual", status_code=201)
@limiter.limit("20/minute")
async def add_manual_job(
    request: Request,
    payload: AdminManualJobCreateRequest,
    current_admin: AdminUser = Depends(require_admin),
):
    del request
    try:
        result = await asyncio.to_thread(
            create_manual_job,
            admin_id=current_admin.internal_user_id,
            request=payload,
        )
    except Exception as exc:
        logger.exception("Could not create manual job admin=%s", current_admin.internal_user_id)
        raise HTTPException(status_code=503, detail="Could not save the manual job.") from exc
    return {"data": result}
