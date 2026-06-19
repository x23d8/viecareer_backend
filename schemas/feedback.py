from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


FeedbackEmotion = Literal["very_bad", "bad", "neutral", "good", "great"]
FeedbackSource = Literal["interview", "coding", "report"]
FeedbackTag = Literal[
    "too_hard",
    "too_easy",
    "audio_issue",
    "useful",
    "incorrect_feedback",
]


class SubmitFeedbackRequest(BaseModel):
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
