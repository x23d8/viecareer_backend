"""Conservative HTML/JSON-LD ingestion for sources with written permission.

The connectors intentionally do not authenticate, rotate proxies, solve challenges, or
bypass access controls. Every third-party source is disabled until both ENABLED and
AUTHORIZED are explicitly set by the deployment operator.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from config import (
    JOB_CRAWLER_USER_AGENT,
    JOB_SOURCE_MAX_DETAILS,
    JOB_SOURCE_REQUEST_DELAY_SECONDS,
    _job_source_authorized,
    _job_source_enabled,
    _job_source_listing_urls,
)
from core.database import get_connection
from schemas.jobs import JobIngestionPayload
from services.job_market_service import upsert_ingested_job

logger = logging.getLogger("viecareer")


class SourceAccessBlocked(PermissionError):
    """The source explicitly rejected or throttled the configured crawler."""


@dataclass(frozen=True)
class SourceDefinition:
    name: str
    allowed_hosts: tuple[str, ...]
    detail_pattern: re.Pattern[str]


SOURCES = {
    "linkedin": SourceDefinition(
        name="linkedin",
        allowed_hosts=("www.linkedin.com", "linkedin.com"),
        detail_pattern=re.compile(r"^/jobs/view/(?:[^/?]+-)?(?P<id>\d+)/?$"),
    ),
    "topcv": SourceDefinition(
        name="topcv",
        allowed_hosts=("www.topcv.vn", "topcv.vn"),
        detail_pattern=re.compile(
            r"^/(?:viec-lam/[^/?]+/(?P<plain_id>\d+)\.html|brand/[^/]+/tuyen-dung/[^/?]+-j(?P<brand_id>\d+)\.html)$"
        ),
    ),
    "itviec": SourceDefinition(
        name="itviec",
        allowed_hosts=("itviec.com", "www.itviec.com"),
        # ITviec uses /it-jobs for English and /viec-lam-it for Vietnamese.
        # Detail URLs end with a four-or-more digit posting ID; requiring that
        # shape avoids category pages such as /it-jobs/microsoft-dynamics-365.
        detail_pattern=re.compile(r"^/(?:it-jobs|viec-lam-it)/[^/?]+-(?P<id>\d{4,})/?$"),
    ),
}

JOB_TYPE_ALIASES = {
    "aie": "ai engineer",
    "ai": "ai engineer",
    "ba": "business analysis",
    "business analyst": "business analysis",
    "kinh te": "kinh te",
    "it": "information technology",
}


def sync_authorized_sources(requested_source: str | None = None) -> list[dict[str, Any]]:
    if requested_source and requested_source not in SOURCES:
        raise ValueError(f"Unknown job source: {requested_source}")

    names = [requested_source] if requested_source else list(SOURCES)
    return [sync_source(name) for name in names]


def sync_source(source: str) -> dict[str, Any]:
    definition = SOURCES[source]
    run_id = _start_run(source)
    if not _job_source_enabled(source):
        return _finish_run(run_id, source, "skipped", message="Source is disabled.")
    if not _job_source_authorized(source):
        return _finish_run(
            run_id,
            source,
            "skipped",
            message="Written crawling/feed permission has not been confirmed.",
        )

    listing_urls = _job_source_listing_urls(source)
    if not listing_urls:
        return _finish_run(run_id, source, "skipped", message="No listing URLs configured.")

    discovered: list[str] = []
    upserted = 0
    errors = 0
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
            trust_env=False,
            headers={
                "User-Agent": JOB_CRAWLER_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "vi,en;q=0.8",
            },
        ) as client:
            robots_cache: dict[str, RobotFileParser] = {}
            for listing_url in listing_urls:
                _validate_source_url(definition, listing_url)
                _require_robots_permission(client, listing_url, robots_cache)
                listing_html = _fetch(client, listing_url)
                discovered.extend(_extract_detail_urls(definition, listing_url, listing_html))
                time.sleep(JOB_SOURCE_REQUEST_DELAY_SECONDS)

            detail_urls = list(dict.fromkeys(discovered))[:JOB_SOURCE_MAX_DETAILS]
            for detail_url in detail_urls:
                try:
                    _require_robots_permission(client, detail_url, robots_cache)
                    payload = _extract_job_payload(definition, detail_url, _fetch(client, detail_url))
                    upsert_ingested_job(source=source, payload=payload)
                    upserted += 1
                except Exception:
                    errors += 1
                    logger.warning("Could not ingest %s job url=%s", source, detail_url, exc_info=True)
                time.sleep(JOB_SOURCE_REQUEST_DELAY_SECONDS)

        status = "succeeded" if upserted or not discovered else "failed"
        message = None if status == "succeeded" else "No discovered job could be normalized."
        return _finish_run(
            run_id,
            source,
            status,
            discovered_count=len(set(discovered)),
            upserted_count=upserted,
            error_count=errors,
            message=message,
        )
    except Exception as exc:
        logger.exception("Job ingestion failed source=%s", source)
        return _finish_run(
            run_id,
            source,
            "failed",
            discovered_count=len(set(discovered)),
            upserted_count=upserted,
            error_count=errors + 1,
            message=str(exc)[:1000],
        )


def preview_source_jobs(
    source: str,
    *,
    job_type: str,
    level: str | None = None,
    location: str | None = None,
    company: str | None = None,
    experience_years_min: int | None = None,
    experience_years_max: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Discover and normalize jobs without writing any database records."""

    if source not in SOURCES:
        raise ValueError(f"Unknown job source: {source}")
    if not _job_source_enabled(source):
        raise PermissionError("Source is disabled by backend configuration.")
    if not _job_source_authorized(source):
        raise PermissionError("Written crawling/feed permission has not been confirmed.")

    definition = SOURCES[source]
    listing_url = build_keyword_listing_url(source, job_type)
    _validate_source_url(definition, listing_url)
    normalized: list[JobIngestionPayload] = []
    errors: list[dict[str, str]] = []
    discovered: list[str] = []
    discovered_set: set[str] = set()
    attempted: set[str] = set()
    pages_crawled = 0
    filtered_out_count = 0
    has_more = False
    detail_limit = min(max(1, limit), JOB_SOURCE_MAX_DETAILS)

    with httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(20.0),
        trust_env=False,
        headers={
            "User-Agent": JOB_CRAWLER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "vi,en;q=0.8",
        },
    ) as client:
        robots_cache: dict[str, RobotFileParser] = {}
        listing_queue = [listing_url]
        queued_pages = {listing_url}
        visited_pages: set[str] = set()

        while listing_queue:
            current_listing_url = listing_queue.pop(0)
            queued_pages.discard(current_listing_url)
            if current_listing_url in visited_pages:
                continue
            visited_pages.add(current_listing_url)

            _require_robots_permission(client, current_listing_url, robots_cache)
            listing_html = _fetch(client, current_listing_url)
            pages_crawled += 1

            page_detail_urls = _extract_detail_urls(
                definition,
                current_listing_url,
                listing_html,
            )
            new_detail_urls: list[str] = []
            for detail_url in page_detail_urls:
                if detail_url not in discovered_set:
                    discovered.append(detail_url)
                    discovered_set.add(detail_url)
                    new_detail_urls.append(detail_url)

            for page_url in _extract_pagination_urls(
                definition,
                current_listing_url,
                listing_html,
            ):
                if page_url not in visited_pages and page_url not in queued_pages:
                    listing_queue.append(page_url)
                    queued_pages.add(page_url)

            for index, detail_url in enumerate(new_detail_urls):
                if len(attempted) >= JOB_SOURCE_MAX_DETAILS:
                    has_more = True
                    break
                attempted.add(detail_url)
                try:
                    _require_robots_permission(client, detail_url, robots_cache)
                    payload = _extract_job_payload(definition, detail_url, _fetch(client, detail_url))
                    if level and payload.experience_level != level:
                        filtered_out_count += 1
                        continue
                    if not _matches_preview_filters(
                        payload,
                        location=location,
                        company=company,
                        experience_years_min=experience_years_min,
                        experience_years_max=experience_years_max,
                    ):
                        filtered_out_count += 1
                        continue
                    normalized.append(payload)
                except Exception as exc:
                    logger.warning("Could not preview %s job url=%s", source, detail_url, exc_info=True)
                    errors.append({"url": detail_url, "message": str(exc)[:500]})
                finally:
                    time.sleep(JOB_SOURCE_REQUEST_DELAY_SECONDS)

                if len(normalized) >= detail_limit:
                    has_more = index + 1 < len(new_detail_urls) or bool(listing_queue)
                    break

            if has_more or len(normalized) >= detail_limit:
                break

    return {
        "source": source,
        "listing_url": listing_url,
        "discovered_count": len(discovered),
        "scanned_count": len(attempted),
        "normalized_count": len(normalized),
        "filtered_out_count": filtered_out_count,
        "pages_crawled": pages_crawled,
        "has_more": has_more,
        "remaining_status": (
            "More jobs are available for this keyword."
            if has_more
            else "No more jobs left for this keyword."
        ),
        "error_count": len(errors),
        "errors": errors[:10],
        "items": normalized,
    }


