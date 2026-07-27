"""Detailed, admin-only Excel workbook report generation."""

from __future__ import annotations

import csv
import io
import json

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from core.database import get_connection
from services.feedback_service import _ensure_feedback_schema


MAX_EXPORT_ROWS = 50_000

REPORT_LABELS = {
    "overview": "Overview",
    "users": "Users Management",
    "sessions": "Interview Sessions",
    "activity": "Activity Logs",
    "feedback": "Feedbacks",
    "ai_costs": "AI Costs",
}


def build_admin_report_workbook(
    *,
    pages: list[str],
    date_from: date | None,
    date_to: date | None,
    generated_by: str,
    overview_data: dict[str, Any] | None = None,
    plan_filter: str = "all",
) -> bytes:
    """Build one formatted XLSX workbook containing all selected report datasets."""

    start_at, end_at = _date_window(date_from, date_to)
    generated_at = datetime.now(timezone.utc)
    workbook = Workbook()
    workbook.remove(workbook.active)
    manifest: list[dict[str, Any]] = []

    def add_sheet(
        *,
        page: str,
        title: str,
        rows: list[dict[str, Any]],
        preferred_fields: Iterable[str] = (),
        truncated: bool = False,
        range_applied: bool = True,
    ) -> None:
        fields = _fieldnames(rows, preferred_fields)
        _write_worksheet(workbook, title=title, rows=rows, fields=fields)
        manifest.append(
            {
                "report_page": page,
                "report_label": REPORT_LABELS[page],
                "worksheet": title,
                "row_count": len(rows),
                "row_limit": MAX_EXPORT_ROWS if page != "overview" else "",
                "truncated": truncated,
                "date_range_applied": range_applied,
                "date_from": start_at.date().isoformat() if range_applied else "",
                "date_to": (end_at - timedelta(days=1)).date().isoformat() if range_applied else "",
                "plan_filter": plan_filter.title(),
                "generated_at_utc": generated_at.isoformat(),
                "generated_by": generated_by,
            }
        )

    if "overview" in pages and overview_data is not None:
        for filename, rows, preferred_fields, range_applied in _overview_files(overview_data):
            add_sheet(
                page="overview",
                title=_sheet_title(filename),
                rows=rows,
                preferred_fields=preferred_fields,
                range_applied=range_applied,
            )

    database_pages = {"users", "sessions", "activity", "feedback", "ai_costs"}.intersection(pages)
    if database_pages:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if "feedback" in database_pages or "users" in database_pages:
                    _ensure_feedback_schema(cur)

                if "users" in pages:
                    rows, truncated = _export_users(cur, plan_filter)
                    add_sheet(
                        page="users",
                        title="Users",
                        rows=rows,
                        preferred_fields=USER_FIELDS,
                        truncated=truncated,
                        range_applied=False,
                    )

                if "sessions" in pages:
                    rows, truncated = _export_sessions(cur, start_at, end_at)
                    add_sheet(
                        page="sessions",
                        title="Interview Sessions",
                        rows=rows,
                        preferred_fields=SESSION_FIELDS,
                        truncated=truncated,
                    )
                    qa_rows, qa_truncated = _export_session_questions(cur, start_at, end_at)
                    add_sheet(
                        page="sessions",
                        title="Session Questions",
                        rows=qa_rows,
                        preferred_fields=SESSION_QUESTION_FIELDS,
                        truncated=qa_truncated,
                    )

                if "activity" in pages:
                    rows, truncated = _export_activity(cur, start_at, end_at)
                    add_sheet(
                        page="activity",
                        title="Activity Logs",
                        rows=rows,
                        preferred_fields=ACTIVITY_FIELDS,
                        truncated=truncated,
                    )

                if "feedback" in pages:
                    user_rows, user_truncated = _export_user_feedback_summary(cur, plan_filter)
                    add_sheet(
                        page="feedback",
                        title="User Feedback Summary",
                        rows=user_rows,
                        preferred_fields=USER_FEEDBACK_FIELDS,
                        truncated=user_truncated,
                        range_applied=False,
                    )
                    rows, truncated = _export_feedback(cur, start_at, end_at, plan_filter)
                    add_sheet(
                        page="feedback",
                        title="Feedback Details",
                        rows=rows,
                        preferred_fields=FEEDBACK_FIELDS,
                        truncated=truncated,
                    )
                    report_rows, reports_truncated = _export_user_reports(
                        cur,
                        start_at,
                        end_at,
                        plan_filter,
                    )
                    add_sheet(
                        page="feedback",
                        title="User Reports",
                        rows=report_rows,
                        preferred_fields=USER_REPORT_FIELDS,
                        truncated=reports_truncated,
                    )

                if "ai_costs" in pages:
                    rows, truncated = _export_ai_usage(cur, start_at, end_at)
                    add_sheet(
                        page="ai_costs",
                        title="AI Usage Details",
                        rows=rows,
                        preferred_fields=AI_USAGE_FIELDS,
                        truncated=truncated,
                    )
                    for filename, aggregate_rows, preferred_fields in _ai_rollup_files(rows):
                        add_sheet(
                            page="ai_costs",
                            title=_sheet_title(filename),
                            rows=aggregate_rows,
                            preferred_fields=preferred_fields,
                            truncated=truncated,
                        )

    manifest_fields = [
        "report_page",
        "report_label",
        "worksheet",
        "row_count",
        "row_limit",
        "truncated",
        "date_range_applied",
        "date_from",
        "date_to",
        "plan_filter",
        "generated_at_utc",
        "generated_by",
    ]
    _write_worksheet(workbook, title="Export Guide", rows=manifest, fields=manifest_fields, index=0)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _sheet_title(filename: str) -> str:
    titles = {
        "overview/kpis.csv": "Overview KPIs",
        "overview/business-summary.csv": "Business Summary",
        "overview/paid-accounts-by-plan.csv": "Ever-Paid Accounts by Plan",
        "overview/report-feature-usage.csv": "Report Feature Usage",
        "overview/users-by-plan.csv": "Current Users by Plan",
        "overview/function-metrics.csv": "Function Metrics",
        "overview/landing-summary.csv": "Landing Summary",
        "overview/landing-by-source.csv": "Landing by Source",
        "overview/landing-trend.csv": "Landing Trend",
        "overview/feature-trend.csv": "Feature Trend",
        "overview/ai-cost-trend.csv": "AI Cost Trend",
        "overview/revenue-trend.csv": "Revenue Trend",
        "ai-costs/summary.csv": "AI Cost Summary",
        "ai-costs/daily-breakdown.csv": "AI Cost by Day",
        "ai-costs/feature-breakdown.csv": "AI Cost by Feature",
        "ai-costs/model-breakdown.csv": "AI Cost by Model",
    }
    return titles[filename]


