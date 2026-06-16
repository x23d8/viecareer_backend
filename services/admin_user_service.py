"""Admin user management and per-user analytics."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from core.api_usage import ensure_tracking_tables
from core.database import get_connection
from services.landing_event_service import ensure_landing_events_schema

UserMetricGroupBy = Literal["day", "week", "month"]


def list_admin_users(
    *,
    limit: int,
    offset: int,
    search: str | None,
    status_filter: str | None,
    plan_filter: str | None,
) -> dict[str, Any]:
    ensure_tracking_tables()

    where_parts = ["true"]
    params: list[Any] = []

    if search:
        where_parts.append(
            """
            (
              u.id::text ilike %s
              or u.external_auth_id ilike %s
              or u.email ilike %s
              or u.full_name ilike %s
            )
            """
        )
        term = f"%{search}%"
        params.extend([term, term, term, term])

    if status_filter:
        where_parts.append("u.status = %s")
        params.append(status_filter)

    if plan_filter:
        where_parts.append("coalesce(p.code, 'free') = %s")
        params.append(plan_filter)

    where_sql = " and ".join(where_parts)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                  count(*)::int as total,
                  count(*) filter (where status = 'active')::int as active,
                  count(*) filter (where status = 'suspended')::int as suspended,
                  count(*) filter (where status = 'pending')::int as pending
                from users
                """
            )
            stats = dict(cur.fetchone() or {})

            cur.execute(
                f"""
                with latest_subscription as (
                  select distinct on (user_id)
                    user_id,
                    plan_id,
                    status,
                    start_at,
                    end_at
                  from user_subscriptions
                  order by user_id, start_at desc, id desc
                ),
                filtered_page as (
                  select
                    u.id,
                    u.external_auth_id,
                    u.email,
                    u.full_name,
                    u.role_id,
                    r.code as role_code,
                    u.status,
                    u.avatar_url,
                    u.last_active_at,
                    u.created_at,
                    u.updated_at,
                    up.birth_year,
                    up.university,
                    up.target_position,
                    coalesce(p.code, 'free') as plan_code,
                    coalesce(p.name, 'Free') as plan_name,
                    coalesce(p.quota_total, 0)::int as quota_total,
                    coalesce(p.rate_limit_per_minute, 0)::int as rate_limit_per_minute
                  from users u
                  left join roles r on r.id = u.role_id
                  left join user_profiles up on up.user_id = u.id
                  left join latest_subscription ls on ls.user_id = u.id
                  left join plans p on p.id = ls.plan_id
                  where {where_sql}
                  order by coalesce(u.last_active_at, u.updated_at, u.created_at) desc nulls last, u.created_at desc
                  limit %s offset %s
                ),
                session_counts as (
                  select user_id, count(*)::int as total_sessions
                  from interview_sessions
                  where user_id in (select id from filtered_page)
                  group by user_id
                ),
                usage_totals as (
                  select
                    fp.id as user_id,
                    count(l.*)::int as ai_calls,
                    coalesce(sum(l.cost_usd), 0) as total_cost_usd,
                    coalesce(sum(l.total_tokens), 0)::int as total_tokens
                  from filtered_page fp
                  left join api_usage_logs l
                    on l.user_id = fp.id
                    or (
                      fp.external_auth_id is not null
                      and l.external_auth_id = fp.external_auth_id
                    )
                  group by fp.id
                ),
                activity_totals as (
                  select
                    fp.id as user_id,
                    count(e.*)::int as activity_events,
                    max(e.occurred_at) as last_event_at
                  from filtered_page fp
                  left join user_activity_events e
                    on e.user_id = fp.id
                    or (
                      fp.external_auth_id is not null
                      and e.external_auth_id = fp.external_auth_id
                    )
                  group by fp.id
                )
                select
                  fp.*,
                  coalesce(sc.total_sessions, 0)::int as total_sessions,
                  coalesce(ut.ai_calls, 0)::int as ai_calls,
                  coalesce(ut.total_tokens, 0)::int as total_tokens,
                  coalesce(ut.total_cost_usd, 0) as total_cost_usd,
                  coalesce(at.activity_events, 0)::int as activity_events,
                  coalesce(fp.last_active_at, at.last_event_at, fp.updated_at, fp.created_at) as effective_last_active_at
                from filtered_page fp
                left join session_counts sc on sc.user_id = fp.id
                left join usage_totals ut on ut.user_id = fp.id
                left join activity_totals at on at.user_id = fp.id
                order by effective_last_active_at desc nulls last, fp.created_at desc
                """,
                (*params, limit, offset),
            )
            users = [dict(row) for row in cur.fetchall()]

            cur.execute(
                f"""
                with latest_subscription as (
                  select distinct on (user_id) user_id, plan_id
                  from user_subscriptions
                  order by user_id, start_at desc, id desc
                )
                select count(*)::int as filtered_total
                from users u
                left join latest_subscription ls on ls.user_id = u.id
                left join plans p on p.id = ls.plan_id
                where {where_sql}
                """,
                params,
            )
            filtered_total = int((cur.fetchone() or {}).get("filtered_total") or 0)

    return {
        "stats": stats,
        "items": [_serialize_user(row) for row in users],
        "pagination": {"limit": limit, "offset": offset, "total": filtered_total},
    }


