from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ErrorCategory = Literal[
    "authentication",
    "network",
    "api",
    "validation",
    "database",
    "realtime",
    "media",
    "ui",
    "unexpected",
]
ErrorSeverity = Literal["warning", "error", "fatal"]
ErrorPlatform = Literal["android", "web"]
ErrorReportStatus = Literal["open", "reviewing", "resolved"]

_FORBIDDEN_CONTEXT_KEYS = {
    "authorization",
    "cookie",
    "password",
    "passcode",
    "secret",
    "token",
    "access_token",
    "refresh_token",
}


class SubmitErrorReportRequest(BaseModel):
    """Structured client failure. User identity always comes from the verified JWT."""

    model_config = ConfigDict(extra="forbid")

    category: ErrorCategory
    severity: ErrorSeverity = "error"
    platform: ErrorPlatform
    location: str = Field(..., min_length=1, max_length=200)
    route: str | None = Field(default=None, max_length=500)
    error_type: str = Field(..., min_length=1, max_length=160)
    message: str = Field(..., min_length=1, max_length=2000)
    technical_detail: str | None = Field(default=None, max_length=12000)
    stack_trace: str | None = Field(default=None, max_length=16000)
    http_status: int | None = Field(default=None, ge=100, le=599)
    request_id: str | None = Field(default=None, max_length=160)
    app_version: str | None = Field(default=None, max_length=80)
    occurred_at_epoch_ms: int | None = Field(default=None, ge=0, le=4_102_444_800_000)
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("location", "error_type", "message")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("route", "technical_detail", "stack_trace", "request_id", "app_version")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None

    @field_validator("context")
    @classmethod
    def validate_context(cls, value: dict[str, Any]) -> dict[str, Any]:
        if any(str(key).strip().lower() in _FORBIDDEN_CONTEXT_KEYS for key in value):
            raise ValueError("context contains a sensitive key")
        # Prevent the context bag from bypassing the request body limits.
        if len(str(value)) > 12_000:
            raise ValueError("context is too large")
        return value


class UpdateErrorReportStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ErrorReportStatus
