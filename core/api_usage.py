"""Low-overhead API cost and user activity tracking.

Tracking is intentionally best-effort: failures are logged server-side and are
never allowed to affect the user-facing request path.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any

from psycopg2.extras import Json

from core.database import get_connection

logger = logging.getLogger("viecareer")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="usage-log")
_schema_lock = Lock()
_schema_ready = False

DEFAULT_MODEL_PRICING: dict[str, dict[str, float]] = {
    # USD per 1M text tokens. Override with OPENAI_PRICING_JSON when prices change.
    "gpt-4o": {"input_per_1m": 2.50, "output_per_1m": 10.00},
    "gpt-4o-mini": {"input_per_1m": 0.15, "output_per_1m": 0.60},
    "gpt-realtime-2": {"input_per_1m": 4.00, "output_per_1m": 24.00},
    "gpt-realtime-mini": {"input_per_1m": 4.00, "output_per_1m": 24.00},
}


@dataclass(frozen=True)
class UsageContext:
    feature: str
    external_auth_id: str | None = None
    user_id: str | None = None
    related_type: str | None = None
    related_id: str | None = None
    meta: dict[str, Any] | None = None


def usage_context(
    *,
    feature: str,
    external_auth_id: str | None = None,
    user_id: str | None = None,
    related_type: str | None = None,
    related_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> UsageContext:
    return UsageContext(
        feature=feature,
        external_auth_id=external_auth_id,
        user_id=user_id,
        related_type=related_type,
        related_id=related_id,
        meta=meta,
    )


def track_openai_usage_async(
    response: Any,
    context: UsageContext | None,
    *,
    status: str = "success",
    model: str | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
    meta: dict[str, Any] | None = None,
    skip_without_usage: bool = False,
    cost_usd_override: Decimal | float | str | None = None,
) -> None:
    if context is None:
        return

    usage = _extract_usage(response)
    if skip_without_usage and status == "success" and not usage["has_usage"]:
        return

    model_name = model or _extract_model(response) or "unknown"
    merged_meta = _compact_meta(context.meta, meta)
    cost_usd = _decimal_cost(cost_usd_override)
    if cost_usd is None:
        cost_usd = estimate_cost_usd(
            model_name,
            usage["prompt_tokens"],
            usage["completion_tokens"],
        )

    record = {
        "external_auth_id": context.external_auth_id,
        "user_id": context.user_id,
        "provider": "openai",
        "model": model_name,
        "feature": context.feature,
        "related_type": context.related_type,
        "related_id": _uuid_or_none(context.related_id),
        "status": status,
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "input_audio_seconds": usage["input_audio_seconds"],
        "output_audio_seconds": usage["output_audio_seconds"],
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "error_code": error_code,
        "meta_json": merged_meta,
    }
    _submit(_insert_api_usage, record)


def track_blocked_ai_usage_async(
    context: UsageContext | None,
    *,
    error_code: str,
    model: str = "none",
    duration_ms: int | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Record an AI request blocked before provider execution."""

    track_openai_usage_async(
        {
            "model": model,
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        },
        context,
        status="blocked",
        model=model,
        duration_ms=duration_ms,
        error_code=error_code,
        meta=meta,
        cost_usd_override=0,
    )