def _write_worksheet(
    workbook: Workbook,
    *,
    title: str,
    rows: list[dict[str, Any]],
    fields: list[str],
    index: int | None = None,
) -> None:
    sheet = workbook.create_sheet(title=title, index=index)
    header_fill = PatternFill("solid", fgColor="1E3A5F")
    alternate_fill = PatternFill("solid", fgColor="F3F6FA")
    for column, field in enumerate(fields, start=1):
        cell = sheet.cell(row=1, column=column, value=_header_label(field))
        cell.fill = header_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    widths = [len(_header_label(field)) for field in fields]
    for row_number, row in enumerate(rows, start=2):
        for column, field in enumerate(fields, start=1):
            value = _excel_cell(row.get(field))
            cell = sheet.cell(row=row_number, column=column, value=value)
            cell.alignment = Alignment(vertical="top", wrap_text=_should_wrap(field, value))
            if row_number % 2 == 0:
                cell.fill = alternate_fill
            if isinstance(value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm:ss"
            elif isinstance(value, date):
                cell.number_format = "yyyy-mm-dd"
            elif "cost_usd" in field:
                cell.number_format = '$0.000000'
            elif field.endswith("_vnd"):
                cell.number_format = '#,##0 "VND"'
            widths[column - 1] = max(widths[column - 1], min(len(str(value or "")), 45))

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(fields))}{max(len(rows) + 1, 1)}"
    sheet.sheet_view.showGridLines = False
    sheet.row_dimensions[1].height = 32
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = min(max(width + 2, 11), 47)


def _header_label(field: str) -> str:
    initialisms = {"id": "ID", "ai": "AI", "api": "API", "cv": "CV", "jd": "JD", "usd": "USD", "vnd": "VND", "url": "URL", "json": "JSON", "utc": "UTC"}
    return " ".join(initialisms.get(part, part.capitalize()) for part in field.split("_"))


def _should_wrap(field: str, value: Any) -> bool:
    return any(token in field for token in ("comment", "detail", "description", "json", "answer", "question", "bio", "job_description")) or len(str(value or "")) > 80


def _excel_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
        if value.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
    return value


# Backwards-compatible import for callers that have not yet adopted the workbook name.
build_admin_report_archive = build_admin_report_workbook


def csv_bytes(rows: list[dict[str, Any]], fieldnames: list[str]) -> bytes:
    """Encode rows as Excel-friendly UTF-8 CSV while neutralizing formulas."""

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _csv_cell(row.get(field)) for field in fieldnames})
    return output.getvalue().encode("utf-8-sig")


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _fieldnames(rows: list[dict[str, Any]], preferred: Iterable[str]) -> list[str]:
    fields = list(preferred)
    known = set(fields)
    for row in rows:
        for key in row:
            if key not in known:
                known.add(key)
                fields.append(key)
    return fields or ["value"]


def _date_window(date_from: date | None, date_to: date | None) -> tuple[datetime, datetime]:
    today = datetime.now(timezone.utc).date()
    start_date = date_from or (today - timedelta(days=29))
    end_date = date_to or today
    return (
        datetime.combine(start_date, time.min, tzinfo=timezone.utc),
        datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc),
    )


