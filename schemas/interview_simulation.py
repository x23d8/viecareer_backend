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
    cv_text: str = ""
    jd_text: str = Field(..., min_length=1)
    extracted_analysis: dict[str, Any] | None = None


class GenerateInterviewBlueprintRequest(InterviewContext):
    target_question_count: int = Field(default=8, ge=4, le=20)
    language: InterviewLanguage = "en"
    interview_style: InterviewStyle = "mixed"


class GenerateCodingPlanRequest(InterviewContext):
    language: InterviewLanguage = "en"
    role_level: str | None = None
    challenge_count: int = Field(default=1, ge=1, le=3)


class InterviewTurn(BaseModel):
    role: Literal["assistant", "user", "system"]
    text: str
    question_id: str | None = None
    timestamp_sec: int | None = None


class SelectNextQuestionRequest(BaseModel):
    blueprint: dict[str, Any]
    transcript: list[InterviewTurn] = Field(default_factory=list)
    answered_question_ids: list[str] = Field(default_factory=list)
    covered_topics: list[str] = Field(default_factory=list)
    language: InterviewLanguage = "en"
    answered_count_by_group: dict[str, int] = Field(default_factory=dict)