def track_user_activity_async(
    event_name: str,
    *,
    external_auth_id: str | None = None,
    user_id: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    source: str = "backend",
    success: bool = True,
    duration_ms: int | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    record = {
        "external_auth_id": external_auth_id,
        "user_id": user_id,
        "event_name": event_name,
        "object_type": object_type,
        "object_id": _uuid_or_none(object_id),
        "source": source,
        "success": success,
        "duration_ms": duration_ms,
        "meta_json": _compact_meta(meta),
    }
    _submit(_insert_activity, record)


def ensure_tracking_tables() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            _ensure_tracking_schema(cur)
        conn.commit()


def estimate_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> Decimal | None:
    pricing = _pricing_for_model(model)
    if not pricing:
        return None

    input_price = _decimal_price(pricing.get("input_per_1m") or pricing.get("input"))
    output_price = _decimal_price(pricing.get("output_per_1m") or pricing.get("output"))
    if input_price is None or output_price is None:
        return None

    million = Decimal("1000000")
    cost = (Decimal(prompt_tokens) * input_price / million) + (
        Decimal(completion_tokens) * output_price / million
    )
    return cost.quantize(Decimal("0.000001"))


def _submit(fn, record: dict[str, Any]) -> None:
    try:
        _executor.submit(fn, record)
    except Exception:
        logger.debug("Could not schedule usage tracking", exc_info=True)


def _insert_api_usage(record: dict[str, Any]) -> None:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_tracking_schema(cur)
                internal_user_id = _resolve_user_id(
                    cur,
                    record.get("user_id"),
                    record.get("external_auth_id"),
                )
                cur.execute(
                    """
                    insert into api_usage_logs (
                      user_id,
                      external_auth_id,
                      provider,
                      model,
                      feature,
                      related_type,
                      related_id,
                      status,
                      prompt_tokens,
                      completion_tokens,
                      total_tokens,
                      input_audio_seconds,
                      output_audio_seconds,
                      cost_usd,
                      duration_ms,
                      error_code,
                      meta_json
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        internal_user_id,
                        record.get("external_auth_id"),
                        record["provider"],
                        record["model"],
                        record["feature"],
                        record.get("related_type"),
                        record.get("related_id"),
                        record["status"],
                        record["prompt_tokens"],
                        record["completion_tokens"],
                        record["total_tokens"],
                        record.get("input_audio_seconds"),
                        record.get("output_audio_seconds"),
                        record.get("cost_usd"),
                        record.get("duration_ms"),
                        record.get("error_code"),
                        Json(record["meta_json"]) if record.get("meta_json") else None,
                    ),
                )
            conn.commit()
    except Exception:
        logger.debug("API usage tracking failed", exc_info=True)


def _insert_activity(record: dict[str, Any]) -> None:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_tracking_schema(cur)
                internal_user_id = _resolve_user_id(
                    cur,
                    record.get("user_id"),
                    record.get("external_auth_id"),
                )
                cur.execute(
                    """
                    insert into user_activity_events (
                      user_id,
                      external_auth_id,
                      event_name,
                      object_type,
                      object_id,
                      source,
                      success,
                      duration_ms,
                      meta_json
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        internal_user_id,
                        record.get("external_auth_id"),
                        record["event_name"],
                        record.get("object_type"),
                        record.get("object_id"),
                        record["source"],
                        record["success"],
                        record.get("duration_ms"),
                        Json(record["meta_json"]) if record.get("meta_json") else None,
                    ),
                )
            conn.commit()
    except Exception:
        logger.debug("User activity tracking failed", exc_info=True)


def _ensure_tracking_schema(cur) -> None:
    global _schema_ready
    if _schema_ready:
        return

    with _schema_lock:
        if _schema_ready:
            return
        cur.execute(
            """
            create table if not exists api_usage_logs (
              id bigserial primary key,
              occurred_at timestamptz not null default now(),
              user_id uuid null references users(id) on delete set null,
              external_auth_id varchar(255) null,
              provider varchar(30) not null default 'openai',
              model varchar(100) not null,
              feature varchar(60) not null,
              related_type varchar(40) null,
              related_id uuid null,
              status varchar(20) not null check (status in ('success', 'error', 'cancelled', 'blocked')),
              prompt_tokens int not null default 0,
              completion_tokens int not null default 0,
              total_tokens int not null default 0,
              input_audio_seconds int null,
              output_audio_seconds int null,
              cost_usd numeric(12,6) null,
              duration_ms int null,
              error_code varchar(80) null,
              meta_json jsonb null
            )
            """
        )
        cur.execute("alter table api_usage_logs drop constraint if exists api_usage_logs_status_check")
        cur.execute(
            """
            alter table api_usage_logs
            add constraint api_usage_logs_status_check
            check (status in ('success', 'error', 'cancelled', 'blocked'))
            """
        )
        cur.execute(
            """
            create table if not exists user_activity_events (
              id bigserial primary key,
              occurred_at timestamptz not null default now(),
              user_id uuid null references users(id) on delete set null,
              external_auth_id varchar(255) null,
              event_name varchar(80) not null,
              object_type varchar(40) null,
              object_id uuid null,
              source varchar(20) not null default 'backend',
              success boolean not null default true,
              duration_ms int null,
              meta_json jsonb null
            )
            """
        )
        cur.execute(
            """
            create index if not exists idx_api_usage_logs_user_time
            on api_usage_logs (user_id, occurred_at desc)
            """
        )
        cur.execute(
            """
            create index if not exists idx_api_usage_logs_feature_time
            on api_usage_logs (feature, occurred_at desc)
            """
        )
        cur.execute(
            """
            create index if not exists idx_user_activity_events_user_time
            on user_activity_events (user_id, occurred_at desc)
            """
        )
        cur.execute(
            """
            create index if not exists idx_user_activity_events_name_time
            on user_activity_events (event_name, occurred_at desc)
            """
        )
        _schema_ready = True


def _resolve_user_id(cur, user_id: str | None, external_auth_id: str | None) -> str | None:
    if _uuid_or_none(user_id):
        return str(user_id)
    if not external_auth_id:
        return None

    cur.execute(
        """
        select id
        from users
        where external_auth_id = %s
        limit 1
        """,
        (external_auth_id,),
    )
    row = cur.fetchone()
    return str(row["id"]) if row else None


def _extract_usage(response: Any) -> dict[str, Any]:
    usage = _get_usage_object(response)
    prompt_tokens = _int_attr(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _int_attr(usage, "completion_tokens", "output_tokens")
    total_tokens = _int_attr(usage, "total_tokens") or prompt_tokens + completion_tokens
    return {
        "has_usage": usage is not None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_audio_seconds": _int_attr(usage, "input_audio_seconds"),
        "output_audio_seconds": _int_attr(usage, "output_audio_seconds"),
    }


def _get_usage_object(response: Any) -> Any:
    if response is None:
        return None
    if isinstance(response, dict):
        direct = response.get("usage")
        if direct is not None:
            return direct
        nested = response.get("response")
        if isinstance(nested, dict):
            return nested.get("usage")
        return None
    return getattr(response, "usage", None)


def _extract_model(response: Any) -> str | None:
    if response is None:
        return None
    if isinstance(response, dict):
        direct = response.get("model")
        if direct:
            return str(direct)
        nested = response.get("response")
        if isinstance(nested, dict) and nested.get("model"):
            return str(nested["model"])
        return None
    value = getattr(response, "model", None)
    return str(value) if value else None


def _int_attr(obj: Any, *names: str) -> int:
    if obj is None:
        return 0
    for name in names:
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0
    return 0


def _pricing_for_model(model: str) -> dict[str, Any] | None:
    pricing = _pricing_table()
    if model in pricing:
        return pricing[model]

    for key in sorted(pricing, key=len, reverse=True):
        if model.startswith(f"{key}-"):
            return pricing[key]
    return None


def _pricing_table() -> dict[str, dict[str, Any]]:
    raw = os.getenv("OPENAI_PRICING_JSON", "").strip()
    if not raw:
        return DEFAULT_MODEL_PRICING

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("OPENAI_PRICING_JSON is invalid; falling back to defaults")
        return DEFAULT_MODEL_PRICING

    if not isinstance(parsed, dict):
        return DEFAULT_MODEL_PRICING

    merged = dict(DEFAULT_MODEL_PRICING)
    for model, value in parsed.items():
        if isinstance(model, str) and isinstance(value, dict):
            merged[model] = value
    return merged


def _decimal_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimal_cost(value: Any) -> Decimal | None:
    raw = _decimal_price(value)
    if raw is None:
        return None
    return raw.quantize(Decimal("0.000001"))


def _uuid_or_none(value: Any) -> str | None:
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None


def _compact_meta(*parts: dict[str, Any] | None) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key, value in part.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                merged[key] = value
            elif isinstance(value, (list, tuple, set)):
                merged[key] = len(value)
            elif isinstance(value, dict):
                merged[key] = {str(k): v for k, v in list(value.items())[:12] if isinstance(v, (str, int, float, bool))}
    return merged or None