def _fetch_limited(cur, query: str, params: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    cur.execute(query, [*params, MAX_EXPORT_ROWS + 1])
    rows = [dict(row) for row in cur.fetchall()]
    return rows[:MAX_EXPORT_ROWS], len(rows) > MAX_EXPORT_ROWS


def _overview_files(data: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]], list[str], bool]]:
    window = data.get("window") or {}
    window_values = {
        "date_from": window.get("date_from"),
        "date_to": window.get("date_to"),
        "group_by": window.get("group_by"),
    }
    kpi_rows = [
        {**window_values, "metric": key, "value": value}
        for key, value in (data.get("kpis") or {}).items()
    ]
    business = data.get("business_report") or {}
    business_summary = [
        {**window_values, "metric": key, "value": value}
        for key, value in business.items()
        if key not in {"scope", "ever_paid_accounts_by_plan", "feature_usage"}
    ]
    paid_accounts_by_plan = [
        {"scope": "all_time", **dict(row)}
        for row in business.get("ever_paid_accounts_by_plan") or []
    ]
    report_feature_usage = [
        {**window_values, **dict(row)}
        for row in business.get("feature_usage") or []
    ]
    users_by_plan = [
        {"scope": "current_at_export", **dict(row)}
        for row in data.get("users_by_plan") or []
    ]
    function_metrics = [{**window_values, **dict(row)} for row in data.get("function_metrics") or []]
    landing = data.get("landing_metrics") or {}
    landing_summary = [
        {**window_values, "metric": key, "value": value}
        for key, value in (landing.get("summary") or {}).items()
    ]
    landing_sources = [{**window_values, **dict(row)} for row in landing.get("by_source") or []]
    landing_trend = [{**window_values, **dict(row)} for row in landing.get("trend") or []]
    charts = data.get("charts") or {}
    feature_trend = [{**window_values, **dict(row)} for row in charts.get("feature_trend") or []]
    ai_cost_trend = [{**window_values, **dict(row)} for row in charts.get("ai_cost_by_day") or []]
    revenue_trend = [{**window_values, **dict(row)} for row in (charts.get("revenue") or charts.get("revenue_by_month") or [])]

    return [
        ("overview/kpis.csv", kpi_rows, ["date_from", "date_to", "group_by", "metric", "value"], True),
        ("overview/business-summary.csv", business_summary, ["date_from", "date_to", "group_by", "metric", "value"], True),
        ("overview/paid-accounts-by-plan.csv", paid_accounts_by_plan, ["scope", "plan_code", "plan_name", "paid_accounts"], False),
        ("overview/report-feature-usage.csv", report_feature_usage, ["date_from", "date_to", "group_by", "feature_key", "label", "total_uses"], True),
        ("overview/users-by-plan.csv", users_by_plan, ["scope", "plan_code", "plan_name", "users", "is_paid", "monthly_revenue"], False),
        ("overview/function-metrics.csv", function_metrics, ["date_from", "date_to", "group_by", "feature_key", "label", "description", "kind", "events", "active_users", "ai_calls", "completions", "total_tokens", "total_cost_usd"], True),
        ("overview/landing-summary.csv", landing_summary, ["date_from", "date_to", "group_by", "metric", "value"], True),
        ("overview/landing-by-source.csv", landing_sources, ["date_from", "date_to", "group_by", "source", "landing_views", "pricing_clicks", "signup_clicks", "login_clicks", "unique_visitors"], True),
        ("overview/landing-trend.csv", landing_trend, ["date_from", "date_to", "group_by", "period"], True),
        ("overview/feature-trend.csv", feature_trend, ["date_from", "date_to", "group_by", "period"], True),
        ("overview/ai-cost-trend.csv", ai_cost_trend, ["date_from", "date_to", "group_by", "period", "total_cost_usd"], True),
        ("overview/revenue-trend.csv", revenue_trend, ["date_from", "date_to", "group_by", "period", "revenue"], True),
    ]


USER_FIELDS = [
    "user_id", "external_auth_id", "auth_provider", "email", "full_name", "role_code", "status",
    "avatar_url", "birth_year", "university", "target_position", "bio", "location", "github_url",
    "linkedin_url", "plan_category", "plan_code", "plan_name", "subscription_status", "subscription_start_at",
    "subscription_end_at", "subscription_auto_renew", "subscription_provider", "plan_quota_total",
    "plan_rate_limit_per_minute", "total_sessions", "completed_sessions", "average_session_score",
    "cv_jd_analyses", "feedback_count", "average_feedback_rating", "ai_calls", "prompt_tokens",
    "completion_tokens", "total_tokens", "total_ai_cost_usd", "activity_events", "payment_attempts",
    "paid_transactions", "pending_transactions", "failed_transactions", "usage_based_purchases",
    "total_paid_vnd", "last_paid_at", "terms_accepted_at", "terms_version", "privacy_policy_accepted_at",
    "privacy_policy_version", "last_active_at", "created_at", "updated_at",
]


