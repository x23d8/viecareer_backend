"""Admin-facing feature tracking aggregates.

The raw tracking tables use implementation-level names. This service maps
those names into business feature keys that admins can reason about.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from core.api_usage import ensure_tracking_tables
from core.database import get_connection
from services.landing_event_service import ensure_landing_events_schema

FeatureGroupBy = Literal["day", "week", "month"]


FEATURE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "feature_key": "page_view",
        "label": "Traffic & page views",
        "description": "Website visits and viewed pages from landing tracking.",
        "kind": "traffic",
    },
    {
        "feature_key": "upload_cv",
        "label": "CV uploads",
        "description": "Users parsing CV/JD files before analysis.",
        "kind": "activation",
    },
    {
        "feature_key": "cv_jd_analysis",
        "label": "AI Resume Review",
        "description": "CV/JD analysis runs and saved resume review results.",
        "kind": "ai",
    },
    {
        "feature_key": "realtime_interview",
        "label": "Mock interviews",
        "description": "Realtime interview sessions, adaptive questions, and reports.",
        "kind": "ai",
    },
    {
        "feature_key": "coding_interview",
        "label": "Coding interview",
        "description": "Coding challenge generation and coding interview support.",
        "kind": "ai",
    },
    {
        "feature_key": "action_plan",
        "label": "Action plan",
        "description": "Learning/action plan generation and roadmap activity.",
        "kind": "planning",
    },
    {
        "feature_key": "chatbot",
        "label": "Chatbot",
        "description": "Generic chat, VieBot, and interview chat conversations.",
        "kind": "ai",
    },
    {
        "feature_key": "pricing",
        "label": "Pricing",
        "description": "Pricing page interactions and plan interest.",
        "kind": "conversion",
    },
    {
        "feature_key": "feedback",
        "label": "Feedback",
        "description": "Coding/interview feedback activity when tracked.",
        "kind": "quality",
    },
)

_FEATURE_BY_KEY = {item["feature_key"]: item for item in FEATURE_DEFINITIONS}


def get_feature_metrics(
    *,
    days: int,
    group_by: FeatureGroupBy,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    ensure_tracking_tables()
    ensure_landing_events_schema()

    start_at, end_at = _date_window(days, date_from, date_to)

    with get_connection() as conn:
        with conn.cursor() as cur:
            feature_rows = _fetch_feature_rows(cur, start_at, end_at, group_by)
            feature_totals = _fetch_feature_totals(cur, start_at, end_at)
            page_rows = _fetch_top_pages(cur, start_at, end_at)
            dropoff = _fetch_interview_dropoff(cur, start_at, end_at)

    feature_cards = _feature_cards(feature_totals)
    trend = _feature_trend(feature_rows)
    summary = _summary(feature_cards, dropoff)

    return {
        "window": {
            "date_from": start_at.date(),
            "date_to": (end_at - timedelta(days=1)).date(),
            "group_by": group_by,
            "days": days,
        },
        "summary": summary,
        "features": feature_cards,
        "trend": trend,
        "top_pages": page_rows,
        "dropoffs": {"realtime_interview": dropoff},
    }


def _fetch_feature_rows(cur, start_at: datetime, end_at: datetime, group_by: FeatureGroupBy) -> list[dict[str, Any]]:
    cur.execute(
        """
        with normalized_events as (
          select
            date_trunc(%s, occurred_at)::date as period,
            case
              when feature = 'cvjd_extract' then 'cv_jd_analysis'
              when feature in ('realtime_session', 'realtime_response', 'next_question', 'interview_blueprint', 'interview_score')
                then 'realtime_interview'
              when feature = 'coding_plan' then 'coding_interview'
              when feature in ('chat_viebot', 'chat_generic', 'chat_interview') then 'chatbot'
              else feature
            end as feature_key,
            coalesce(user_id::text, external_auth_id, 'anonymous') as actor_key,
            1 as event_count,
            1 as ai_call_count,
            0 as completion_count,
            coalesce(total_tokens, 0) as total_tokens,
            coalesce(cost_usd, 0) as total_cost_usd
          from api_usage_logs
          where occurred_at >= %s and occurred_at < %s

          union all

          select
            date_trunc(%s, occurred_at)::date as period,
            case
              when event_name in ('cvjd_saved') then 'cv_jd_analysis'
              when event_name in ('file_parse') then 'upload_cv'
              when event_name in ('realtime_token_created', 'interview_started', 'interview_completed', 'report_saved')
                then 'realtime_interview'
              when event_name in ('coding_feedback_generated', 'coding_feedback_saved') then 'feedback'
              else event_name
            end as feature_key,
            coalesce(user_id::text, external_auth_id, 'anonymous') as actor_key,
            1 as event_count,
            0 as ai_call_count,
            case when event_name in ('cvjd_saved', 'interview_completed', 'report_saved', 'coding_feedback_saved') then 1 else 0 end
              as completion_count,
            0 as total_tokens,
            0::numeric as total_cost_usd
          from user_activity_events
          where occurred_at >= %s and occurred_at < %s

          union all

          select
            date_trunc(%s, created_at)::date as period,
            case
              when event_name = 'landing_view' then 'page_view'
              when event_name = 'pricing_click' then 'pricing'
              when event_name in ('signup_click', 'login_click') then 'account_intent'
              else event_name
            end as feature_key,
            coalesce(user_id::text, visitor_id, 'anonymous') as actor_key,
            1 as event_count,
            0 as ai_call_count,
            case when event_name in ('pricing_click', 'signup_click', 'login_click') then 1 else 0 end as completion_count,
            0 as total_tokens,
            0::numeric as total_cost_usd
          from landing_events
          where created_at >= %s and created_at < %s
        )
        select
          period,
          feature_key,
          count(distinct actor_key)::int as unique_users,
          sum(event_count)::int as events,
          sum(ai_call_count)::int as ai_calls,
          sum(completion_count)::int as completions,
          sum(total_tokens)::int as total_tokens,
          coalesce(sum(total_cost_usd), 0) as total_cost_usd
        from normalized_events
        where feature_key in %s
        group by period, feature_key
        order by period asc, feature_key asc
        """,
        (
            group_by,
            start_at,
            end_at,
            group_by,
            start_at,
            end_at,
            group_by,
            start_at,
            end_at,
            tuple(_FEATURE_BY_KEY),
        ),
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_feature_totals(cur, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
    cur.execute(
        """
        with normalized_events as (
          select
            case
              when feature = 'cvjd_extract' then 'cv_jd_analysis'
              when feature in ('realtime_session', 'realtime_response', 'next_question', 'interview_blueprint', 'interview_score')
                then 'realtime_interview'
              when feature = 'coding_plan' then 'coding_interview'
              when feature in ('chat_viebot', 'chat_generic', 'chat_interview') then 'chatbot'
              else feature
            end as feature_key,
            coalesce(user_id::text, external_auth_id, 'anonymous') as actor_key,
            1 as event_count,
            1 as ai_call_count,
            0 as completion_count,
            coalesce(total_tokens, 0) as total_tokens,
            coalesce(cost_usd, 0) as total_cost_usd
          from api_usage_logs
          where occurred_at >= %s and occurred_at < %s

          union all

          select
            case
              when event_name in ('cvjd_saved') then 'cv_jd_analysis'
              when event_name in ('file_parse') then 'upload_cv'
              when event_name in ('realtime_token_created', 'interview_started', 'interview_completed', 'report_saved')
                then 'realtime_interview'
              when event_name in ('coding_feedback_generated', 'coding_feedback_saved') then 'feedback'
              else event_name
            end as feature_key,
            coalesce(user_id::text, external_auth_id, 'anonymous') as actor_key,
            1 as event_count,
            0 as ai_call_count,
            case when event_name in ('cvjd_saved', 'interview_completed', 'report_saved', 'coding_feedback_saved') then 1 else 0 end
              as completion_count,
            0 as total_tokens,
            0::numeric as total_cost_usd
          from user_activity_events
          where occurred_at >= %s and occurred_at < %s

          union all

          select
            case
              when event_name = 'landing_view' then 'page_view'
              when event_name = 'pricing_click' then 'pricing'
              when event_name in ('signup_click', 'login_click') then 'account_intent'
              else event_name
            end as feature_key,
            coalesce(user_id::text, visitor_id, 'anonymous') as actor_key,
            1 as event_count,
            0 as ai_call_count,
            case when event_name in ('pricing_click', 'signup_click', 'login_click') then 1 else 0 end as completion_count,
            0 as total_tokens,
            0::numeric as total_cost_usd
          from landing_events
          where created_at >= %s and created_at < %s
        )
        select
          feature_key,
          count(distinct actor_key)::int as unique_users,
          sum(event_count)::int as events,
          sum(ai_call_count)::int as ai_calls,
          sum(completion_count)::int as completions,
          sum(total_tokens)::int as total_tokens,
          coalesce(sum(total_cost_usd), 0) as total_cost_usd
        from normalized_events
        where feature_key in %s
        group by feature_key
        order by feature_key asc
        """,
        (
            start_at,
            end_at,
            start_at,
            end_at,
            start_at,
            end_at,
            tuple(_FEATURE_BY_KEY),
        ),
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_top_pages(cur, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
    cur.execute(
        """
        select
          coalesce(nullif(path, ''), '/') as path,
          count(*)::int as views,
          count(distinct visitor_id)::int as unique_visitors
        from landing_events
        where event_name = 'landing_view'
          and created_at >= %s
          and created_at < %s
        group by coalesce(nullif(path, ''), '/')
        order by views desc, unique_visitors desc, path asc
        limit 8
        """,
        (start_at, end_at),
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_interview_dropoff(cur, start_at: datetime, end_at: datetime) -> dict[str, Any]:
    cur.execute(
        """
        with starts as (
          select
            id,
            occurred_at,
            object_id,
            coalesce(user_id::text, external_auth_id, 'anonymous') as actor_key
          from user_activity_events
          where event_name = 'interview_started'
            and occurred_at >= %s
            and occurred_at < %s
        ),
        completions as (
          select object_id, min(occurred_at) as completed_at
          from user_activity_events
          where event_name in ('interview_completed', 'report_saved')
            and occurred_at >= %s
          group by object_id
        )
        select
          count(*)::int as started,
          count(distinct starts.actor_key)::int as started_users,
          count(*) filter (where completions.object_id is not null)::int as completed,
          count(*) filter (where completions.object_id is null)::int as abandoned,
          count(distinct starts.actor_key) filter (where completions.object_id is null)::int as abandoned_users
        from starts
        left join completions
          on completions.object_id = starts.object_id
         and completions.completed_at >= starts.occurred_at
        """,
        (start_at, end_at, start_at),
    )
    row = dict(cur.fetchone() or {})
    started = int(row.get("started") or 0)
    abandoned = int(row.get("abandoned") or 0)
    row["abandonment_rate"] = round((abandoned / started) * 100, 1) if started else 0
    row["label"] = "Started mock interviews without a completion event"
    return row


def _feature_cards(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "events": 0,
        "unique_users": 0,
        "ai_calls": 0,
        "completions": 0,
        "total_tokens": 0,
        "total_cost_usd": Decimal("0"),
    })

    for row in rows:
        feature_key = row["feature_key"]
        totals[feature_key]["events"] += int(row.get("events") or 0)
        totals[feature_key]["unique_users"] += int(row.get("unique_users") or 0)
        totals[feature_key]["ai_calls"] += int(row.get("ai_calls") or 0)
        totals[feature_key]["completions"] += int(row.get("completions") or 0)
        totals[feature_key]["total_tokens"] += int(row.get("total_tokens") or 0)
        totals[feature_key]["total_cost_usd"] += Decimal(str(row.get("total_cost_usd") or 0))

    cards = []
    for definition in FEATURE_DEFINITIONS:
        feature_key = definition["feature_key"]
        total = totals[feature_key]
        cards.append(
            {
                **definition,
                "events": total["events"],
                "active_users": total["unique_users"],
                "ai_calls": total["ai_calls"],
                "completions": total["completions"],
                "total_tokens": total["total_tokens"],
                "total_cost_usd": str(total["total_cost_usd"].quantize(Decimal("0.000001"))),
            }
        )
    return cards


def _feature_trend(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    periods: dict[date, dict[str, Any]] = {}
    for row in rows:
        period = row["period"]
        bucket = periods.setdefault(period, {"period": period})
        bucket[row["feature_key"]] = int(row.get("events") or 0)
    return [periods[key] for key in sorted(periods)]


def _summary(features: list[dict[str, Any]], dropoff: dict[str, Any]) -> dict[str, Any]:
    top_feature = max(features, key=lambda item: item["events"], default=None)
    by_key = {feature["feature_key"]: feature for feature in features}
    return {
        "total_events": sum(feature["events"] for feature in features),
        "active_users": sum(feature["active_users"] for feature in features),
        "ai_calls": sum(feature["ai_calls"] for feature in features),
        "total_cost_usd": str(
            sum(Decimal(str(feature["total_cost_usd"])) for feature in features).quantize(Decimal("0.000001"))
        ),
        "cv_uploads": by_key.get("upload_cv", {}).get("events", 0),
        "resume_reviews": by_key.get("cv_jd_analysis", {}).get("events", 0),
        "completed_mock_interviews": by_key.get("realtime_interview", {}).get("completions", 0),
        "abandoned_interviews": dropoff.get("abandoned", 0),
        "most_used_feature": top_feature,
    }

def _date_window(days: int, date_from: date | None = None, date_to: date | None = None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if date_from or date_to:
        start_date = date_from or (now.date() - timedelta(days=days - 1))
        end_date = date_to or now.date()
        start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
        end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
        return start_at, end_at

    end_at = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=timezone.utc)
    start_at = end_at - timedelta(days=days)
    return start_at, end_at
