"""Request models for personalized CV/JD roadmaps."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class CreateRoadmapRequest(BaseModel):
    cv_jd_analysis_id: UUID
    language: Literal["vi", "en"] = "vi"


class AddRoadmapTaskRequest(BaseModel):
    client_key: str = Field(..., min_length=1, max_length=64)
    skill_key: str = Field(..., min_length=1, max_length=255)
    title: str = Field(..., min_length=1, max_length=255)
    description: str = Field(..., min_length=1, max_length=4000)
    phase_name: str = Field(default="Mục tiêu bổ sung", max_length=255)
    task_type: Literal["technical", "performance", "behavioral"] = "technical"
    duration_minutes: int = Field(default=60, ge=5, le=1440)
    expected_boost: int = Field(default=0, ge=0, le=100)


class UpdateRoadmapTaskRequest(BaseModel):
    status: Literal["planned", "in_progress", "completed"]
    completion_note: str | None = Field(default=None, max_length=4000)
