from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


FeedbackEmotion = Literal["very_bad", "bad", "neutral", "good", "great"]
FeedbackSource = Literal["interview", "coding", "report"]
FeedbackTag = Literal[
    "too_hard",
    "too_easy",
    "audio_issue",
    "useful",
    "incorrect_feedback",
]
UserReportKind = Literal["feedback", "bug"]


class SubmitFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    source: FeedbackSource
    emotion: FeedbackEmotion
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)
    tags: list[FeedbackTag] = Field(default_factory=list, max_length=5)

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("tags")
    @classmethod
    def unique_tags(cls, value: list[FeedbackTag]) -> list[FeedbackTag]:
        return list(dict.fromkeys(value))


class CreateCodingSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: str = Field(default="Coding practice", min_length=1, max_length=255)
    company: str | None = Field(default=None, max_length=255)
    duration_seconds: int | None = Field(default=None, ge=0, le=86400)
    overall_score: int | None = Field(default=None, ge=0, le=100)

    @field_validator("position")
    @classmethod
    def normalize_position(cls, value: str) -> str:
        return value.strip()

    @field_validator("company")
    @classmethod
    def normalize_company(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class SubmitUserReportRequest(BaseModel):
    """Session-independent feedback or bug report from any product feature."""

    model_config = ConfigDict(extra="forbid")

    kind: UserReportKind
    feature: str = Field(..., min_length=1, max_length=80)
    path: str | None = Field(default=None, max_length=500)
    comment: str = Field(..., min_length=3, max_length=4000)

    @field_validator("feature", "comment")
    @classmethod
    def normalize_required_report_text(cls, value: str) -> str:
        normalized = " ".join(value.split()) if len(value) <= 100 else value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("path")
    @classmethod
    def normalize_report_path(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None
