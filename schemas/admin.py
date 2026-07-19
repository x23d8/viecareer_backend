from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class AdminUserStatusRequest(BaseModel):
    status: Literal["active", "suspended"]


AdminReportPage = Literal[
    "overview",
    "users",
    "sessions",
    "activity",
    "feedback",
    "ai_costs",
]


class AdminReportExportRequest(BaseModel):
    pages: list[AdminReportPage] = Field(min_length=1, max_length=6)
    date_from: date | None = None
    date_to: date | None = None
    group_by: Literal["day", "week", "month"] = "day"
    plan_filter: Literal["all", "free", "paid"] = "all"

    @field_validator("pages")
    @classmethod
    def pages_must_be_unique(cls, value: list[AdminReportPage]) -> list[AdminReportPage]:
        if len(value) != len(set(value)):
            raise ValueError("pages must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_date_range(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        return self
