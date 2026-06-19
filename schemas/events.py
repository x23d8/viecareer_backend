from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator


LandingEventName = Literal["landing_view", "pricing_click", "signup_click", "login_click"]
LandingSource = Literal["facebook", "tiktok", "google", "direct", "other"]
LandingMetricGroupBy = Literal["day", "week", "month"]


class LandingEventRequest(BaseModel):
    visitor_id: str = Field(..., min_length=8, max_length=100)
    event_name: LandingEventName
    source: LandingSource = "direct"
    utm_source: str | None = Field(default=None, max_length=100)
    utm_medium: str | None = Field(default=None, max_length=100)
    utm_campaign: str | None = Field(default=None, max_length=150)
    referrer: str | None = Field(default=None, max_length=2048)
    path: str | None = Field(default=None, max_length=2048)
    target_path: str | None = Field(default=None, max_length=2048)
    pricing_plan_code: str | None = Field(default=None, max_length=50)

    @field_validator("source", mode="before")
    @classmethod
    def normalize_source(cls, value: object) -> str:
        if value in (None, ""):
            return "direct"

        normalized = str(value).strip().lower()
        if normalized in {"facebook", "fb"}:
            return "facebook"
        if normalized in {"tiktok", "tik_tok"}:
            return "tiktok"
        if normalized in {"google", "google_ads", "googleads"}:
            return "google"
        if normalized in {"direct", "web", "website"}:
            return "direct"
        return "other"


class LandingMetricsQuery(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    source: LandingSource | None = None
    group_by: LandingMetricGroupBy = "day"
