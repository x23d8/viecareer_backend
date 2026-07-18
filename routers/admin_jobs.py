import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from core.clerk_auth import AdminUser, require_admin
from core.rate_limit import limiter
from schemas.admin_jobs import AdminJobCrawlCommitRequest, AdminJobCrawlPreviewRequest
from services.admin_job_crawler_service import (
    commit_crawl_preview,
    create_crawl_preview,
    get_crawler_sources,
)

router = APIRouter(prefix="/api/admin/job-crawler", tags=["admin-job-crawler"])


@router.get("/sources")
@limiter.limit("30/minute")
def list_crawler_sources(
    request: Request,
    current_admin: AdminUser = Depends(require_admin),
):
    del request, current_admin
    return {"data": get_crawler_sources()}


@router.post("/preview")
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


@router.post("/commit")
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
