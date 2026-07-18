#!/usr/bin/env python3
"""Standalone ITviec job crawler for public job listings.

Examples:
    python crawl.py --keyword python --cookies-file itviec.com_18-07-2026.json --max-jobs 20
    python crawl.py --keyword "data engineer" --output jobs.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://itviec.com"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crawl public ITviec jobs by keyword")
    parser.add_argument("--keyword", default="python", help="Keyword to search for (default: python)")
    parser.add_argument("--cookies", default=None, help="Raw cookie string, for example: sessionid=abc; consent=yes")
    parser.add_argument("--cookies-file", default=None, help="Path to a browser-exported cookies JSON file")
    parser.add_argument("--output", default=None, help="Path to save the JSON results")
    parser.add_argument("--max-jobs", type=int, default=20, help="Maximum number of jobs to crawl")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between requests in seconds")
    return parser


def slugify_keyword(keyword: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
    return normalized or "it"


def build_listing_url(keyword: str) -> str:
    slug = slugify_keyword(keyword)
    if slug in {"it", "it-jobs"}:
        return f"{BASE_URL}/it-jobs"
    return f"{BASE_URL}/it-jobs/{slug}"


def parse_cookie_string(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            cookies[key] = value
    return cookies


def load_cookies_from_file(path: Path) -> dict[str, str]:
    content = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return parse_cookie_string(content)

    if isinstance(payload, dict):
        cookie_items = payload.get("cookies") or []
        if isinstance(cookie_items, list):
            cookies: dict[str, str] = {}
            for item in cookie_items:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                value = item.get("value")
                if name and value:
                    cookies[str(name)] = str(value)
            return cookies

    raise ValueError(f"Unsupported cookies file format: {path}")


def load_cookies(args: argparse.Namespace) -> dict[str, str]:
    if args.cookies:
        return parse_cookie_string(args.cookies)

    if args.cookies_file:
        return load_cookies_from_file(Path(args.cookies_file))

    for env_name in ["JOB_SOURCE_ITVIEC_COOKIES", "ITVIEC_COOKIES", "ITVIEC_COOKIES_FILE"]:
        raw_value = os.getenv(env_name, "").strip()
        if not raw_value:
            continue
        if env_name.endswith("FILE"):
            return load_cookies_from_file(Path(raw_value))
        return parse_cookie_string(raw_value)

    return {}


def extract_listing_urls(html: str, listing_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if not href:
            continue
        candidate = urljoin(listing_url, href)
        parsed = urlparse(candidate)
        if parsed.hostname not in {"itviec.com", "www.itviec.com"}:
            continue
        if re.match(r"^/it-jobs/[^/?]+-\d{4,}/?$", parsed.path):
            urls.append(candidate)
    return list(dict.fromkeys(urls))


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def clean_html(value: Any) -> str:
    if not value:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text("\n", strip=True)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def infer_experience_level(title: str, description: str) -> str | None:
    text = f"{title} {description}".lower()
    if re.search(r"\b(intern|internship|trainee|trainee engineer|fresher)\b", text):
        return "internship"
    if re.search(r"\b(senior|lead|principal|staff)\b", text):
        return "senior"
    if re.search(r"\b(mid|middle|2\s*\+\s*years|3\s*\+\s*years|4\s*\+\s*years)\b", text):
        return "middle"
    if re.search(r"\b(junior|entry|1\s*year|fresh)\b", text):
        return "junior"
    return None


def extract_job_payload(url: str, html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    payload: dict[str, Any] = {"source_url": url, "metadata": {}}

    json_ld_nodes = soup.find_all("script", type="application/ld+json")
    structured: dict[str, Any] | None = None
    for node in json_ld_nodes:
        if not node.string:
            continue
        try:
            parsed = json.loads(node.string)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("@type") == "JobPosting":
            structured = parsed
            break
        if isinstance(parsed, list):
            for entry in parsed:
                if isinstance(entry, dict) and entry.get("@type") == "JobPosting":
                    structured = entry
                    break
            if structured:
                break

    if structured is not None:
        organization = structured.get("hiringOrganization") or {}
        if not isinstance(organization, dict):
            organization = {}

        location = structured.get("jobLocation") or {}
        if isinstance(location, dict):
            address = location.get("address") or {}
            if isinstance(address, dict):
                locality = normalize_text(address.get("addressLocality"))
                country = normalize_text(address.get("addressCountry"))
                payload["location"] = ", ".join(filter(None, [locality, country])) or None
            else:
                payload["location"] = None
        else:
            payload["location"] = None

        payload["title"] = normalize_text(structured.get("title") or "")
        payload["company_name"] = normalize_text(organization.get("name") or "")
        payload["company_logo_url"] = normalize_text(organization.get("logo") or "")
        payload["employment_type"] = normalize_text(structured.get("employmentType") or "").lower().replace("_", "-") or None
        payload["workplace_type"] = None
        if structured.get("jobLocationType"):
            payload["workplace_type"] = normalize_text(structured.get("jobLocationType")).lower()
        payload["description"] = clean_html(structured.get("description") or "")
        payload["posted_at"] = normalize_text(structured.get("datePosted") or "")
        payload["skills"] = []
        skills = structured.get("skills") or []
        if isinstance(skills, str):
            payload["skills"] = [item.strip() for item in skills.split(",") if item.strip()]
        elif isinstance(skills, list):
            payload["skills"] = [normalize_text(item) for item in skills if normalize_text(item)]

        salary = structured.get("baseSalary") or {}
        if isinstance(salary, dict):
            value = salary.get("value") or {}
            if isinstance(value, dict):
                min_value = value.get("minValue")
                max_value = value.get("maxValue")
                currency = normalize_text(salary.get("currency") or "")
                unit_text = normalize_text(value.get("unitText") or "")
                if min_value is not None and max_value is not None:
                    payload["salary_text"] = f"{min_value:,} - {max_value:,} {currency} {unit_text}".strip()
                else:
                    payload["salary_text"] = None
            else:
                payload["salary_text"] = None
        else:
            payload["salary_text"] = None

        payload["experience_level"] = infer_experience_level(payload.get("title", ""), payload.get("description", ""))
        payload["metadata"]["structured_data_found"] = True
    else:
        title_tag = soup.find(["h1", "h2"])
        payload["title"] = normalize_text(title_tag.get_text(" ", strip=True) if title_tag else "")
        company_tag = soup.select_one("[class*='company'], [class*='employer'], [id*='company']")
        payload["company_name"] = normalize_text(company_tag.get_text(" ", strip=True) if company_tag else "")
        payload["description"] = clean_html(soup.get_text("\n", strip=True))
        payload["experience_level"] = infer_experience_level(payload.get("title", ""), payload.get("description", ""))
        payload["metadata"]["structured_data_found"] = False

    payload["external_id"] = re.search(r"-(\d{4,})/?$", urlparse(url).path)
    if payload["external_id"]:
        payload["external_id"] = payload["external_id"].group(1)
    else:
        payload["external_id"] = urlparse(url).path.split("/")[-1]

    payload["metadata"]["listing_url"] = url
    return payload


def crawl_itviec(keyword: str, cookies: dict[str, str], max_jobs: int, delay: float) -> list[dict[str, Any]]:
    listing_url = build_listing_url(keyword)
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "vi,en;q=0.8",
    }

    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(20.0), trust_env=False, headers=headers, cookies=cookies) as client:
        response = client.get(listing_url)
        response.raise_for_status()
        listing_html = response.text
        detail_urls = extract_listing_urls(listing_html, listing_url)[:max_jobs]

        results: list[dict[str, Any]] = []
        for detail_url in detail_urls:
            time.sleep(delay)
            detail_response = client.get(detail_url)
            detail_response.raise_for_status()
            payload = extract_job_payload(detail_url, detail_response.text)
            payload["keyword"] = keyword
            results.append(payload)

        return results


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.keyword.strip():
        parser.error("--keyword must not be empty")

    cookies = load_cookies(args)
    if not cookies:
        print("No cookies were provided. Supply --cookies, --cookies-file, or the JOB_SOURCE_ITVIEC_COOKIES env var.", file=sys.stderr)

    try:
        jobs = crawl_itviec(args.keyword, cookies, args.max_jobs, args.delay)
    except Exception as exc:
        print(f"Crawl failed: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output or f"itviec_jobs_{slugify_keyword(args.keyword)}.json")
    output_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Saved {len(jobs)} jobs to {output_path}")
    for job in jobs[:5]:
        print(f"- {job.get('title')} | {job.get('company_name')} | {job.get('location')} | {job.get('experience_level')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
