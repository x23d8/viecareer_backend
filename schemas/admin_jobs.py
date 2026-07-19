from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from schemas.jobs import EmploymentType, WorkplaceType

CrawlLevel = Literal["internship", "fresher", "junior", "middle", "senior", "lead", "manager"]
CrawlerSource = Literal["linkedin", "topcv", "itviec"]


class AdminJobCrawlPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[CrawlerSource] = Field(..., min_length=1, max_length=3)
    job_type: str = Field(..., min_length=2, max_length=120)
    level: CrawlLevel | None = None
    location: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    experience_years_min: int | None = Field(default=None, ge=0, le=50)
    experience_years_max: int | None = Field(default=None, ge=0, le=50)
    max_results: int = Field(default=20, ge=1, le=100)

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, values: list[CrawlerSource]) -> list[CrawlerSource]:
        return list(dict.fromkeys(values))

    @field_validator("job_type")
    @classmethod
    def normalize_job_type(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("location", "company")
    @classmethod
    def normalize_optional_filter(cls, value: str | None) -> str | None:
        normalized = " ".join((value or "").split())
        return normalized or None

    @model_validator(mode="after")
    def validate_experience_range(self):
        if (
            self.experience_years_min is not None
            and self.experience_years_max is not None
            and self.experience_years_max < self.experience_years_min
        ):
            raise ValueError("experience_years_max must be greater than or equal to experience_years_min")
        return self


class AdminJobCrawlCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: UUID
    quantity: int = Field(..., ge=1, le=100)
    selected_keys: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("selected_keys")
    @classmethod
    def normalize_selected_keys(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            key = value.strip()
            if key and key not in normalized:
                normalized.append(key[:320])
        return normalized


class AdminManualJobCreateRequest(BaseModel):
    """Validated admin input for a first-party, manually entered job posting."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=2, max_length=300)
    company_name: str = Field(..., min_length=2, max_length=300)
    source_url: HttpUrl
    apply_url: HttpUrl | None = None
    company_logo_url: HttpUrl | None = None
    location: str | None = Field(default=None, max_length=300)
    workplace_type: WorkplaceType | None = None
    employment_type: EmploymentType | None = None
    experience_level: CrawlLevel | None = None
    experience_years_min: int | None = Field(default=None, ge=0, le=50)
    experience_years_max: int | None = Field(default=None, ge=0, le=50)
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default="VND", max_length=10)
    salary_text: str | None = Field(default=None, max_length=200)
    description: str = Field(..., min_length=120, max_length=100_000)
    skills: list[str] = Field(default_factory=list, max_length=100)
    posted_at: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("title", "company_name")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("location", "salary_text")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        normalized = " ".join((value or "").split())
        return normalized or None

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return "\n".join(line.rstrip() for line in value.strip().splitlines())

    @field_validator("skills")
    @classmethod
    def normalize_skills(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            skill = " ".join(value.split())[:100]
            key = skill.casefold()
            if skill and key not in seen:
                normalized.append(skill)
                seen.add(key)
        return normalized

    @field_validator("salary_currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        normalized = (value or "").strip().upper()
        return normalized or None

    @model_validator(mode="after")
    def validate_ranges(self):
        if (
            self.experience_years_min is not None
            and self.experience_years_max is not None
            and self.experience_years_max < self.experience_years_min
        ):
            raise ValueError("experience_years_max must be greater than or equal to experience_years_min")
        if self.salary_min is not None and self.salary_max is not None and self.salary_max < self.salary_min:
            raise ValueError("salary_max must be greater than or equal to salary_min")
        return self
