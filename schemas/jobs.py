from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


JobSource = Literal["viecareer", "linkedin", "topcv", "itviec"]
WorkplaceType = Literal["remote", "hybrid", "on-site"]
EmploymentType = Literal[
    "full-time",
    "part-time",
    "contract",
    "internship",
    "temporary",
    "freelance",
    "other",
]
ExperienceLevel = Literal["internship", "fresher", "junior", "middle", "senior", "lead", "manager"]
JobInteractionType = Literal["impression", "view", "save", "unsave", "dismiss", "apply"]


class JobInteractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: JobInteractionType
    context: str | None = Field(default=None, max_length=30)
    source: JobSource | None = None

    @field_validator("context")
    @classmethod
    def normalize_context(cls, value: str | None) -> str | None:
        normalized = (value or "").strip().lower()
        return normalized or None


class JobPreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    desired_titles: list[str] = Field(default_factory=list, max_length=10)
    locations: list[str] = Field(default_factory=list, max_length=10)
    workplace_types: list[WorkplaceType] = Field(default_factory=list, max_length=3)
    employment_types: list[EmploymentType] = Field(default_factory=list, max_length=7)

    @field_validator("desired_titles", "locations")
    @classmethod
    def normalize_text_list(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.strip()
            if item and item.casefold() not in {entry.casefold() for entry in normalized}:
                normalized.append(item[:200])
        return normalized


class JobIngestionPayload(BaseModel):
    """Normalized payload accepted from an authorized source connector."""

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(..., min_length=1, max_length=255)
    source_url: str = Field(..., min_length=8, max_length=4000)
    apply_url: str | None = Field(default=None, max_length=4000)
    title: str = Field(..., min_length=1, max_length=300)
    company_name: str = Field(..., min_length=1, max_length=300)
    company_logo_url: str | None = Field(default=None, max_length=4000)
    location: str | None = Field(default=None, max_length=300)
    workplace_type: WorkplaceType | None = None
    employment_type: EmploymentType | None = None
    experience_level: str | None = Field(default=None, max_length=30)
    experience_years_min: int | None = Field(default=None, ge=0, le=50)
    experience_years_max: int | None = Field(default=None, ge=0, le=50)
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, max_length=10)
    salary_text: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=100_000)
    skills: list[str] = Field(default_factory=list, max_length=100)
    posted_at: str | None = None
    expires_at: str | None = None
    metadata: dict = Field(default_factory=dict)

    @field_validator("external_id", "title", "company_name")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("skills")
    @classmethod
    def normalize_skills(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            skill = " ".join(value.split())[:100]
            key = skill.casefold()
            if skill and key not in seen:
                result.append(skill)
                seen.add(key)
        return result