def _matches_preview_filters(
    payload: JobIngestionPayload,
    *,
    location: str | None,
    company: str | None,
    experience_years_min: int | None,
    experience_years_max: int | None,
) -> bool:
    if location and _ascii_slug(location) not in _ascii_slug(payload.location or ""):
        return False
    if company and _ascii_slug(company) not in _ascii_slug(payload.company_name):
        return False

    if experience_years_min is None and experience_years_max is None:
        return True
    if payload.experience_years_min is None and payload.experience_years_max is None:
        return False

    job_min = payload.experience_years_min
    job_max = payload.experience_years_max
    if job_min is None:
        job_min = job_max
    if job_max is None:
        job_max = job_min
    if job_min is None or job_max is None:
        return False
    if experience_years_min is not None and job_max < experience_years_min:
        return False
    if experience_years_max is not None and job_min > experience_years_max:
        return False
    return True


def build_keyword_listing_url(source: str, job_type: str) -> str:
    """Build a public keyword/category URL while keeping hosts strictly allow-listed."""

    normalized = _ascii_slug(job_type)
    keyword = JOB_TYPE_ALIASES.get(normalized, normalized).strip()
    if not keyword:
        raise ValueError("Job type is required.")
    slug = re.sub(r"[^a-z0-9]+", "-", keyword).strip("-")
    if source == "topcv":
        return f"https://www.topcv.vn/tim-viec-lam-{slug}"
    if source == "itviec":
        return "https://itviec.com/it-jobs" if normalized == "it" else f"https://itviec.com/it-jobs/{slug}"
    if source == "linkedin":
        return f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(keyword)}&location=Vietnam"
    raise ValueError(f"Unknown job source: {source}")


