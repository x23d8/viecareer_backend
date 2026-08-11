"""Persistence and redaction for authenticated client error reports."""

import hashlib
import json
import logging
import re
from typing import Any

from fastapi import HTTPException, status

from core.database import get_connection

logger = logging.getLogger("viecareer")

_JWT_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])")
_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[^\s,;]+")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(password|passcode|secret|access[_-]?token|refresh[_-]?token|authorization)\s*[:=]\s*([^\s,;&]+)"
)
_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "password",
    "passcode",
    "secret",
    "token",
    "access_token",
    "refresh_token",
}


def submit_error_report(
    *,
    external_auth_id: str,
    email: str | None,
    category: str,
    severity: str,
    platform: str,
    location: str,
    route: str | None,
    error_type: str,
    message: str,
    technical_detail: str | None,
    stack_trace: str | None,
    http_status: int | None,
    request_id: str | None,
    app_version: str | None,
    occurred_at_epoch_ms: int | None,
    context: dict[str, Any],
) -> dict[str, Any]:
    safe_message = _redact_text(message, 2_000) or "Unknown client error"
    safe_detail = _redact_text(technical_detail, 12_000)
    safe_stack = _redact_text(stack_trace, 16_000)
    safe_context = _redact_context(context)
    fingerprint = _fingerprint(
        platform=platform,
        category=category,
        location=location,
        error_type=error_type,
        http_status=http_status,
        message=safe_message,
    )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_error_report_schema(cur)
                cur.execute(
                    """
                    select id
                    from users
                    where external_auth_id = %s
                    limit 1
                    """,
                    (external_auth_id,),
                )
                user = cur.fetchone()
                cur.execute(
                    """
                    insert into app_error_reports (
                      user_id,
                      external_auth_id,
                      email,
                      category,
                      severity,
                      platform,
                      location,
                      route,
                      error_type,
                      message,
                      technical_detail,
                      stack_trace,
                      http_status,
                      request_id,
                      app_version,
                      occurred_at_epoch_ms,
                      context_json,
                      fingerprint
                    )
                    values (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s::jsonb, %s
                    )
                    on conflict (external_auth_id, fingerprint) do update set
                      user_id = coalesce(excluded.user_id, app_error_reports.user_id),
                      email = coalesce(excluded.email, app_error_reports.email),
                      severity = excluded.severity,
                      route = coalesce(excluded.route, app_error_reports.route),
                      message = excluded.message,
                      technical_detail = coalesce(excluded.technical_detail, app_error_reports.technical_detail),
                      stack_trace = coalesce(excluded.stack_trace, app_error_reports.stack_trace),
                      http_status = coalesce(excluded.http_status, app_error_reports.http_status),
                      request_id = coalesce(excluded.request_id, app_error_reports.request_id),
                      app_version = coalesce(excluded.app_version, app_error_reports.app_version),
                      occurred_at_epoch_ms = coalesce(excluded.occurred_at_epoch_ms, app_error_reports.occurred_at_epoch_ms),
                      context_json = app_error_reports.context_json || excluded.context_json,
                      occurrence_count = app_error_reports.occurrence_count + 1,
                      status = case when app_error_reports.status = 'resolved' then 'open' else app_error_reports.status end,
                      last_seen_at = now(),
                      updated_at = now()
                    returning *
                    """,
                    (
                        str(user["id"]) if user else None,
                        external_auth_id,
                        email,
                        category,
                        severity,
                        platform,
                        location,
                        route,
                        error_type,
                        safe_message,
                        safe_detail,
                        safe_stack,
                        http_status,
                        request_id,
                        app_version,
                        occurred_at_epoch_ms,
                        json.dumps(safe_context),
                        fingerprint,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return _error_payload(row)
    except Exception as exc:
        logger.exception(
            "Could not save client error user=%s category=%s location=%s",
            external_auth_id,
            category,
            location,
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not save the error report. Please try again.",
        ) from exc


def list_admin_error_reports(
    *,
    limit: int,
    offset: int,
    search: str | None,
    category: str | None,
    platform: str | None,
    severity: str | None,
    report_status: str | None,
) -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_error_report_schema(cur)
                clauses = ["true"]
                params: list[Any] = []
                for column, value in (
                    ("e.category", category),
                    ("e.platform", platform),
                    ("e.severity", severity),
                    ("e.status", report_status),
                ):
                    if value:
                        clauses.append(f"{column} = %s")
                        params.append(value)
                if search:
                    term = f"%{search.strip()}%"
                    clauses.append(
                        """
                        (
                          coalesce(e.email, '') ilike %s
                          or coalesce(u.full_name, '') ilike %s
                          or e.external_auth_id ilike %s
                          or e.location ilike %s
                          or e.error_type ilike %s
                          or e.message ilike %s
                          or coalesce(e.request_id, '') ilike %s
                        )
                        """
                    )
                    params.extend([term] * 7)
                where_sql = " and ".join(clauses)
                cur.execute(
                    f"""
                    select count(*)::int as total
                    from app_error_reports e
                    left join users u on u.id = e.user_id
                    where {where_sql}
                    """,
                    params,
                )
                total = int((cur.fetchone() or {}).get("total") or 0)
                cur.execute(
                    f"""
                    select e.*, u.full_name
                    from app_error_reports e
                    left join users u on u.id = e.user_id
                    where {where_sql}
                    order by e.last_seen_at desc, e.created_at desc
                    limit %s offset %s
                    """,
                    [*params, limit, offset],
                )
                items = [_error_payload(row) for row in cur.fetchall()]
        return {"items": items, "pagination": {"limit": limit, "offset": offset, "total": total}}
    except Exception as exc:
        logger.exception("Could not list client error reports")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load error reports.",
        ) from exc


def update_error_report_status(*, report_id: str, report_status: str) -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_error_report_schema(cur)
                cur.execute(
                    """
                    update app_error_reports
                    set status = %s, updated_at = now()
                    where id = %s
                    returning *
                    """,
                    (report_status, report_id),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Error report was not found.")
            conn.commit()
        return _error_payload(row)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not update client error report=%s", report_id)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not update error report.") from exc


def _redact_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _JWT_PATTERN.sub("[REDACTED_JWT]", redacted)
    redacted = _SECRET_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    return redacted[:limit]


def _redact_context(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(key)[:80]: _redact_context(item, depth + 1)
            for key, item in list(value.items())[:40]
            if str(key).strip().lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_redact_context(item, depth + 1) for item in value[:40]]
    if isinstance(value, str):
        return _redact_text(value, 1_000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1_000]


def _fingerprint(
    *,
    platform: str,
    category: str,
    location: str,
    error_type: str,
    http_status: int | None,
    message: str,
) -> str:
    normalized_message = re.sub(r"\b\d+\b", "#", message.lower())[:500]
    material = "|".join(
        [platform, category, location.lower(), error_type.lower(), str(http_status or ""), normalized_message]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _ensure_error_report_schema(cur) -> None:
    cur.execute(
        """
        create table if not exists app_error_reports (
          id uuid primary key default gen_random_uuid(),
          user_id uuid null references users(id) on delete set null,
          external_auth_id varchar(255) not null,
          email varchar(320) null,
          category varchar(30) not null check (category in ('authentication', 'network', 'api', 'validation', 'database', 'realtime', 'media', 'ui', 'unexpected')),
          severity varchar(20) not null check (severity in ('warning', 'error', 'fatal')),
          platform varchar(20) not null check (platform in ('android', 'web')),
          location varchar(200) not null,
          route varchar(500) null,
          error_type varchar(160) not null,
          message text not null,
          technical_detail text null,
          stack_trace text null,
          http_status smallint null check (http_status between 100 and 599),
          request_id varchar(160) null,
          app_version varchar(80) null,
          occurred_at_epoch_ms bigint null,
          context_json jsonb not null default '{}'::jsonb,
          fingerprint varchar(64) not null,
          occurrence_count integer not null default 1,
          status varchar(20) not null default 'open' check (status in ('open', 'reviewing', 'resolved')),
          first_seen_at timestamptz not null default now(),
          last_seen_at timestamptz not null default now(),
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now(),
          unique (external_auth_id, fingerprint)
        )
        """
    )
    cur.execute("create index if not exists idx_app_error_reports_last_seen on app_error_reports (last_seen_at desc)")
    cur.execute("create index if not exists idx_app_error_reports_status on app_error_reports (status, severity, last_seen_at desc)")
    cur.execute("create index if not exists idx_app_error_reports_user on app_error_reports (external_auth_id, last_seen_at desc)")
    cur.execute("create index if not exists idx_app_error_reports_category on app_error_reports (category, platform, last_seen_at desc)")


def _error_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "user_id": str(row["user_id"]) if row.get("user_id") else None,
        "external_auth_id": row.get("external_auth_id"),
        "user_name": row.get("full_name") or row.get("email") or "Unsynced Clerk user",
        "email": row.get("email"),
        "category": row["category"],
        "severity": row["severity"],
        "platform": row["platform"],
        "location": row["location"],
        "route": row.get("route"),
        "error_type": row["error_type"],
        "message": row["message"],
        "technical_detail": row.get("technical_detail"),
        "stack_trace": row.get("stack_trace"),
        "http_status": row.get("http_status"),
        "request_id": row.get("request_id"),
        "app_version": row.get("app_version"),
        "occurred_at_epoch_ms": row.get("occurred_at_epoch_ms"),
        "context": row.get("context_json") or {},
        "fingerprint": row["fingerprint"],
        "occurrence_count": int(row.get("occurrence_count") or 1),
        "status": row["status"],
        "first_seen_at": _iso(row.get("first_seen_at")),
        "last_seen_at": _iso(row.get("last_seen_at")),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)