def _export_users(cur, plan_filter: str = "all") -> tuple[list[dict[str, Any]], bool]:
    return _fetch_limited(
        cur,
        """
        select
          u.id::text as user_id,
          u.external_auth_id,
          u.auth_provider,
          u.email,
          u.full_name,
          r.code as role_code,
          u.status,
          u.avatar_url,
          up.birth_year,
          up.university,
          up.target_position,
          up.bio,
          up.location,
          up.github_url,
          up.linkedin_url,
          case
            when subscription.plan_code is not null
              and subscription.plan_code <> 'free'
              and subscription.subscription_is_current
            then 'Paid'
            else 'Free'
          end as plan_category,
          coalesce(subscription.plan_code, 'free') as plan_code,
          coalesce(subscription.plan_name, 'Free') as plan_name,
          subscription.subscription_status,
          subscription.start_at as subscription_start_at,
          subscription.end_at as subscription_end_at,
          subscription.auto_renew as subscription_auto_renew,
          subscription.provider as subscription_provider,
          coalesce(subscription.quota_total, 0)::int as plan_quota_total,
          coalesce(subscription.rate_limit_per_minute, 0)::int as plan_rate_limit_per_minute,
          coalesce(sessions.total_sessions, 0)::int as total_sessions,
          coalesce(sessions.completed_sessions, 0)::int as completed_sessions,
          sessions.average_session_score,
          coalesce(analyses.cv_jd_analyses, 0)::int as cv_jd_analyses,
          coalesce(feedback.feedback_count, 0)::int as feedback_count,
          feedback.average_feedback_rating,
          coalesce(usage.ai_calls, 0)::int as ai_calls,
          coalesce(usage.prompt_tokens, 0)::int as prompt_tokens,
          coalesce(usage.completion_tokens, 0)::int as completion_tokens,
          coalesce(usage.total_tokens, 0)::int as total_tokens,
          coalesce(usage.total_ai_cost_usd, 0) as total_ai_cost_usd,
          coalesce(activity.activity_events, 0)::int as activity_events,
          coalesce(payments.payment_attempts, 0)::int as payment_attempts,
          coalesce(payments.paid_transactions, 0)::int as paid_transactions,
          coalesce(payments.pending_transactions, 0)::int as pending_transactions,
          coalesce(payments.failed_transactions, 0)::int as failed_transactions,
          coalesce(payments.usage_based_purchases, 0)::int as usage_based_purchases,
          coalesce(payments.total_paid_vnd, 0)::bigint as total_paid_vnd,
          payments.last_paid_at,
          u.terms_accepted_at,
          u.terms_version,
          u.privacy_policy_accepted_at,
          u.privacy_policy_version,
          u.last_active_at,
          u.created_at,
          u.updated_at
        from users u
        left join roles r on r.id = u.role_id
        left join user_profiles up on up.user_id = u.id
        left join lateral (
          select
            p.code as plan_code,
            p.name as plan_name,
            p.quota_total,
            p.rate_limit_per_minute,
            us.status as subscription_status,
            (
              us.status = 'active'
              and us.start_at <= now()
              and (us.end_at is null or us.end_at >= now())
            ) as subscription_is_current,
            us.start_at,
            us.end_at,
            us.auto_renew,
            us.provider
          from user_subscriptions us
          join plans p on p.id = us.plan_id
          where us.user_id = u.id
          order by
            (
              us.status = 'active'
              and us.start_at <= now()
              and (us.end_at is null or us.end_at >= now())
            ) desc,
            case p.code
              when 'team' then 500
              when 'pro' then 400
              when 'standard' then 300
              when 'usage_based' then 200
              when 'free' then 100
              else 0
            end desc,
            us.start_at desc,
            us.id desc
          limit 1
        ) subscription on true
        left join lateral (
          select
            count(*)::int as total_sessions,
            count(*) filter (where status = 'completed')::int as completed_sessions,
            round(avg(overall_score) filter (where overall_score is not null), 2) as average_session_score
          from interview_sessions s where s.user_id = u.id
        ) sessions on true
        left join lateral (
          select count(*)::int as cv_jd_analyses from cv_jd_analyses a where a.user_id = u.id
        ) analyses on true
        left join lateral (
          select count(*)::int as feedback_count, round(avg(rating), 2) as average_feedback_rating
          from session_feedback f where f.user_id = u.id
        ) feedback on true
        left join lateral (
          select
            count(*)::int as ai_calls,
            coalesce(sum(prompt_tokens), 0)::int as prompt_tokens,
            coalesce(sum(completion_tokens), 0)::int as completion_tokens,
            coalesce(sum(total_tokens), 0)::int as total_tokens,
            coalesce(sum(cost_usd), 0) as total_ai_cost_usd
          from api_usage_logs a
          where a.user_id = u.id or (u.external_auth_id is not null and a.external_auth_id = u.external_auth_id)
        ) usage on true
        left join lateral (
          select count(*)::int as activity_events
          from user_activity_events e
          where e.user_id = u.id or (u.external_auth_id is not null and e.external_auth_id = u.external_auth_id)
        ) activity on true
        left join lateral (
          select
            count(*)::int as payment_attempts,
            count(*) filter (where pt.status = 'paid')::int as paid_transactions,
            count(*) filter (where pt.status = 'pending')::int as pending_transactions,
            count(*) filter (where pt.status in ('failed', 'cancelled', 'expired'))::int as failed_transactions,
            count(*) filter (where pt.status = 'paid' and plan.code = 'usage_based')::int as usage_based_purchases,
            coalesce(sum(pt.amount_vnd) filter (where pt.status = 'paid'), 0)::bigint as total_paid_vnd,
            max(pt.paid_at) filter (where pt.status = 'paid') as last_paid_at
          from payment_transactions pt
          join plans plan on plan.id = pt.plan_id
          where pt.user_id = u.id
        ) payments on true
        where %s = 'all'
          or case
            when subscription.plan_code is not null
              and subscription.plan_code <> 'free'
              and subscription.subscription_is_current
            then 'paid'
            else 'free'
          end = %s
        order by u.created_at desc, u.email asc
        limit %s
        """,
        [plan_filter, plan_filter],
    )


