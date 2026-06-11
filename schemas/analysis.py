"""Request schemas for the CV/JD extraction and interview scoring endpoints."""

from typing import Any, List, Literal

from pydantic import BaseModel, EmailStr, Field


class ExtractCVJDRequest(BaseModel):
    cv_text: str
    jd_text: str
    external_auth_id: str | None = None  # Clerk user_id; used for user-level rate limiting


class SaveCVJDAnalysisRequest(BaseModel):
    external_auth_id: str = Field(..., min_length=1)
    email: EmailStr
    account_full_name: str
    avatar_url: str | None = None
    auth_provider: str = "clerk"
    analysis: dict[str, Any]


class TranscriptEvent(BaseModel):
    role: Literal["user", "assistant", "system"]
    text: str
    timestamp_sec: int | None = None
    source: str | None = "gpt-realtime"


class ScoreInterviewRequest(BaseModel):
    qa_pairs: List[dict]  # [{question, answer, timestamp}]
    position: str
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
    position: str
    duration_seconds: int = 0
    overall_score: int = 0
    session_score: int = 0
    transcript_events: List[TranscriptEvent] = Field(default_factory=list)
    scored_qa: List[dict[str, Any]] = Field(default_factory=list)
    interview_dimensions: dict[str, Any] = Field(default_factory=dict)
    final_dimensions: dict[str, Any] = Field(default_factory=dict)
    phase2_signals: List[dict[str, Any]] = Field(default_factory=list)
