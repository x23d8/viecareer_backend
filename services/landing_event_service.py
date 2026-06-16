from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status

from core.database import get_connection
from schemas.events import LandingEventRequest, LandingMetricGroupBy, LandingSource


EVENT_NAMES = ("landing_view", "pricing_click", "signup_click", "login_click")
SOURCE_VALUES = ("facebook", "tiktok", "google", "direct", "other")

_schema_ready = False


def ensure_landing_events_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                create table if not exists landing_events (
                  id uuid primary key default gen_random_uuid(),
                  visitor_id varchar(100) not null,
                  user_id uuid null references users(id) on delete set null,
                  event_name varchar(50) not null check (
                    event_name in ('landing_view', 'pricing_click', 'signup_click', 'login_click')
                  ),
                  source varchar(50) not null default 'direct' check (
                    source in ('facebook', 'tiktok', 'google', 'direct', 'other')
                  ),
                  utm_source varchar(100) null,
                  utm_medium varchar(100) null,
                  utm_campaign varchar(150) null,
                  referrer text null,
                  path text null,
                  target_path text null,
                  pricing_plan_code varchar(50) null,
                  ip_address varchar(64) null,
                  user_agent text null,
                  created_at timestamptz not null default now()
                )
                """
            )
            cur.execute("create index if not exists idx_landing_events_created on landing_events (created_at desc)")
            cur.execute(
                "create index if not exists idx_landing_events_source_created on landing_events (source, created_at desc)"
            )
            cur.execute(
                "create index if not exists idx_landing_events_event_created on landing_events (event_name, created_at desc)"
            )
            cur.execute(
                "create index if not exists idx_landing_events_visitor_created on landing_events (visitor_id, created_at desc)"
            )
        conn.commit()
    _schema_ready = True


def record_landing_event(
    payload: LandingEventRequest,
    *,
    external_auth_id: str | None,
    ip_address: str | None,
    user_agent: str | None,
) -> dict[str, Any]:
    ensure_landing_events_schema()

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                user_id = _lookup_user_id(cur, external_auth_id)

                cur.execute(
                    """
                    select id
                    from landing_events
                    where visitor_id = %s
                      and event_name = %s
                      and coalesce(path, '') = coalesce(%s, '')
                      and created_at >= now() - interval '10 seconds'
                    order by created_at desc
                    limit 1
                    """,
                    (payload.visitor_id, payload.event_name, payload.path),
                )
                duplicate = cur.fetchone()
                if duplicate:
                    return {"id": str(duplicate["id"]), "deduplicated": True}

                cur.execute(
                    """
                    insert into landing_events (
                      visitor_id,
                      user_id,
                      event_name,
                      source,
                      utm_source,
                      utm_medium,
                      utm_campaign,
                      referrer,
                      path,
                      target_path,
                      pricing_plan_code,
                      ip_address,
                      user_agent
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    returning id, created_at
                    """,
                    (
                        payload.visitor_id,
                        user_id,
                        payload.event_name,
                        payload.source,
                        payload.utm_source,
                        payload.utm_medium,
                        payload.utm_campaign,
                        payload.referrer,
                        payload.path,
                        payload.target_path,
                        payload.pricing_plan_code,
                        ip_address,
                        user_agent,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to record landing event.",
        ) from exc

    return {"id": str(row["id"]), "created_at": row["created_at"], "deduplicated": False}


def get_landing_metrics(
    *,
    date_from: date | None,
    date_to: date | None,
    source: LandingSource | None,
    group_by: LandingMetricGroupBy,
) -> dict[str, Any]:
    ensure_landing_events_schema()

    start_at, end_at = _date_window(date_from, date_to)
    where_parts = ["created_at >= %s", "created_at < %s"]
    params: list[Any] = [start_at, end_at]
    if source:
        where_parts.append("source = %s")
        params.append(source)
    where_sql = " and ".join(where_parts)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select
                      event_name,
                      count(*)::int as total,
                      count(distinct visitor_id)::int as unique_visitors
                    from landing_events
                    where {where_sql}
                    group by event_name
                    """,
                    params,
                )
                event_rows = cur.fetchall()

                cur.execute(
                    f"""
                    select
                      source,
                      count(*) filter (where event_name = 'landing_view')::int as landing_views,
                      count(*) filter (where event_name = 'pricing_click')::int as pricing_clicks,
                      count(*) filter (where event_name = 'signup_click')::int as signup_clicks,
                      count(*) filter (where event_name = 'login_click')::int as login_clicks,
                      count(distinct visitor_id)::int as unique_visitors
                    from landing_events
                    where {where_sql}
                    group by source
                    order by landing_views desc, source asc
                    """,
                    params,
                )
                by_source = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    f"""
                    select
                      date_trunc(%s, created_at)::date as period,
                      count(*) filter (where event_name = 'landing_view')::int as landing_views,
                      count(*) filter (where event_name = 'pricing_click')::int as pricing_clicks,
                      count(*) filter (where event_name = 'signup_click')::int as signup_clicks,
                      count(*) filter (where event_name = 'login_click')::int as login_clicks,
                      count(distinct visitor_id)::int as unique_visitors
                    from landing_events
                    where {where_sql}
                    group by period
                    order by period asc
                    """,
                    [group_by, *params],
                )
                trend = [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load landing metrics.",
        ) from exc

    summary = {event_name: 0 for event_name in EVENT_NAMES}
    unique_by_event = {event_name: 0 for event_name in EVENT_NAMES}
    for row in event_rows:
        summary[row["event_name"]] = row["total"]
        unique_by_event[row["event_name"]] = row["unique_visitors"]

    return {
        "window": {"date_from": start_at.date(), "date_to": (end_at - timedelta(days=1)).date(), "group_by": group_by},
        "summary": {
            "landing_views": summary["landing_view"],
            "pricing_clicks": summary["pricing_click"],
            "signup_clicks": summary["signup_click"],
            "login_clicks": summary["login_click"],
            "unique_visitors": unique_by_event["landing_view"],
        },
        "by_source": by_source,
        "trend": trend,
        "integrations": {
            "google_analytics": {
                "measurement_id": "G-QNBNB5Z6FP",
                "status": "client_side_gtag_enabled",
                "tracked_events": ["page_view", *EVENT_NAMES],
                "dashboard_note": "Use GA4 for marketing attribution, acquisition channels, and campaign comparison.",
            },
            "custom_tracking": {
                "status": "database_events_enabled",
                "endpoint": "POST /api/events/landing",
                "table": "landing_events",
                "tracked_fields": [
                    "visitor_id",
                    "user_id",
                    "event_name",
                    "source",
                    "utm_source",
                    "utm_medium",
                    "utm_campaign",
                    "referrer",
                    "path",
                    "target_path",
                    "pricing_plan_code",
                    "ip_address",
                    "user_agent",
                    "created_at",
                ],
            },
        },
    }


def _lookup_user_id(cur, external_auth_id: str | None) -> str | None:
    if not external_auth_id:
        return None
    cur.execute("select id from users where external_auth_id = %s", (external_auth_id,))
    row = cur.fetchone()
    return str(row["id"]) if row else None


def _date_window(date_from: date | None, date_to: date | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start_date = date_from or (now.date() - timedelta(days=29))
    end_date = date_to or now.date()
    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start_at, end_at
