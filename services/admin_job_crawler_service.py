"""Admin-only two-phase crawler preview and commit workflow."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from config import JOB_CRAWLER_USER_AGENT, _job_source_authorized, _job_source_enabled
from schemas.jobs import JobIngestionPayload
from services.job_ingestion_service import SOURCES, SourceAccessBlocked, preview_source_jobs
from services.job_market_service import upsert_ingested_job

logger = logging.getLogger("viecareer")

PREVIEW_TTL = timedelta(minutes=30)


@dataclass
class PreviewEntry:
    key: str
    source: str
    payload: JobIngestionPayload


@dataclass
class PreviewSession:
    owner_id: str
    expires_at: datetime
    items: list[PreviewEntry]


_sessions: dict[str, PreviewSession] = {}
_sessions_lock = threading.Lock()


def get_crawler_sources() -> list[dict]:
    labels = {"linkedin": "LinkedIn", "topcv": "TopCV", "itviec": "ITviec"}
    items: list[dict] = []
    for source in SOURCES:
        enabled = _job_source_enabled(source)
        authorized = _job_source_authorized(source)
        items.append(
            {
                "source": source,
                "label": labels[source],
                "enabled": enabled,
                "authorized": authorized,
                "available": enabled and authorized,
                "message": _source_message(enabled, authorized),
            }
        )
    return items


def create_crawl_preview(
    *,
    admin_id: str,
    sources: list[str],
    job_type: str,
    level: str | None,
    max_results: int,
) -> dict:
    _purge_expired_sessions()
    entries: list[PreviewEntry] = []
    source_results: list[dict] = []

    for source in sources:
        remaining = max_results - len(entries)
        if remaining <= 0:
            break
        try:
            result = preview_source_jobs(
                source,
                job_type=job_type,
                level=level,
                limit=remaining,
            )
            for payload in result.pop("items"):
                key = f"{source}:{payload.external_id}"
                entries.append(PreviewEntry(key=key, source=source, payload=payload))
            source_results.append({"status": "succeeded", **result})
        except SourceAccessBlocked as exc:
            logger.warning("Admin crawler access blocked source=%s: %s", source, exc)
            source_results.append(
                {
                    "source": source,
                    "status": "blocked",
                    "listing_url": None,
                    "discovered_count": 0,
                    "scanned_count": 0,
                    "normalized_count": 0,
                    "pages_crawled": 0,
                    "has_more": False,
                    "remaining_status": "No more jobs left because the source blocked access.",
                    "error_count": 1,
                    "errors": [{"url": "", "message": str(exc)[:500]}],
                }
            )
        except Exception as exc:
            logger.warning("Admin crawler preview failed source=%s", source, exc_info=True)
            source_results.append(
                {
                    "source": source,
                    "status": "failed",
                    "listing_url": None,
                    "discovered_count": 0,
                    "scanned_count": 0,
                    "normalized_count": 0,
                    "pages_crawled": 0,
                    "has_more": False,
                    "remaining_status": "No more jobs could be checked because discovery failed.",
                    "error_count": 1,
                    "errors": [{"url": "", "message": str(exc)[:500]}],
                }
            )

    preview_id = str(uuid4())
    expires_at = datetime.now(UTC) + PREVIEW_TTL
    with _sessions_lock:
        _sessions[preview_id] = PreviewSession(
            owner_id=admin_id,
            expires_at=expires_at,
            items=entries,
        )

    has_more = any(bool(item.get("has_more")) for item in source_results)
    return {
        "preview_id": preview_id,
        "job_type": job_type,
        "level": level,
        "detected_count": sum(item["discovered_count"] for item in source_results),
        "available_count": len(entries),
        "has_more": has_more,
        "expires_at": expires_at.isoformat(),
        "source_results": source_results,
        "items": [_preview_payload(entry) for entry in entries],
        "notice": (
            "More jobs are available. Increase the preview number to load the next jobs."
            if has_more
            else "No more jobs left for the selected keyword and filters."
        ),
    }


def commit_crawl_preview(
    *,
    admin_id: str,
    preview_id: str,
    quantity: int,
    selected_keys: list[str],
) -> dict:
    _purge_expired_sessions()
    with _sessions_lock:
        session = _sessions.get(preview_id)
    if session is None:
        raise LookupError("Preview not found or expired. Please run preview again.")
    if session.owner_id != admin_id:
        raise PermissionError("This preview belongs to another administrator.")

    if selected_keys:
        requested = set(selected_keys)
        candidates = [entry for entry in session.items if entry.key in requested]
    else:
        candidates = list(session.items)
    if quantity > len(candidates):
        raise ValueError(f"Only {len(candidates)} selected preview jobs are available.")

    imported: list[dict] = []
    errors: list[dict] = []
    for entry in candidates[:quantity]:
        try:
            job_id = upsert_ingested_job(source=entry.source, payload=entry.payload)
            imported.append(
                {
                    "key": entry.key,
                    "job_id": job_id,
                    "source": entry.source,
                    "title": entry.payload.title,
                    "company_name": entry.payload.company_name,
                }
            )
        except Exception as exc:
            logger.exception("Could not commit preview job key=%s", entry.key)
            errors.append({"key": entry.key, "message": str(exc)[:500]})

    return {
        "preview_id": preview_id,
        "requested_count": quantity,
        "upserted_count": len(imported),
        "error_count": len(errors),
        "items": imported,
        "errors": errors,
        "message": "Jobs were updated in the database." if imported else "No job was updated.",
    }


def _preview_payload(entry: PreviewEntry) -> dict:
    payload = entry.payload
    return {
        "key": entry.key,
        "source": entry.source,
        "external_id": payload.external_id,
        "source_url": payload.source_url,
        "title": payload.title,
        "company_name": payload.company_name,
        "company_logo_url": payload.company_logo_url,
        "location": payload.location,
        "workplace_type": payload.workplace_type,
        "employment_type": payload.employment_type,
        "experience_level": payload.experience_level,
        "salary_text": payload.salary_text,
        "skills": payload.skills[:12],
        "posted_at": payload.posted_at,
        "description_preview": payload.description[:5000],
        "description_length": len(payload.description),
    }


def _purge_expired_sessions() -> None:
    now = datetime.now(UTC)
    with _sessions_lock:
        expired = [key for key, session in _sessions.items() if session.expires_at <= now]
        for key in expired:
            _sessions.pop(key, None)


def _source_message(enabled: bool, authorized: bool) -> str:
    if not enabled:
        return "Set JOB_SOURCE_<SOURCE>_ENABLED=true in the backend environment."
    if not authorized:
        return "Written crawling/feed permission must be confirmed in the backend environment."
    if not JOB_CRAWLER_USER_AGENT.strip():
        return "JOB_CRAWLER_USER_AGENT is missing."
    return "Ready for preview; robots.txt is checked on every request."