SESSION_FIELDS = [
    "session_id", "user_id", "external_auth_id", "email", "full_name", "position", "company",
    "session_type", "status", "language", "voice", "cv_jd_analysis_id", "scheduled_at", "started_at",
    "ended_at", "duration_seconds", "overall_score", "question_count", "answered_question_count",
    "average_question_score", "feedback_count", "average_feedback_rating", "job_description",
    "candidate_json", "blueprint_json", "context_json", "created_at", "updated_at",
]


def _export_sessions(cur, start_at: datetime, end_at: datetime) -> tuple[list[dict[str, Any]], bool]:
    return _fetch_limited(
        cur,
        """
        select
          s.id::text as session_id,
          s.user_id::text as user_id,
          u.external_auth_id,
          u.email,
          u.full_name,
          s.position,
          s.company,
          s.session_type,
          s.status,
          s.language,
          s.voice,
          s.cv_jd_analysis_id::text as cv_jd_analysis_id,
          s.scheduled_at,
          s.started_at,
          s.ended_at,
          s.duration_seconds,
          s.overall_score,
          coalesce(qa.question_count, 0)::int as question_count,
          coalesce(qa.answered_question_count, 0)::int as answered_question_count,
          qa.average_question_score,
          coalesce(feedback.feedback_count, 0)::int as feedback_count,
          feedback.average_feedback_rating,
          s.job_description,
          s.candidate_json,
          s.blueprint_json,
          s.context_json,
          s.created_at,
          s.updated_at
        from interview_sessions s
        join users u on u.id = s.user_id
        left join lateral (
          select
            count(*)::int as question_count,
            count(*) filter (where nullif(trim(coalesce(answer, '')), '') is not null)::int as answered_question_count,
            round(avg(score) filter (where score is not null), 2) as average_question_score
          from interview_qa_pairs q where q.session_id = s.id
        ) qa on true
        left join lateral (
          select count(*)::int as feedback_count, round(avg(rating), 2) as average_feedback_rating
          from session_feedback f where f.session_id = s.id
        ) feedback on true
        where coalesce(s.started_at, s.scheduled_at, s.created_at) >= %s
          and coalesce(s.started_at, s.scheduled_at, s.created_at) < %s
        order by coalesce(s.started_at, s.scheduled_at, s.created_at) desc, s.id desc
        limit %s
        """,
        [start_at, end_at],
    )


SESSION_QUESTION_FIELDS = [
    "session_id", "user_id", "email", "full_name", "position", "company", "session_type",
    "session_status", "question_id", "question_order", "question", "answer", "raw_transcript",
    "polished_transcript", "answer_timestamp_sec", "score", "feedback", "strengths_json",
    "weaknesses_json", "optimal_answer", "score_json", "transcript_events_json", "created_at",
]


def _export_session_questions(cur, start_at: datetime, end_at: datetime) -> tuple[list[dict[str, Any]], bool]:
    return _fetch_limited(
        cur,
        """
        select
          s.id::text as session_id,
          s.user_id::text as user_id,
          u.email,
          u.full_name,
          s.position,
          s.company,
          s.session_type,
          s.status as session_status,
          q.id as question_id,
          q.question_order,
          q.question,
          q.answer,
          q.raw_transcript,
          q.polished_transcript,
          q.answer_timestamp_sec,
          q.score,
          q.feedback,
          q.strengths_json,
          q.weaknesses_json,
          q.optimal_answer,
          q.score_json,
          q.transcript_events_json,
          q.created_at
        from interview_qa_pairs q
        join interview_sessions s on s.id = q.session_id
        join users u on u.id = s.user_id
        where coalesce(s.started_at, s.scheduled_at, s.created_at) >= %s
          and coalesce(s.started_at, s.scheduled_at, s.created_at) < %s
        order by coalesce(s.started_at, s.scheduled_at, s.created_at) desc, s.id desc, q.question_order asc
        limit %s
        """,
        [start_at, end_at],
    )


ACTIVITY_FIELDS = [
    "id", "created_at", "source", "log_type", "event_name", "action", "detail", "success",
    "duration_ms", "ip_address", "actor_user_id", "actor_external_auth_id", "actor_email",
    "actor_full_name", "target_user_id", "target_email", "target_full_name", "object_type", "object_id",
    "meta_json",
]


