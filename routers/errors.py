from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from core.clerk_auth import AdminUser, ClerkUser, require_admin, require_auth
from core.rate_limit import limiter
from schemas.errors import (
    ErrorCategory,
    ErrorPlatform,
    ErrorReportStatus,
    ErrorSeverity,
    SubmitErrorReportRequest,
    UpdateErrorReportStatusRequest,
)
from services.error_report_service import (
    list_admin_error_reports,
    submit_error_report,
    update_error_report_status,
)

router = APIRouter(tags=["errors"])


@router.post("/api/errors", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
def create_error_report(
    request: Request,
    payload: SubmitErrorReportRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    """Store one redacted client failure under the verified Clerk identity."""
    del request
    return {
        "data": submit_error_report(
            external_auth_id=current_user.user_id,
            email=current_user.email,
            **payload.model_dump(),
        )
    }


@router.get("/api/admin/errors")
@limiter.limit("30/minute")
def get_admin_error_reports(
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: str | None = Query(default=None, max_length=160),
    category: ErrorCategory | None = Query(default=None),
    platform: ErrorPlatform | None = Query(default=None),
    severity: ErrorSeverity | None = Query(default=None),
    report_status: ErrorReportStatus | None = Query(default=None, alias="status"),
    current_admin: AdminUser = Depends(require_admin),
):
    del request, current_admin
    return {
        "data": list_admin_error_reports(
            limit=limit,
            offset=offset,
            search=search,
            category=category,
            platform=platform,
            severity=severity,
            report_status=report_status,
        )
    }


@router.patch("/api/admin/errors/{report_id}")
@limiter.limit("30/minute")
def set_admin_error_report_status(
    request: Request,
    report_id: UUID,
    payload: UpdateErrorReportStatusRequest,
    current_admin: AdminUser = Depends(require_admin),
):
    del request, current_admin
    return {"data": update_error_report_status(report_id=str(report_id), report_status=payload.status)}