def get_admin_user_metrics(
    *,
    user_id: str,
    days: int,
    group_by: UserMetricGroupBy,
) -> dict[str, Any]:
    ensure_tracking_tables()
    ensure_landing_events_schema()
    internal_user_id = str(UUID(user_id))
    start_at, end_at = _date_window(days)

    with get_connection() as conn:
        with conn.cursor() as cur:
            user = _fetch_user(cur, internal_user_id)
            external_auth_id = user.get("external_auth_id")
            feature_totals = _fetch_user_feature_totals(
                cur,
                internal_user_id,
                external_auth_id,
                start_at,
                end_at,
            )
            trend = _fetch_user_feature_trend(
                cur,
                internal_user_id,
                external_auth_id,
                start_at,
                end_at,
                group_by,
            )
            model_costs = _fetch_user_model_costs(cur, internal_user_id, external_auth_id, start_at, end_at)
            recent_events = _fetch_user_recent_events(cur, internal_user_id, external_auth_id, limit=30)

    features = _feature_cards(feature_totals)
    return {
        "window": {
            "date_from": start_at.date(),
            "date_to": (end_at - timedelta(days=1)).date(),
            "group_by": group_by,
            "days": days,
        },
        "user": _serialize_user(user),
        "summary": _user_metric_summary(features, model_costs, recent_events),
        "features": features,
        "trend": trend,
        "model_costs": model_costs,
        "recent_events": recent_events,
    }