def _export_activity(cur, start_at: datetime, end_at: datetime) -> tuple[list[dict[str, Any]], bool]:
    return _fetch_limited(
        cur,
        """
        select * from (
          select
            'activity_log:' || l.id::text as id,
            l.created_at,
            'activity_logs'::text as source,
            l.log_type,
            null::text as event_name,
            l.action,
            l.detail,
            null::boolean as success,
            null::int as duration_ms,
            l.ip_address,
            actor.id::text as actor_user_id,
            actor.external_auth_id as actor_external_auth_id,
            actor.email as actor_email,
            actor.full_name as actor_full_name,
            target.id::text as target_user_id,
            target.email as target_email,
            target.full_name as target_full_name,
            null::text as object_type,
            null::text as object_id,
            l.meta_json
          from activity_logs l
          left join users actor on actor.id = l.actor_user_id
          left join users target on target.id = l.target_user_id

          union all

          select
            'user_event:' || e.id::text as id,
            e.occurred_at as created_at,
            'user_activity_events'::text as source,
            case
              when e.event_name in ('interview_started', 'interview_completed', 'realtime_token_created', 'report_saved', 'coding_plan_generated') then 'session'
              when e.event_name in ('file_parse', 'cvjd_saved', 'cvjd_cache_hit') then 'account'
              else 'system'
            end as log_type,
            e.event_name,
            e.event_name as action,
            coalesce(e.object_type, e.event_name) as detail,
            e.success,
            e.duration_ms,
            null::varchar(64) as ip_address,
            u.id::text as actor_user_id,
            e.external_auth_id as actor_external_auth_id,
            u.email as actor_email,
            u.full_name as actor_full_name,
            null::text as target_user_id,
            null::text as target_email,
            null::text as target_full_name,
            e.object_type,
            e.object_id::text as object_id,
            e.meta_json
          from user_activity_events e
          left join users u on u.id = e.user_id

          union all

          select
            'payment:' || pt.id::text as id,
            coalesce(pt.paid_at, pt.cancelled_at, pt.updated_at, pt.created_at) as created_at,
            'payment_transactions'::text as source,
            'payment'::text as log_type,
            ('payment_' || pt.status)::text as event_name,
            case
              when pt.status = 'paid' then 'Payment completed'
              when pt.status = 'pending' then 'Payment initiated'
              when pt.status = 'cancelled' then 'Payment cancelled'
              when pt.status = 'expired' then 'Payment expired'
              else 'Payment failed'
            end as action,
            concat(p.name, ' · ', pt.amount_vnd, ' ', pt.currency, ' · ', pt.status) as detail,
            (pt.status = 'paid') as success,
            null::int as duration_ms,
            null::varchar(64) as ip_address,
            u.id::text as actor_user_id,
            u.external_auth_id as actor_external_auth_id,
            u.email as actor_email,
            u.full_name as actor_full_name,
            null::text as target_user_id,
            null::text as target_email,
            null::text as target_full_name,
            'payment'::text as object_type,
            pt.id::text as object_id,
            jsonb_build_object(
              'order_code', pt.order_code,
              'plan_code', p.code,
              'amount_vnd', pt.amount_vnd,
              'provider_status', pt.status
            ) as meta_json
          from payment_transactions pt
          join users u on u.id = pt.user_id
          join plans p on p.id = pt.plan_id
        ) activity
        where activity.created_at >= %s and activity.created_at < %s
        order by activity.created_at desc, activity.id desc
        limit %s
        """,
        [start_at, end_at],
    )


FEEDBACK_FIELDS = [
    "feedback_id", "session_id", "user_id", "external_auth_id", "email", "full_name", "source",
    "plan_category", "plan_code", "plan_name", "subscription_status", "emotion", "rating", "is_negative",
    "comment", "tags_json", "position", "company", "session_type",
    "session_status", "session_duration_seconds", "session_overall_score", "feedback_created_at",
    "feedback_updated_at", "session_created_at",
]

USER_REPORT_FIELDS = [
    "report_id", "user_id", "external_auth_id", "email", "full_name", "kind", "feature", "path",
    "comment", "status", "plan_category", "plan_code", "plan_name", "subscription_status",
    "report_created_at", "report_updated_at",
]


USER_FEEDBACK_FIELDS = [
    "user_id", "external_auth_id", "email", "full_name", "user_status", "plan_category", "plan_code",
    "plan_name", "subscription_status", "feedback_count", "average_rating", "negative_feedback_count",
    "last_feedback_at", "latest_source", "latest_emotion", "latest_rating", "latest_comment",
    "latest_tags_json", "latest_session_id", "latest_position", "latest_company", "total_sessions",
    "completed_sessions", "user_created_at", "last_active_at",
]


def _export_user_feedback_summary(cur, plan_filter: str = "all") -> tuple[list[dict[str, Any]], bool]:
    """Return one row for every user, including users who have never submitted feedback."""

    return _fetch_limited(
        cur,
        """
        select
          u.id::text as user_id,
          u.external_auth_id,
          u.email,
          u.full_name,
          u.status as user_status,
          case
            when subscription.plan_code is not null
              and subscription.plan_code <> 'free'
              and subscription.subscription_is_current
            then 'Paid'
            else 'Free'
          end as plan_category,
          coalesce(subscription.plan_code, 'free') as plan_code,
          coalesce(subscription.plan_name, 'Free') as plan_name,
          subscription.subscription_status,
          coalesce(feedback.feedback_count, 0)::int as feedback_count,
          feedback.average_rating,
          coalesce(feedback.negative_feedback_count, 0)::int as negative_feedback_count,
          feedback.last_feedback_at,
          latest.source as latest_source,
          latest.emotion as latest_emotion,
          latest.rating as latest_rating,
          latest.comment as latest_comment,
          latest.tags_json as latest_tags_json,
          latest.session_id::text as latest_session_id,
          latest.position as latest_position,
          latest.company as latest_company,
          coalesce(sessions.total_sessions, 0)::int as total_sessions,
          coalesce(sessions.completed_sessions, 0)::int as completed_sessions,
          u.created_at as user_created_at,
          u.last_active_at
        from users u
        left join lateral (
          select
            p.code as plan_code,
            p.name as plan_name,
            us.status as subscription_status,
            (
              us.status = 'active'
              and us.start_at <= now()
              and (us.end_at is null or us.end_at >= now())
            ) as subscription_is_current
          from user_subscriptions us
          join plans p on p.id = us.plan_id
          where us.user_id = u.id
          order by
            (
              us.status = 'active'
              and us.start_at <= now()
              and (us.end_at is null or us.end_at >= now())
            ) desc,
            case p.code
              when 'team' then 500
              when 'pro' then 400
              when 'standard' then 300
              when 'usage_based' then 200
              when 'free' then 100
              else 0
            end desc,
            us.start_at desc,
            us.id desc
          limit 1
        ) subscription on true
        left join lateral (
          select
            count(*)::int as feedback_count,
            round(avg(f.rating), 2) as average_rating,
            count(*) filter (where f.emotion in ('very_bad', 'bad') or f.rating <= 2)::int as negative_feedback_count,
            max(coalesce(f.updated_at, f.created_at)) as last_feedback_at
          from session_feedback f
          where f.user_id = u.id
        ) feedback on true
        left join lateral (
          select f.source, f.emotion, f.rating, f.comment, f.tags_json, f.session_id, s.position, s.company
          from session_feedback f
          join interview_sessions s on s.id = f.session_id
          where f.user_id = u.id
          order by coalesce(f.updated_at, f.created_at) desc, f.id desc
          limit 1
        ) latest on true
        left join lateral (
          select
            count(*)::int as total_sessions,
            count(*) filter (where s.status = 'completed')::int as completed_sessions
          from interview_sessions s
          where s.user_id = u.id
        ) sessions on true
        where %s = 'all'
          or case
            when subscription.plan_code is not null
              and subscription.plan_code <> 'free'
              and subscription.subscription_is_current
            then 'paid'
            else 'free'
          end = %s
        order by feedback.last_feedback_at desc nulls last, u.created_at desc, u.email asc
        limit %s
        """,
        [plan_filter, plan_filter],
    )


