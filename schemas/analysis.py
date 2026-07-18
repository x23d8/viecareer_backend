"""Request schemas for the CV/JD extraction and interview scoring endpoints."""

from typing import Any, List, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class ExtractCVJDRequest(BaseModel):
    cv_text: str = Field(..., min_length=1, max_length=60_000)
    jd_text: str = Field(..., min_length=1, max_length=50_000)
    external_auth_id: str | None = None  # Clerk user_id; used for user-level rate limiting


class SaveCVJDAnalysisRequest(BaseModel):
    external_auth_id: str = Field(..., min_length=1)
    email: EmailStr
    account_full_name: str
    avatar_url: str | None = None
    auth_provider: str = "clerk"
    job_id: UUID | None = None
    analysis: dict[str, Any]


class TranscriptEvent(BaseModel):
    role: Literal["user", "assistant", "system"]
    text: str = Field(..., min_length=1, max_length=20_000)
    timestamp_sec: int | None = None
    source: str | None = "gpt-realtime"


class ScoreInterviewRequest(BaseModel):
    qa_pairs: List[dict] = Field(..., min_length=1, max_length=50)
    position: str = Field(..., min_length=1, max_length=255)
    response_language: Literal["vi", "en"] = "en"
    transcript_events: List[TranscriptEvent] = Field(default_factory=list)
    baseline_dimensions: dict[str, Any] | None = None
    cv_jd_analysis_id: str | None = None
    external_auth_id: str | None = None  # Clerk user_id; used for user-level rate limiting


class SaveInterviewReportRequest(BaseModel):
    external_auth_id: str = Field(..., min_length=1)
    email: EmailStr
    account_full_name: str
    avatar_url: str | None = None
    auth_provider: str = "clerk"
    cv_jd_analysis_id: str | None = None
    job_id: UUID | None = None
    live_session_id: str | None = None
    position: str = Field(..., min_length=1, max_length=255)
    duration_seconds: int = 0
    overall_score: int = 0
    session_score: int = 0
    transcript_events: List[TranscriptEvent] = Field(default_factory=list)
    scored_qa: List[dict[str, Any]] = Field(default_factory=list)
    interview_dimensions: dict[str, Any] = Field(default_factory=dict)
    final_dimensions: dict[str, Any] = Field(default_factory=dict)
    phase2_signals: List[dict[str, Any]] = Field(default_factory=list)
    code_interview: dict[str, Any] | None = None


class GenerateFinalReportRequest(BaseModel):
    live_interview: dict[str, Any] = Field(default_factory=dict)
    code_interview: dict[str, Any] | None = None
    baseline_dimensions: dict[str, Any] | None = None
    cv_jd_analysis_id: str | None = None
