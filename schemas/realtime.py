"""Contracts for realtime interview session context."""

from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from schemas.interview_simulation import InterviewLanguage, InterviewStyle, InterviewVoice


class RealtimeCandidate(BaseModel):
    """Candidate context that must remain attached to the interview session."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    profile: dict[str, Any] = Field(..., min_length=1)
    cv_text: str = Field(default="", max_length=100_000)

    @field_validator("name", mode="before")
    @classmethod
    def strip_candidate_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class RealtimeSessionMetadata(BaseModel):
    """Required context supplied before realtime audio is persisted."""

    model_config = ConfigDict(extra="forbid")

    external_auth_id: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    account_full_name: str = Field(..., min_length=1, max_length=255)
    avatar_url: str | None = None
    auth_provider: str = Field(default="clerk", min_length=1, max_length=50)
    cv_jd_analysis_id: str | None = None

    candidate: RealtimeCandidate
    position: str = Field(..., min_length=1, max_length=255)
    job_description: str = Field(..., min_length=1, max_length=100_000)
    language: InterviewLanguage
    voice: InterviewVoice
    blueprint: dict[str, Any]
    interview_style: InterviewStyle = "mixed"

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_frontend_keys(cls, raw: Any) -> Any:
        """Accept the older client.start shape while storing one canonical shape."""
        if not isinstance(raw, dict):
            return raw

        data = dict(raw)

        if "job_description" not in data and "jd_summary" in data:
            data["job_description"] = data["jd_summary"]
        if "blueprint" not in data and "interview_blueprint" in data:
            data["blueprint"] = data["interview_blueprint"]

        legacy_candidate_keys = (
            "candidate_name",
            "candidate_full_name",
            "candidate_cv_text",
            "cv_text",
            "extracted_analysis",
        )
        if "candidate" not in data and any(key in data for key in legacy_candidate_keys):
            extracted = data.get("extracted_analysis")
            profile = extracted if isinstance(extracted, dict) else {}
            name = (
                _string_value(data.get("candidate_name"))
                or _string_value(data.get("candidate_full_name"))
                or _profile_name(profile)
                or _string_value(data.get("account_full_name"))
            )
            cv_text = _string_value(data.get("cv_text")) or _string_value(data.get("candidate_cv_text"))
            if name or profile or cv_text:
                data["candidate"] = {
                    "name": name,
                    "profile": profile or {"name": name},
                    "cv_text": cv_text,
                }

        for key in (
            "candidate_name",
            "candidate_full_name",
            "candidate_cv_text",
            "cv_text",
            "extracted_analysis",
            "interview_blueprint",
            "jd_summary",
        ):
            data.pop(key, None)

        return data

    @field_validator(
        "external_auth_id",
        "account_full_name",
        "auth_provider",
        "position",
        "job_description",
        mode="before",
    )
    @classmethod
    def strip_required_strings(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("blueprint")
    @classmethod
    def require_blueprint_questions(cls, value: dict[str, Any]) -> dict[str, Any]:
        questions = value.get("questions")
        if not isinstance(questions, list) or not questions:
            raise ValueError("blueprint.questions must contain at least one question")
        return value


def _string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _profile_name(profile: dict[str, Any]) -> str:
    for key in ("fullName", "full_name", "candidateName", "candidate_name", "name"):
        value = _string_value(profile.get(key))
        if value:
            return value
    return ""
