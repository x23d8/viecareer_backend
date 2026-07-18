import hashlib

from schemas.admin_jobs import AdminManualJobCreateRequest
from schemas.jobs import JobIngestionPayload
from services.job_market_service import upsert_ingested_job


def create_manual_job(*, admin_id: str, request: AdminManualJobCreateRequest) -> dict:
    """Create or safely merge a manually entered first-party job posting."""

    source_url = str(request.source_url)
    external_id = f"manual-{hashlib.sha256(source_url.casefold().encode('utf-8')).hexdigest()[:40]}"
    payload = JobIngestionPayload(
        external_id=external_id,
        source_url=source_url,
        apply_url=str(request.apply_url) if request.apply_url else source_url,
        title=request.title,
        company_name=request.company_name,
        company_logo_url=str(request.company_logo_url) if request.company_logo_url else None,
        location=request.location,
        workplace_type=request.workplace_type,
        employment_type=request.employment_type,
        experience_level=request.experience_level,
        experience_years_min=request.experience_years_min,
        experience_years_max=request.experience_years_max,
        salary_min=request.salary_min,
        salary_max=request.salary_max,
        salary_currency=request.salary_currency,
        salary_text=request.salary_text,
        description=request.description,
        skills=request.skills,
        posted_at=request.posted_at.isoformat() if request.posted_at else None,
        expires_at=request.expires_at.isoformat() if request.expires_at else None,
        metadata={
            "ingestion_method": "admin_manual",
            "created_by_admin_id": admin_id,
        },
    )
    job_id = upsert_ingested_job(source="viecareer", payload=payload)
    return {
        "job_id": job_id,
        "external_id": external_id,
        "title": payload.title,
        "company_name": payload.company_name,
        "source": "viecareer",
        "source_url": payload.source_url,
        "message": "Manual job saved successfully.",
    }
