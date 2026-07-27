from fastapi import APIRouter, Depends, Query, Request, status

from core.clerk_auth import AdminUser, ClerkUser, require_admin, require_auth
from core.rate_limit import limiter
from schemas.feedback import (
    CreateCodingSessionRequest,
    FeedbackEmotion,
    FeedbackSource,
    FeedbackTag,
    SubmitFeedbackRequest,
    SubmitUserReportRequest,
)
from services.feedback_service import (
    create_completed_coding_session,
    list_admin_feedback,
    submit_user_report,
    submit_session_feedback,
)

router = APIRouter(tags=["feedback"])


@router.post("/api/feedback", status_code=status.HTTP_201_CREATED)
@limiter.limit("20/minute")
def submit_feedback(
    request: Request,
    payload: SubmitFeedbackRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    """Create or update feedback for one authenticated user's session and source."""
    del request
    return {
        "data": submit_session_feedback(
            external_auth_id=current_user.user_id,
            email=current_user.email,
            session_id=str(payload.session_id),
            source=payload.source,
            emotion=payload.emotion,
            rating=payload.rating,
            comment=payload.comment,
            tags=list(payload.tags),
        )
    }


@router.post("/api/feedback/reports", status_code=status.HTTP_201_CREATED)
@limiter.limit("20/minute")
def submit_general_report(
    request: Request,
    payload: SubmitUserReportRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    """Accept feedback and bug reports without requiring a CV or interview session."""

    del request
    return {
        "data": submit_user_report(
            external_auth_id=current_user.user_id,
            email=current_user.email,
            kind=payload.kind,
            feature=payload.feature,
            path=payload.path,
            comment=payload.comment,
        )
    }


@router.post("/api/feedback/coding-sessions", status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
def create_coding_session(
    request: Request,
    payload: CreateCodingSessionRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    """Create the completed coding session that subsequent feedback must reference."""
    del request
    return {
        "data": create_completed_coding_session(
            external_auth_id=current_user.user_id,
            email=current_user.email,
            position=payload.position,
            company=payload.company,
            duration_seconds=payload.duration_seconds,
            overall_score=payload.overall_score,
        )
    }


@router.get("/api/admin/feedback")
@limiter.limit("30/minute")
def get_admin_feedback(
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: str | None = Query(default=None, max_length=120),
    negative_only: bool = Query(default=False),
    source: FeedbackSource | None = Query(default=None),
    emotion: FeedbackEmotion | None = Query(default=None),
    rating_max: int | None = Query(default=None, ge=1, le=5),
    tag: FeedbackTag | None = Query(default=None),
    current_admin: AdminUser = Depends(require_admin),
):
    """List session feedback; negative_only includes bad emotions or ratings of 1-2."""
    del request, current_admin
    return {
        "data": list_admin_feedback(
            limit=limit,
            offset=offset,
            search=search,
            negative_only=negative_only,
            source=source,
            emotion=emotion,
            rating_max=rating_max,
            tag=tag,
        )
    }