def _fetch_user(cur, user_id: str) -> dict[str, Any]:
    cur.execute(
        """
        with target_user as (
          select *
          from users
          where id = %s
        ),
        latest_subscription as (
          select distinct on (user_id)
            user_id,
            plan_id,
            status,
            start_at,
            end_at
          from user_subscriptions
          order by user_id, start_at desc, id desc
        ),
        session_counts as (
          select user_id, count(*)::int as total_sessions
          from interview_sessions
          where user_id in (select id from target_user)
          group by user_id
        ),
        usage_totals as (
          select
            tu.id as user_id,
            count(l.*)::int as ai_calls,
            coalesce(sum(l.cost_usd), 0) as total_cost_usd,
            coalesce(sum(l.total_tokens), 0)::int as total_tokens
          from target_user tu
          left join api_usage_logs l
            on l.user_id = tu.id
            or (
              tu.external_auth_id is not null
              and l.external_auth_id = tu.external_auth_id
            )
          group by tu.id
        ),
        activity_totals as (
          select
            tu.id as user_id,
            count(e.*)::int as activity_events,
            max(e.occurred_at) as last_event_at
          from target_user tu
          left join user_activity_events e
            on e.user_id = tu.id
            or (
              tu.external_auth_id is not null
              and e.external_auth_id = tu.external_auth_id
            )
          group by tu.id
        )
        select
          u.id,
          u.external_auth_id,
          u.email,
          u.full_name,
          u.role_id,
          r.code as role_code,
          u.status,
          u.avatar_url,
          u.last_active_at,
          u.created_at,
          u.updated_at,
          up.birth_year,
          up.university,
          up.target_position,
          coalesce(p.code, 'free') as plan_code,
          coalesce(p.name, 'Free') as plan_name,
          coalesce(p.quota_total, 0)::int as quota_total,
          coalesce(p.rate_limit_per_minute, 0)::int as rate_limit_per_minute,
          coalesce(sc.total_sessions, 0)::int as total_sessions,
          coalesce(ut.ai_calls, 0)::int as ai_calls,
          coalesce(ut.total_tokens, 0)::int as total_tokens,
          coalesce(ut.total_cost_usd, 0) as total_cost_usd,
          coalesce(at.activity_events, 0)::int as activity_events,
          coalesce(u.last_active_at, at.last_event_at, u.updated_at, u.created_at) as effective_last_active_at
        from target_user u
        left join roles r on r.id = u.role_id
        left join user_profiles up on up.user_id = u.id
        left join latest_subscription ls on ls.user_id = u.id
        left join plans p on p.id = ls.plan_id
        left join session_counts sc on sc.user_id = u.id
        left join usage_totals ut on ut.user_id = u.id
        left join activity_totals at on at.user_id = u.id
        limit 1
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError("User not found")
    return dict(row)


def _fetch_user_feature_totals(
    cur,
    user_id: str,
    external_auth_id: str | None,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
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
            1 as event_count,
            1 as ai_call_count,
            0 as completion_count,
            coalesce(total_tokens, 0) as total_tokens,
            coalesce(cost_usd, 0) as total_cost_usd
          from api_usage_logs
          where (user_id = %s or (%s is not null and external_auth_id = %s))
            and occurred_at >= %s
            and occurred_at < %s

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
            1 as event_count,
            0 as ai_call_count,
            case when event_name in ('cvjd_saved', 'interview_completed', 'report_saved', 'coding_feedback_saved') then 1 else 0 end
              as completion_count,
            0 as total_tokens,
            0::numeric as total_cost_usd
          from user_activity_events
          where (user_id = %s or (%s is not null and external_auth_id = %s))
            and occurred_at >= %s
            and occurred_at < %s

          union all

          select
            case
              when event_name = 'landing_view' then 'page_view'
              when event_name = 'pricing_click' then 'pricing'
              when event_name in ('signup_click', 'login_click') then 'account_intent'
              else event_name
            end as feature_key,
            1 as event_count,
            0 as ai_call_count,
            case when event_name in ('pricing_click', 'signup_click', 'login_click') then 1 else 0 end as completion_count,
            0 as total_tokens,
            0::numeric as total_cost_usd
          from landing_events
          where user_id = %s and created_at >= %s and created_at < %s
        )
        select
          feature_key,
          sum(event_count)::int as events,
          sum(ai_call_count)::int as ai_calls,
          sum(completion_count)::int as completions,
          sum(total_tokens)::int as total_tokens,
          coalesce(sum(total_cost_usd), 0) as total_cost_usd
        from normalized_events
        where feature_key in ('page_view', 'upload_cv', 'cv_jd_analysis', 'realtime_interview', 'coding_interview', 'action_plan', 'chatbot', 'pricing', 'feedback')
        group by feature_key
        order by feature_key asc
        """,
        (
            user_id,
            external_auth_id,
            external_auth_id,
            start_at,
            end_at,
            user_id,
            external_auth_id,
            external_auth_id,
            start_at,
            end_at,
            user_id,
            start_at,
            end_at,
        ),
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_user_feature_trend(
    cur,
    user_id: str,
    external_auth_id: str | None,
    start_at: datetime,
    end_at: datetime,
    group_by: UserMetricGroupBy,
) -> list[dict[str, Any]]:
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
            1 as event_count
          from api_usage_logs
          where (user_id = %s or (%s is not null and external_auth_id = %s))
            and occurred_at >= %s
            and occurred_at < %s

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
            1 as event_count
          from user_activity_events
          where (user_id = %s or (%s is not null and external_auth_id = %s))
            and occurred_at >= %s
            and occurred_at < %s
        )
        select period, feature_key, sum(event_count)::int as events
        from normalized_events
        where feature_key in ('page_view', 'upload_cv', 'cv_jd_analysis', 'realtime_interview', 'coding_interview', 'action_plan', 'chatbot', 'pricing', 'feedback')
        group by period, feature_key
        order by period asc, feature_key asc
        """,
        (
            group_by,
            user_id,
            external_auth_id,
            external_auth_id,
            start_at,
            end_at,
            group_by,
            user_id,
            external_auth_id,
            external_auth_id,
            start_at,
            end_at,
        ),
    )
    periods: dict[Any, dict[str, Any]] = {}
    for row in cur.fetchall():
        period = row["period"]
        bucket = periods.setdefault(period, {"period": period})
        bucket[row["feature_key"]] = int(row.get("events") or 0)
    return [periods[key] for key in sorted(periods)]


def _fetch_user_model_costs(
    cur,
    user_id: str,
    external_auth_id: str | None,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    cur.execute(
        """
        select
          model,
          count(*)::int as ai_calls,
          coalesce(sum(total_tokens), 0)::int as total_tokens,
          coalesce(sum(cost_usd), 0) as total_cost_usd,
          max(occurred_at) as last_used_at
        from api_usage_logs
        where (user_id = %s or (%s is not null and external_auth_id = %s))
          and occurred_at >= %s
          and occurred_at < %s
        group by model
        order by total_cost_usd desc, ai_calls desc
        """,
        (user_id, external_auth_id, external_auth_id, start_at, end_at),
    )
    return [_serialize_money_row(dict(row)) for row in cur.fetchall()]


def _fetch_user_recent_events(cur, user_id: str, external_auth_id: str | None, limit: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        select
          occurred_at,
          event_name,
          object_type,
          object_id,
          success,
          duration_ms,
          meta_json
        from user_activity_events
        where user_id = %s or (%s is not null and external_auth_id = %s)
        order by occurred_at desc
        limit %s
        """,
        (user_id, external_auth_id, external_auth_id, limit),
    )
    return [dict(row) for row in cur.fetchall()]


