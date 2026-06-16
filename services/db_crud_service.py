"""Schema-aware CRUD helpers for Supabase tables."""

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from psycopg2 import sql
from psycopg2.extras import Json

from core.database import database_target, get_connection


@dataclass(frozen=True)
class TableMeta:
    name: str
    primary_key: str
    columns: tuple[str, ...]
    jsonb_columns: tuple[str, ...] = ()


TABLES: tuple[TableMeta, ...] = (
    TableMeta("roles", "id", ("id", "code", "name", "created_at")),
    TableMeta(
        "plans",
        "id",
        ("id", "code", "name", "monthly_price", "quota_total", "rate_limit_per_minute", "created_at"),
    ),
    TableMeta(
        "users",
        "id",
        (
            "id",
            "external_auth_id",
            "auth_provider",
            "email",
            "password_hash",
            "full_name",
            "role_id",
            "status",
            "avatar_url",
            "last_active_at",
            "created_at",
            "updated_at",
        ),
    ),
    TableMeta(
        "user_profiles",
        "id",
        (
            "id",
            "user_id",
            "birth_year",
            "university",
            "target_position",
            "bio",
            "location",
            "github_url",
            "linkedin_url",
            "created_at",
            "updated_at",
        ),
    ),
    TableMeta(
        "user_subscriptions",
        "id",
        ("id", "user_id", "plan_id", "status", "start_at", "end_at", "auto_renew", "created_at"),
    ),
    TableMeta(
        "auth_sessions",
        "id",
        (
            "id",
            "user_id",
            "refresh_token_hash",
            "user_agent",
            "ip_address",
            "expires_at",
            "revoked_at",
            "created_at",
        ),
    ),
    TableMeta(
        "uploaded_files",
        "id",
        (
            "id",
            "user_id",
            "file_name",
            "file_type",
            "mime_type",
            "file_size_bytes",
            "storage_url",
            "extracted_text",
            "created_at",
        ),
    ),
    TableMeta(
        "cv_jd_analyses",
        "id",
        (
            "id",
            "user_id",
            "cv_file_id",
            "jd_file_id",
            "full_name",
            "target_position",
            "education_level",
            "tech_stack_json",
            "must_have_skills_json",
            "key_responsibilities_json",
            "s_skill",
            "s_exp",
            "s_edu",
            "s_sem",
            "ats_score",
            "jri_baseline",
            "d1_baseline",
            "d2_baseline",
            "d3_baseline",
            "d4_baseline",
            "d5_baseline",
            "llm_raw_json",
            "created_at",
        ),
        ("tech_stack_json", "must_have_skills_json", "key_responsibilities_json", "llm_raw_json"),
    ),
    TableMeta(
        "cv_jd_gap_items",
        "id",
        ("id", "analysis_id", "category", "skill", "status", "cv_evidence", "jd_requirement", "score"),
    ),
    TableMeta(
        "cv_jd_readiness_signals",
        "id",
        (
            "id",
            "analysis_id",
            "source_phase",
            "signal_key",
            "signal_label",
            "dimension",
            "status",
            "score",
            "cv_evidence",
            "jd_requirement",
            "created_at",
        ),
    ),
    TableMeta(
        "interview_sessions",
        "id",
        (
            "id",
            "user_id",
            "cv_jd_analysis_id",
            "position",
            "company",
            "session_type",
            "status",
            "scheduled_at",
            "started_at",
            "ended_at",
            "duration_seconds",
            "overall_score",
            "candidate_json",
            "job_description",
            "language",
            "voice",
            "blueprint_json",
            "context_json",
            "created_at",
            "updated_at",
        ),
        ("candidate_json", "blueprint_json", "context_json"),
    ),
    TableMeta(
        "interview_qa_pairs",
        "id",
        (
            "id",
            "session_id",
            "question_order",
            "question",
            "answer",
            "answer_timestamp_sec",
            "score",
            "strengths_json",
            "weaknesses_json",
            "optimal_answer",
            "transcript_events_json",
            "created_at",
        ),
        ("strengths_json", "weaknesses_json", "transcript_events_json"),
    ),
    TableMeta(
        "interview_scores",
        "id",
        (
            "id",
            "session_id",
            "d1_technical",
            "d2_communication",
            "d3_problem_solving",
            "d4_attitude",
            "d5_cultural_fit",
            "phase2_signals_json",
            "final_dimensions_json",
            "created_at",
        ),
        ("phase2_signals_json", "final_dimensions_json"),
    ),
    TableMeta(
        "interview_transcript_events",
        "id",
        (
            "id",
            "session_id",
            "event_order",
            "role",
            "text",
            "source",
            "timestamp_sec",
            "question_order",
            "created_at",
        ),
    ),
    TableMeta(
        "interview_recordings",
        "id",
        ("id", "session_id", "storage_url", "file_size_bytes", "duration_seconds", "transcript_text", "created_at"),
    ),
    TableMeta(
        "chat_conversations",
        "id",
        ("id", "user_id", "channel", "title", "created_at", "updated_at"),
    ),
    TableMeta(
        "chat_messages",
        "id",
        ("id", "conversation_id", "role", "content", "token_count", "created_at"),
    ),
    TableMeta(
        "user_skills",
        "id",
        ("id", "user_id", "skill_name", "progress_percent", "stage", "trend", "updated_at"),
    ),
    TableMeta(
        "job_applications",
        "id",
        ("id", "user_id", "job_title", "company", "status", "applied_at", "updated_at"),
    ),
    TableMeta(
        "learning_roadmaps",
        "id",
        ("id", "user_id", "target_role", "match_percent", "status", "created_at", "updated_at"),
    ),
    TableMeta(
        "roadmap_tasks",
        "id",
        (
            "id",
            "roadmap_id",
            "phase_name",
            "task_title",
            "task_type",
            "duration_minutes",
            "status",
            "display_order",
            "created_at",
            "updated_at",
        ),
    ),
    TableMeta(
        "activity_logs",
        "id",
        (
            "id",
            "actor_user_id",
            "target_user_id",
            "log_type",
            "action",
            "detail",
            "ip_address",
            "meta_json",
            "created_at",
        ),
        ("meta_json",),
    ),
    TableMeta(
        "api_usage_logs",
        "id",
        (
            "id",
            "occurred_at",
            "user_id",
            "external_auth_id",
            "provider",
            "model",
            "feature",
            "related_type",
            "related_id",
            "status",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "input_audio_seconds",
            "output_audio_seconds",
            "cost_usd",
            "duration_ms",
            "error_code",
            "meta_json",
        ),
        ("meta_json",),
    ),
    TableMeta(
        "user_activity_events",
        "id",
        (
            "id",
            "occurred_at",
            "user_id",
            "external_auth_id",
            "event_name",
            "object_type",
            "object_id",
            "source",
            "success",
            "duration_ms",
            "meta_json",
        ),
        ("meta_json",),
    ),
)