def _export_feedback(
    cur,
    start_at: datetime,
    end_at: datetime,
    plan_filter: str = "all",
) -> tuple[list[dict[str, Any]], bool]:
    rows, truncated = _fetch_limited(
        cur,
        """
        select
          f.id::text as feedback_id,
          f.session_id::text as session_id,
          f.user_id::text as user_id,
          u.external_auth_id,
          u.email,
          u.full_name,
          f.source,
          case
            when subscription.plan_code is not null
              and subscription.plan_code <> 'free'
              and subscription.subscription_is_current
            then 'Paid'
            else 'Free'
          end as plan_category,
          coalesce(subscription.plan_code, 'free') as plan_code,
          coalesce(subscription.plan_name, 'Free') as plan_name,
          subscription.subscription_status,
          f.emotion,
          f.rating,
          (f.emotion in ('very_bad', 'bad') or f.rating <= 2) as is_negative,
          f.comment,
          f.tags_json,
          s.position,
          s.company,
          s.session_type,
          s.status as session_status,
          s.duration_seconds as session_duration_seconds,
          s.overall_score as session_overall_score,
          f.created_at as feedback_created_at,
          f.updated_at as feedback_updated_at,
          s.created_at as session_created_at
        from session_feedback f
        join interview_sessions s on s.id = f.session_id
        join users u on u.id = f.user_id
        left join lateral (
          select
            p.code as plan_code,
            p.name as plan_name,
            us.status as subscription_status,
            (
              us.status = 'active'
              and us.start_at <= now()
              and (us.end_at is null or us.end_at >= now())
            ) as subscription_is_current
          from user_subscriptions us
          join plans p on p.id = us.plan_id
          where us.user_id = u.id
          order by
            (
              us.status = 'active'
              and us.start_at <= now()
              and (us.end_at is null or us.end_at >= now())
            ) desc,
            case p.code
              when 'team' then 500
              when 'pro' then 400
              when 'standard' then 300
              when 'usage_based' then 200
              when 'free' then 100
              else 0
            end desc,
            us.start_at desc,
            us.id desc
          limit 1
        ) subscription on true
        where coalesce(f.updated_at, f.created_at) >= %s
          and coalesce(f.updated_at, f.created_at) < %s
          and (
            %s = 'all'
            or case
              when subscription.plan_code is not null
                and subscription.plan_code <> 'free'
                and subscription.subscription_is_current
              then 'paid'
              else 'free'
            end = %s
          )
        order by coalesce(f.updated_at, f.created_at) desc, f.id desc
        limit %s
        """,
        [start_at, end_at, plan_filter, plan_filter],
    )
    return rows, truncated