def _fetch(client: httpx.Client, url: str) -> str:
    response = client.get(url)
    if response.status_code in {401, 403}:
        host = urlparse(url).hostname or "The source"
        raise SourceAccessBlocked(
            f"{host} returned HTTP {response.status_code} for the configured crawler. "
            "Do not bypass this restriction; request crawler allow-listing or an authorized feed/API from the source."
        )
    if response.status_code == 429:
        host = urlparse(url).hostname or "The source"
        raise SourceAccessBlocked(
            f"{host} rate-limited the crawler (HTTP 429). Reduce the schedule or use the source's authorized feed/API."
        )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        raise ValueError(f"Expected HTML response from {url}")
    return response.text


def _require_robots_permission(
    client: httpx.Client,
    url: str,
    cache: dict[str, RobotFileParser],
) -> None:
    parsed = urlparse(url)
    robots_url = urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    parser = cache.get(robots_url)
    if parser is None:
        response = client.get(robots_url)
        if response.status_code in {401, 403, 429}:
            raise SourceAccessBlocked(
                f"Could not verify crawler permission because {robots_url} returned HTTP {response.status_code}. "
                "Request allow-listing or an authorized feed/API from the source."
            )
        if response.status_code >= 400:
            raise PermissionError(f"Could not verify robots policy at {robots_url}")
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        cache[robots_url] = parser
    if not parser.can_fetch(JOB_CRAWLER_USER_AGENT, url):
        raise PermissionError(f"robots.txt does not allow the configured crawler to access {url}")