TABLE_BY_NAME = {table.name: table for table in TABLES}


def list_table_metadata() -> list[dict[str, Any]]:
    return [
        {
            "name": table.name,
            "primary_key": table.primary_key,
            "columns": list(table.columns),
            "jsonb_columns": list(table.jsonb_columns),
        }
        for table in TABLES
    ]


def check_database_connection() -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select
                      current_database() as database,
                      current_user as user_name,
                      version() as postgres_version
                    """
                )
                row = cur.fetchone()
    except Exception as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    return {"connected": True, "target": database_target(), "server": row}


def list_rows(table: TableMeta, limit: int, offset: int, order_by: str | None) -> dict[str, Any]:
    order_column = _validated_column(table, order_by or table.primary_key)
    query = sql.SQL("select * from {table} order by {order_by} limit %s offset %s").format(
        table=sql.Identifier(table.name),
        order_by=sql.Identifier(order_column),
    )
    return {"data": _fetch_all(query, (limit, offset))}


def get_row(table: TableMeta, item_id: str) -> dict[str, Any]:
    query = sql.SQL("select * from {table} where {pk} = %s").format(
        table=sql.Identifier(table.name),
        pk=sql.Identifier(table.primary_key),
    )
    row = _fetch_one(query, (item_id,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{table.name} row not found")
    return {"data": row}


def create_row(table: TableMeta, payload: dict[str, Any]) -> dict[str, Any]:
    values = _validated_payload(table, payload)
    if not values:
        query = sql.SQL("insert into {table} default values returning *").format(
            table=sql.Identifier(table.name),
        )
        row = _fetch_one(query, ())
        return {"data": row}

    columns = list(values)
    query = sql.SQL("insert into {table} ({columns}) values ({placeholders}) returning *").format(
        table=sql.Identifier(table.name),
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        placeholders=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    row = _fetch_one(query, tuple(values[column] for column in columns))
    return {"data": row}


def update_row(table: TableMeta, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    values = _validated_payload(table, payload)
    if table.primary_key in values:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Primary key cannot be updated")
    if not values:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Request body must include at least one column")

    columns = list(values)
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(column), sql.Placeholder()) for column in columns
    )
    query = sql.SQL("update {table} set {assignments} where {pk} = %s returning *").format(
        table=sql.Identifier(table.name),
        assignments=assignments,
        pk=sql.Identifier(table.primary_key),
    )
    row = _fetch_one(query, (*[values[column] for column in columns], item_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{table.name} row not found")
    return {"data": row}


def delete_row(table: TableMeta, item_id: str) -> dict[str, Any]:
    query = sql.SQL("delete from {table} where {pk} = %s returning *").format(
        table=sql.Identifier(table.name),
        pk=sql.Identifier(table.primary_key),
    )
    row = _fetch_one(query, (item_id,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{table.name} row not found")
    return {"data": row}


def _validated_column(table: TableMeta, column: str) -> str:
    if column not in table.columns:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Unknown column for {table.name}: {column}")
    return column


def _validated_payload(table: TableMeta, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Request body must be a JSON object")

    unknown = sorted(set(payload) - set(table.columns))
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown columns for {table.name}: {', '.join(unknown)}",
        )

    return {
        column: Json(value) if column in table.jsonb_columns and value is not None else value
        for column, value in payload.items()
    }


def _fetch_all(query: sql.Composable, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


def _fetch_one(query: sql.Composable, params: tuple[Any, ...]) -> dict[str, Any] | None:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
            conn.commit()
            return row
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
