from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

CrawlLevel = Literal["internship", "fresher", "junior", "middle", "senior", "lead", "manager"]
CrawlerSource = Literal["linkedin", "topcv", "itviec"]


class AdminJobCrawlPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[CrawlerSource] = Field(..., min_length=1, max_length=3)
    job_type: str = Field(..., min_length=2, max_length=120)
    level: CrawlLevel | None = None
    max_results: int = Field(default=20, ge=1, le=100)

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, values: list[CrawlerSource]) -> list[CrawlerSource]:
        return list(dict.fromkeys(values))

    @field_validator("job_type")
    @classmethod
    def normalize_job_type(cls, value: str) -> str:
        return " ".join(value.split())


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
