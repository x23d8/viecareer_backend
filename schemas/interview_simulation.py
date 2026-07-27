"""Contracts for backend-owned interview simulation flows."""

from typing import Any, Literal

from pydantic import BaseModel, Field


InterviewLanguage = Literal["vi", "en"]
InterviewStyle = Literal["technical", "behavioral", "mixed"]
InterviewVoice = Literal["shimmer", "echo", "alloy", "nova", "onyx", "fable"]
QuestionGroup = Literal[
    "personal_experience",
    "cv_tech_deep_dive",
    "role_specific_knowledge",
    "coding_problem_solving",
]


class InterviewContext(BaseModel):
    cv_text: str = Field(default="", max_length=60_000)
    jd_text: str = Field(..., min_length=1, max_length=50_000)
    extracted_analysis: dict[str, Any] | None = None


class GenerateInterviewBlueprintRequest(InterviewContext):
    target_question_count: int = Field(default=8, ge=4, le=20)
    duration_minutes: int = Field(default=20, ge=5, le=60)
    language: InterviewLanguage = "en"
    interview_style: InterviewStyle = "mixed"


class GenerateCodingPlanRequest(InterviewContext):
    language: InterviewLanguage = "en"
    role_level: str | None = None
    challenge_count: int = Field(default=1, ge=1, le=3)


class EvaluateCodeInterviewRequest(BaseModel):
    challenge: dict[str, Any] = Field(...)
    code: str = Field(..., min_length=1, max_length=80_000)
    language: str = Field(default="python", min_length=1, max_length=40)
    response_language: InterviewLanguage = "en"
    cv_jd_analysis_id: str | None = None
    live_session_id: str | None = None
    elapsed_seconds: int | None = Field(default=None, ge=0, le=24 * 60 * 60)


class InterviewTurn(BaseModel):
    role: Literal["assistant", "user", "system"]
    text: str
    question_id: str | None = None
    timestamp_sec: int | None = None


class SelectNextQuestionRequest(BaseModel):
    blueprint: dict[str, Any] = Field(...)
    transcript: list[InterviewTurn] = Field(default_factory=list, max_length=100)
    answered_question_ids: list[str] = Field(default_factory=list, max_length=100)
    covered_topics: list[str] = Field(default_factory=list, max_length=100)
    language: InterviewLanguage = "en"
    answered_count_by_group: dict[str, int] = Field(default_factory=dict)
