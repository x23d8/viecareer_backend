"""Spam heuristics and short-lived cache for CV/JD analysis requests."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from psycopg2.extras import Json

from core.database import get_connection

logger = logging.getLogger("viecareer")

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

_CV_MIN_CHARS = int(os.getenv("CVJD_CV_MIN_CHARS", "250"))
_JD_MIN_CHARS = int(os.getenv("CVJD_JD_MIN_CHARS", "120"))
_CV_MAX_CHARS = int(os.getenv("CVJD_CV_MAX_CHARS", "60000"))
_JD_MAX_CHARS = int(os.getenv("CVJD_JD_MAX_CHARS", "50000"))
_CACHE_TTL_SECONDS = int(os.getenv("CVJD_DEDUP_CACHE_TTL_SECONDS", "600"))


@dataclass(frozen=True)
class CVJDInputFingerprint:
    input_hash: str
    cv_chars: int
    jd_chars: int


class CVJDSpamRejected(Exception):
    def __init__(self, code: str, message: str, meta: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.meta = meta or {}


def validate_cvjd_request(cv_text: str | None, jd_text: str | None) -> CVJDInputFingerprint:
    raw_cv = str(cv_text or "").strip()
    raw_jd = str(jd_text or "").strip()
    cv = _clean_text(raw_cv)
    jd = _clean_text(raw_jd)

    _validate_length("cv", cv, _CV_MIN_CHARS, _CV_MAX_CHARS)
    _validate_length("jd", jd, _JD_MIN_CHARS, _JD_MAX_CHARS)
    _validate_repetition("cv", raw_cv)
    _validate_repetition("jd", raw_jd)

    return CVJDInputFingerprint(
        input_hash=_hash_pair(cv, jd),
        cv_chars=len(cv),
        jd_chars=len(jd),
    )


def get_cached_cvjd_analysis(input_hash: str) -> str | None:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_cache_schema(cur)
                cur.execute(
                    """
                    select response_text
                    from cv_jd_analysis_cache
                    where input_hash = %s
                      and expires_at > now()
                    limit 1
                    """,
                    (input_hash,),
                )
                row = cur.fetchone()
            conn.commit()
        return str(row["response_text"]) if row else None
    except Exception:
        logger.debug("CV/JD cache lookup failed", exc_info=True)
        return None


def store_cvjd_analysis_cache(input_hash: str, response_text: str | None, meta: dict[str, Any] | None = None) -> None:
    if not input_hash or not response_text:
        return

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_cache_schema(cur)
                cur.execute(
                    """
                    insert into cv_jd_analysis_cache (
                      input_hash,
                      response_text,
                      expires_at,
                      meta_json
                    )
                    values (%s, %s, now() + (%s * interval '1 second'), %s)
                    on conflict (input_hash) do update
                    set
                      response_text = excluded.response_text,
                      created_at = now(),
                      expires_at = excluded.expires_at,
                      meta_json = excluded.meta_json
                    """,
                    (
                        input_hash,
                        response_text,
                        max(1, _CACHE_TTL_SECONDS),
                        Json(meta) if meta else None,
                    ),
                )
                cur.execute("delete from cv_jd_analysis_cache where expires_at <= now()")
            conn.commit()
    except Exception:
        logger.debug("CV/JD cache store failed", exc_info=True)


def _ensure_cache_schema(cur) -> None:
    cur.execute(
        """
        create table if not exists cv_jd_analysis_cache (
          input_hash char(64) primary key,
          response_text text not null,
          created_at timestamptz not null default now(),
          expires_at timestamptz not null,
          meta_json jsonb null
        )
        """
    )
    cur.execute(
        """
        create index if not exists idx_cv_jd_analysis_cache_expires
        on cv_jd_analysis_cache (expires_at)
        """
    )


def _validate_length(label: str, text: str, min_chars: int, max_chars: int) -> None:
    length = len(text)
    if length < min_chars:
        raise CVJDSpamRejected(
            f"{label.upper()}_TEXT_TOO_SHORT",
            f"{label.upper()} text is too short for reliable analysis.",
            {"chars": length, "min_chars": min_chars},
        )
    if length > max_chars:
        raise CVJDSpamRejected(
            f"{label.upper()}_TEXT_TOO_LONG",
            f"{label.upper()} text is too long for analysis.",
            {"chars": length, "max_chars": max_chars},
        )


def _validate_repetition(label: str, text: str) -> None:
    line_reason = _line_repetition_reason(text)
    if line_reason:
        raise CVJDSpamRejected(
            f"{label.upper()}_TEXT_REPETITIVE",
            f"{label.upper()} text has too much repeated content.",
            {"reason": line_reason},
        )

    shingle_reason = _shingle_repetition_reason(text)
    if shingle_reason:
        raise CVJDSpamRejected(
            f"{label.upper()}_TEXT_REPETITIVE",
            f"{label.upper()} text has too much repeated content.",
            {"reason": shingle_reason},
        )


def _line_repetition_reason(text: str) -> dict[str, Any] | None:
    lines = [
        _clean_text(line).lower()
        for line in text.splitlines()
        if len(_clean_text(line)) >= 20
    ]
    if len(lines) < 8:
        return None

    counts: dict[str, int] = {}
    for line in lines:
        counts[line] = counts.get(line, 0) + 1

    max_count = max(counts.values(), default=0)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    repeated_ratio = repeated / max(1, len(lines))
    if max_count >= 8 and repeated_ratio >= 0.35:
        return {
            "kind": "line_repetition",
            "max_repeated_line_count": max_count,
            "repeated_line_ratio": round(repeated_ratio, 3),
        }
    return None


def _shingle_repetition_reason(text: str) -> dict[str, Any] | None:
    tokens = [token.lower() for token in _WORD_RE.findall(text)]
    if len(tokens) < 80:
        return None

    width = 6
    shingles = [" ".join(tokens[i : i + width]) for i in range(0, len(tokens) - width + 1)]
    counts: dict[str, int] = {}
    for shingle in shingles:
        counts[shingle] = counts.get(shingle, 0) + 1

    max_count = max(counts.values(), default=0)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    repeated_ratio = repeated / max(1, len(shingles))
    if max_count >= 10 and repeated_ratio >= 0.35:
        return {
            "kind": "shingle_repetition",
            "max_repeated_shingle_count": max_count,
            "repeated_shingle_ratio": round(repeated_ratio, 3),
        }
    return None


def _hash_pair(cv_text: str, jd_text: str) -> str:
    payload = f"cv:{_normalize_for_hash(cv_text)}\njd:{_normalize_for_hash(jd_text)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_for_hash(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.strip()).lower()


def _clean_text(text: str | None) -> str:
    return _WHITESPACE_RE.sub(" ", str(text or "").strip())