def _extract_detail_urls(definition: SourceDefinition, listing_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    # Current ITviec result cards put the canonical URL on the job-title h3
    # instead of an anchor. Prefer those cards so featured/sidebar links do not
    # get mistaken for keyword matches. Other sources retain the anchor fallback.
    job_cards = soup.select("[data-search--job-selection-target='jobTitle'][data-url]")
    candidates = (
        [str(card.get("data-url")) for card in job_cards]
        if job_cards
        else [str(anchor.get("href")) for anchor in soup.select("a[href]")]
    )
    for raw_candidate in candidates:
        candidate = _clean_url(urljoin(listing_url, raw_candidate))
        parsed = urlparse(candidate)
        if parsed.hostname not in definition.allowed_hosts:
            continue
        if definition.detail_pattern.match(parsed.path):
            urls.append(candidate)
    return list(dict.fromkeys(urls))


def _extract_pagination_urls(
    definition: SourceDefinition,
    listing_url: str,
    html: str,
) -> list[str]:
    """Return same-listing numbered pages while dropping tracking parameters."""

    soup = BeautifulSoup(html, "html.parser")
    listing = urlparse(listing_url)
    pages: dict[int, str] = {}
    for anchor in soup.select("a[href]"):
        candidate = urlparse(urljoin(listing_url, str(anchor.get("href"))))
        if candidate.hostname not in definition.allowed_hosts or candidate.path != listing.path:
            continue
        query = parse_qs(candidate.query)
        raw_page = (query.get("page") or [""])[0]
        if not raw_page.isdigit() or int(raw_page) <= 1:
            continue
        page = int(raw_page)
        normalized_query = urlencode({"page": page, "source": "search_job"})
        pages[page] = urlunparse(
            (candidate.scheme, candidate.netloc, candidate.path, "", normalized_query, "")
        )
    return [pages[page] for page in sorted(pages)]


def _extract_job_payload(definition: SourceDefinition, url: str, html: str) -> JobIngestionPayload:
    soup = BeautifulSoup(html, "html.parser")
    posting = _find_job_posting(soup)
    if not posting:
        raise ValueError("The detail page does not expose JobPosting structured data.")

    organization = posting.get("hiringOrganization") or {}
    if not isinstance(organization, dict):
        organization = {}
    location = _job_location(posting.get("jobLocation"))
    description_html = str(posting.get("description") or "")
    description = BeautifulSoup(description_html, "html.parser").get_text("\n", strip=True)
    title = _string(posting.get("title"))
    company = _string(organization.get("name"))
    if not title or not company:
        raise ValueError("JobPosting is missing title or hiringOrganization.name.")

    parsed_url = urlparse(url)
    matched = definition.detail_pattern.match(parsed_url.path)
    if not matched:
        raise ValueError("Unexpected job detail URL.")
    external_id = next((value for value in matched.groupdict().values() if value), None)
    if not external_id:
        raise ValueError("Could not derive the source job ID.")

    salary = _salary(posting.get("baseSalary"))
    skills = _skills(posting.get("skills") or posting.get("qualifications"))
    workplace_type = _workplace_type(posting, description)
    requirements = _string(posting.get("experienceRequirements"))
    experience_level = _infer_experience_level(
        title,
        description,
        requirements,
    )
    experience_years = _infer_experience_years(title, description, requirements)
    return JobIngestionPayload(
        external_id=external_id,
        source_url=_clean_url(url),
        apply_url=_clean_url(url),
        title=title,
        company_name=company,
        company_logo_url=_image_url(organization.get("logo")),
        location=location,
        workplace_type=workplace_type,
        employment_type=_employment_type(posting.get("employmentType")),
        experience_level=experience_level,
        experience_years_min=experience_years[0],
        experience_years_max=experience_years[1],
        salary_min=salary[0],
        salary_max=salary[1],
        salary_currency=salary[2],
        salary_text=salary[3],
        description=description,
        skills=skills,
        posted_at=_string(posting.get("datePosted")) or None,
        expires_at=_string(posting.get("validThrough")) or None,
        metadata={"structured_data": "schema.org/JobPosting"},
    )


def _find_job_posting(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.get_text(strip=True))
        except (TypeError, json.JSONDecodeError):
            continue
        for candidate in _json_objects(value):
            item_type = candidate.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if "JobPosting" in types:
                return candidate
    return None


def _json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.get("@graph", []):
            yield from _json_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _json_objects(nested)


def _job_location(value: Any) -> str | None:
    if isinstance(value, list):
        parts = [_job_location(item) for item in value]
        return "; ".join(dict.fromkeys(part for part in parts if part)) or None
    if not isinstance(value, dict):
        return _string(value) or None
    address = value.get("address") or value
    if not isinstance(address, dict):
        return _string(address) or None
    parts = [
        _string(address.get("streetAddress")),
        _string(address.get("addressLocality")),
        _string(address.get("addressRegion")),
        _string(address.get("addressCountry")),
    ]
    return ", ".join(dict.fromkeys(part for part in parts if part)) or None


def _salary(value: Any) -> tuple[int | None, int | None, str | None, str | None]:
    if not isinstance(value, dict):
        return None, None, None, None
    currency = _string(value.get("currency")) or None
    raw = value.get("value") or {}
    if not isinstance(raw, dict):
        raw = {"value": raw}
    minimum = _integer(raw.get("minValue") or raw.get("value"))
    maximum = _integer(raw.get("maxValue") or raw.get("value"))
    unit = _string(raw.get("unitText"))
    if minimum is None and maximum is None:
        return None, None, currency, None
    if minimum is None:
        bounds = f"{maximum:,}"
    elif maximum is None or minimum == maximum:
        bounds = f"{minimum:,}"
    else:
        bounds = f"{minimum:,} - {maximum:,}"
    return minimum, maximum, currency, " ".join(part for part in [bounds, currency, unit] if part)


def _skills(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        raw = re.split(r"[,;|\n]", _string(value))
    return [item.strip()[:100] for item in raw if item.strip()][:100]


def _workplace_type(posting: dict[str, Any], description: str) -> str | None:
    location_type = _string(posting.get("jobLocationType")).casefold()
    text = f"{location_type} {description[:2000]}".casefold()
    if "telecommute" in text or re.search(r"\bremote\b|làm việc từ xa", text):
        return "remote"
    if re.search(r"\bhybrid\b|kết hợp", text):
        return "hybrid"
    if re.search(r"\bon[- ]?site\b|at office|tại văn phòng", text):
        return "on-site"
    return None


def _employment_type(value: Any) -> str | None:
    raw = " ".join(value if isinstance(value, list) else [str(value or "")]).casefold()
    mappings = (
        ("intern", "internship"),
        ("part", "part-time"),
        ("contract", "contract"),
        ("temporary", "temporary"),
        ("freelance", "freelance"),
        ("full", "full-time"),
    )
    return next((normalized for marker, normalized in mappings if marker in raw), None)


def _infer_experience_level(title: str, description: str, requirements: str = "") -> str | None:
    text = _ascii_slug(f"{title} {requirements} {description[:6000]}")
    markers = (
        ("manager", r"\bmanager\b|\bhead of\b|\bdirector\b|truong phong|giam doc"),
        ("lead", r"\bteam lead(?:er)?\b|\btech lead\b|\bprincipal\b|\blead\b"),
        ("senior", r"\bsenior\b|\bsr\.?\b"),
        ("internship", r"\bintern(?:ship)?\b|thuc tap"),
        ("fresher", r"\bfresher\b|entry[ -]level|khong yeu cau kinh nghiem|no experience"),
        ("junior", r"\bjunior\b|\bjr\.?\b"),
        ("middle", r"\bmid(?:dle)?[ -]level\b|\bmiddle\b"),
    )
    for level, pattern in markers:
        if re.search(pattern, text):
            return level

    years = [int(value) for value in re.findall(r"\b(\d{1,2})\+?\s*(?:years?|nam)\b", text)]
    if years:
        minimum = min(years)
        if minimum >= 5:
            return "senior"
        if minimum >= 2:
            return "middle"
        return "junior"
    return None


def _infer_experience_years(
    title: str,
    description: str,
    requirements: str = "",
) -> tuple[int | None, int | None]:
    text = _ascii_slug(f"{title} {requirements} {description[:8000]}")
    if re.search(r"khong yeu cau kinh nghiem|no experience required|no prior experience", text):
        return 0, 0
    if re.search(r"duoi\s+1\s+nam|less than\s+1\s+year", text):
        return 0, 1

    range_match = re.search(
        r"\b(\d{1,2})\s*(?:-|–|—|to|den)\s*(\d{1,2})\s*(?:years?|nam)\b",
        text,
    )
    if range_match:
        lower, upper = int(range_match.group(1)), int(range_match.group(2))
        if 0 <= lower <= upper <= 50:
            return lower, upper

    minimum_match = re.search(
        r"(?:at least|minimum|toi thieu|tren|tu)?\s*\b(\d{1,2})\+?\s*(?:years?|nam)(?:\s+kinh nghiem)?\b",
        text,
    )
    if minimum_match:
        years = int(minimum_match.group(1))
        if 0 <= years <= 50:
            return years, None
    return None, None


def _validate_source_url(definition: SourceDefinition, url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in definition.allowed_hosts:
        raise ValueError(f"URL is outside the allow-list for {definition.name}: {url}")
    if parsed.username or parsed.password:
        raise ValueError("Source URLs must not contain credentials.")


def _clean_url(value: str) -> str:
    parsed = urlparse(value)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _ascii_slug(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold().replace("đ", "d"))
    return " ".join(
        "".join(character for character in decomposed if not unicodedata.combining(character)).split()
    )


def _image_url(value: Any) -> str | None:
    if isinstance(value, dict):
        return _string(value.get("url")) or None
    return _string(value) or None


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return _string(value.get("name") or value.get("value") or value.get("url"))
    return " ".join(str(value).split())


def _integer(value: Any) -> int | None:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def _start_run(source: str) -> str:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into job_ingestion_runs (source, status) values (%s, 'running') returning id",
                (source,),
            )
            run_id = str(cur.fetchone()["id"])
        conn.commit()
    return run_id


def _finish_run(
    run_id: str,
    source: str,
    status: str,
    *,
    discovered_count: int = 0,
    upserted_count: int = 0,
    error_count: int = 0,
    message: str | None = None,
) -> dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update job_ingestion_runs set
                  status = %s,
                  discovered_count = %s,
                  upserted_count = %s,
                  error_count = %s,
                  message = %s,
                  finished_at = now()
                where id = %s
                """,
                (status, discovered_count, upserted_count, error_count, message, run_id),
            )
        conn.commit()
    return {
        "id": run_id,
        "source": source,
        "status": status,
        "discovered_count": discovered_count,
        "upserted_count": upserted_count,
        "error_count": error_count,
        "message": message,
    }