def _export_user_reports(
    cur,
    start_at: datetime,
    end_at: datetime,
    plan_filter: str = "all",
) -> tuple[list[dict[str, Any]], bool]:
    return _fetch_limited(
        cur,
        """
        select
          r.id::text as report_id,
          r.user_id::text as user_id,
          u.external_auth_id,
          u.email,
          u.full_name,
          r.kind,
          r.feature,
          r.path,
          r.comment,
          r.status,
          case
            when subscription.plan_code is not null
              and subscription.plan_code <> 'free'
              and subscription.subscription_is_current
            then 'Paid'
            else 'Free'
          end as plan_category,
          coalesce(subscription.plan_code, 'free') as plan_code,
          coalesce(subscription.plan_name, 'Free') as plan_name,
          subscription.subscription_status,
          r.created_at as report_created_at,
          r.updated_at as report_updated_at
        from user_reports r
        join users u on u.id = r.user_id
        left join lateral (
          select
            p.code as plan_code,
            p.name as plan_name,
            us.status as subscription_status,
            (
              us.status = 'active'
              and us.start_at <= now()
              and (us.end_at is null or us.end_at >= now())
            ) as subscription_is_current
          from user_subscriptions us
          join plans p on p.id = us.plan_id
          where us.user_id = u.id
          order by
            (
              us.status = 'active'
              and us.start_at <= now()
              and (us.end_at is null or us.end_at >= now())
            ) desc,
            case p.code
              when 'team' then 500
              when 'pro' then 400
              when 'standard' then 300
              when 'usage_based' then 200
              when 'free' then 100
              else 0
            end desc,
            us.start_at desc,
            us.id desc
          limit 1
        ) subscription on true
        where r.created_at >= %s
          and r.created_at < %s
          and (
            %s = 'all'
            or case
              when subscription.plan_code is not null
                and subscription.plan_code <> 'free'
                and subscription.subscription_is_current
              then 'paid'
              else 'free'
            end = %s
          )
        order by r.created_at desc, r.id desc
        limit %s
        """,
        [start_at, end_at, plan_filter, plan_filter],
    )


AI_USAGE_FIELDS = [
    "usage_id", "occurred_at", "user_id", "external_auth_id", "email", "full_name", "provider",
    "model", "feature", "related_type", "related_id", "status", "prompt_tokens", "completion_tokens",
    "total_tokens", "input_audio_seconds", "output_audio_seconds", "cost_usd", "duration_ms", "error_code",
    "meta_json",
]


def _export_ai_usage(cur, start_at: datetime, end_at: datetime) -> tuple[list[dict[str, Any]], bool]:
    return _fetch_limited(
        cur,
        """
        select
          l.id as usage_id,
          l.occurred_at,
          l.user_id::text as user_id,
          l.external_auth_id,
          u.email,
          u.full_name,
          l.provider,
          l.model,
          l.feature,
          l.related_type,
          l.related_id::text as related_id,
          l.status,
          l.prompt_tokens,
          l.completion_tokens,
          l.total_tokens,
          l.input_audio_seconds,
          l.output_audio_seconds,
          l.cost_usd,
          l.duration_ms,
          l.error_code,
          l.meta_json
        from api_usage_logs l
        left join users u on u.id = l.user_id
        where l.occurred_at >= %s and l.occurred_at < %s
        order by l.occurred_at desc, l.id desc
        limit %s
        """,
        [start_at, end_at],
    )


AI_ROLLUP_FIELDS = [
    "group", "ai_calls", "success_count", "error_count", "cancelled_count", "blocked_count",
    "prompt_tokens", "completion_tokens", "total_tokens", "input_audio_seconds", "output_audio_seconds",
    "total_cost_usd", "average_duration_ms",
]


def _ai_rollup_files(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]], list[str]]]:
    overall = _aggregate_ai(rows, lambda _: "all")
    daily = _aggregate_ai(rows, lambda row: str(row.get("occurred_at") or "")[:10])
    by_feature = _aggregate_ai(rows, lambda row: str(row.get("feature") or "unknown"))
    by_model = _aggregate_ai(rows, lambda row: str(row.get("model") or "unknown"))
    return [
        ("ai-costs/summary.csv", overall, AI_ROLLUP_FIELDS),
        ("ai-costs/daily-breakdown.csv", daily, AI_ROLLUP_FIELDS),
        ("ai-costs/feature-breakdown.csv", by_feature, AI_ROLLUP_FIELDS),
        ("ai-costs/model-breakdown.csv", by_model, AI_ROLLUP_FIELDS),
    ]


def _aggregate_ai(rows: list[dict[str, Any]], group_key) -> list[dict[str, Any]]:
    aggregates: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "ai_calls": 0,
            "success_count": 0,
            "error_count": 0,
            "cancelled_count": 0,
            "blocked_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "input_audio_seconds": 0,
            "output_audio_seconds": 0,
            "total_cost_usd": Decimal("0"),
            "duration_total_ms": 0,
            "duration_samples": 0,
        }
    )
    for row in rows:
        key = group_key(row)
        item = aggregates[key]
        item["ai_calls"] += 1
        status = str(row.get("status") or "")
        status_key = f"{status}_count"
        if status_key in item:
            item[status_key] += 1
        item["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
        item["completion_tokens"] += int(row.get("completion_tokens") or 0)
        item["total_tokens"] += int(row.get("total_tokens") or 0)
        item["input_audio_seconds"] += int(row.get("input_audio_seconds") or 0)
        item["output_audio_seconds"] += int(row.get("output_audio_seconds") or 0)
        item["total_cost_usd"] += Decimal(str(row.get("cost_usd") or 0))
        if row.get("duration_ms") is not None:
            item["duration_total_ms"] += int(row["duration_ms"])
            item["duration_samples"] += 1

    output: list[dict[str, Any]] = []
    for key in sorted(aggregates):
        item = aggregates[key]
        samples = item.pop("duration_samples")
        duration_total = item.pop("duration_total_ms")
        output.append(
            {
                "group": key,
                **item,
                "total_cost_usd": format(item["total_cost_usd"], ".6f"),
                "average_duration_ms": round(duration_total / samples, 2) if samples else "",
            }
        )
    if not output:
        output.append({field: ("all" if field == "group" else 0) for field in AI_ROLLUP_FIELDS})
    return output