def _feature_cards(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = {
        "page_view": "Traffic & page views",
        "upload_cv": "CV uploads",
        "cv_jd_analysis": "AI Resume Review",
        "realtime_interview": "Mock interviews",
        "coding_interview": "Coding interview",
        "action_plan": "Action plan",
        "chatbot": "Chatbot",
        "pricing": "Pricing",
        "feedback": "Feedback",
    }
    by_key = {row["feature_key"]: row for row in rows}
    cards = []
    for feature_key, label in labels.items():
        row = by_key.get(feature_key, {})
        cards.append(
            {
                "feature_key": feature_key,
                "label": label,
                "events": int(row.get("events") or 0),
                "ai_calls": int(row.get("ai_calls") or 0),
                "completions": int(row.get("completions") or 0),
                "total_tokens": int(row.get("total_tokens") or 0),
                "total_cost_usd": str(Decimal(str(row.get("total_cost_usd") or 0)).quantize(Decimal("0.000001"))),
            }
        )
    return cards


def _user_metric_summary(
    features: list[dict[str, Any]],
    model_costs: list[dict[str, Any]],
    recent_events: list[dict[str, Any]],
) -> dict[str, Any]:
    total_cost = sum(Decimal(str(feature["total_cost_usd"])) for feature in features)
    top_feature = max(features, key=lambda item: item["events"], default=None)
    return {
        "total_events": sum(feature["events"] for feature in features),
        "ai_calls": sum(feature["ai_calls"] for feature in features),
        "total_tokens": sum(feature["total_tokens"] for feature in features),
        "total_cost_usd": str(total_cost.quantize(Decimal("0.000001"))),
        "features_used": sum(1 for feature in features if feature["events"] > 0),
        "top_feature": top_feature,
        "models_used": len(model_costs),
        "last_event_at": recent_events[0]["occurred_at"] if recent_events else None,
    }


def _serialize_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "external_auth_id": row.get("external_auth_id"),
        "email": row.get("email"),
        "full_name": row.get("full_name"),
        "role_id": row.get("role_id"),
        "role_code": row.get("role_code"),
        "status": row.get("status"),
        "avatar_url": row.get("avatar_url"),
        "created_at": row.get("created_at"),
        "last_active_at": row.get("effective_last_active_at") or row.get("last_active_at"),
        "profile": {
            "birth_year": row.get("birth_year"),
            "university": row.get("university"),
            "target_position": row.get("target_position"),
        },
        "plan": {
            "code": row.get("plan_code") or "free",
            "name": row.get("plan_name") or "Free",
            "quota_total": int(row.get("quota_total") or 0),
            "rate_limit_per_minute": int(row.get("rate_limit_per_minute") or 0),
        },
        "usage": {
            "total_sessions": int(row.get("total_sessions") or 0),
            "ai_calls": int(row.get("ai_calls") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
            "total_cost_usd": str(Decimal(str(row.get("total_cost_usd") or 0)).quantize(Decimal("0.000001"))),
            "activity_events": int(row.get("activity_events") or 0),
        },
    }


def _serialize_money_row(row: dict[str, Any]) -> dict[str, Any]:
    row["total_cost_usd"] = str(Decimal(str(row.get("total_cost_usd") or 0)).quantize(Decimal("0.000001")))
    return row


def _date_window(days: int) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end_at = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=timezone.utc)
    start_at = end_at - timedelta(days=days)
    return start_at, end_at
