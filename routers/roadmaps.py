"""Authenticated APIs for per-CV/JD personalized roadmaps."""

from fastapi import APIRouter, Depends, Request

from core.clerk_auth import ClerkUser, require_auth
from core.rate_limit import limiter
from schemas.roadmaps import (
    AddRoadmapTaskRequest,
    CreateRoadmapRequest,
    UpdateRoadmapTaskRequest,
)
from services.roadmap_service import (
    add_roadmap_task,
    get_or_create_roadmap,
    get_roadmap,
    list_roadmaps,
    update_roadmap_task,
)

router = APIRouter(prefix="/api/roadmaps", tags=["roadmaps"])


@router.get("")
@limiter.limit("60/minute")
def list_user_roadmaps(
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
):
    del request
    return {"data": list_roadmaps(external_auth_id=current_user.user_id)}


@router.post("")
@limiter.limit("30/minute")
def create_user_roadmap(
    request: Request,
    payload: CreateRoadmapRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    del request
    return {
        "data": get_or_create_roadmap(
            external_auth_id=current_user.user_id,
            cv_jd_analysis_id=str(payload.cv_jd_analysis_id),
            language=payload.language,
        )
    }


@router.get("/{roadmap_id}")
@limiter.limit("60/minute")
def get_user_roadmap(
    request: Request,
    roadmap_id: str,
    current_user: ClerkUser = Depends(require_auth),
):
    del request
    return {"data": get_roadmap(external_auth_id=current_user.user_id, roadmap_id=roadmap_id)}


@router.post("/{roadmap_id}/tasks")
@limiter.limit("60/minute")
def add_user_roadmap_task(
    request: Request,
    roadmap_id: str,
    payload: AddRoadmapTaskRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    del request
    return {
        "data": add_roadmap_task(
            external_auth_id=current_user.user_id,
            roadmap_id=roadmap_id,
            task=payload.model_dump(),
        )
    }


@router.patch("/{roadmap_id}/tasks/{task_id}")
@limiter.limit("60/minute")
def update_user_roadmap_task(
    request: Request,
    roadmap_id: str,
    task_id: int,
    payload: UpdateRoadmapTaskRequest,
    current_user: ClerkUser = Depends(require_auth),
):
    del request
    return {
        "data": update_roadmap_task(
            external_auth_id=current_user.user_id,
            roadmap_id=roadmap_id,
            task_id=task_id,
            task_status=payload.status,
            completion_note=payload.completion_note,
        )
    }
