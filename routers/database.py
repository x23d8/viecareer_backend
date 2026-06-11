"""Compact Swagger CRUD endpoints for Supabase database tables.

These endpoints are restricted to authenticated users only.
For full admin-only access control, add a role check against the database
(e.g., check users.role = 'admin') once a role system is established.
"""

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, status

from core.clerk_auth import ClerkUser, require_auth
from core.rate_limit import limiter
from services.db_crud_service import (
    TABLE_BY_NAME,
    TableMeta,
    check_database_connection,
    create_row,
    delete_row,
    get_row,
    list_rows,
    list_table_metadata,
    update_row,
)

router = APIRouter(prefix="/api/db", tags=["database"])


def _table_from_path(
    table_name: str = Path(
        ...,
        description="Whitelisted table name. Call /api/db/_tables to see available tables.",
        examples=["plans"],
    ),
) -> TableMeta:
    table = TABLE_BY_NAME.get(table_name)
    if table is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown table: {table_name}")
    return table


@router.get("/_health", summary="Check Supabase pooler connection")
@limiter.limit("30/minute")
def db_health(
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
):
    return check_database_connection()


@router.get("/_tables", summary="List configured database tables")
@limiter.limit("30/minute")
def db_tables(
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
):
    return {"data": list_table_metadata()}


@router.get("/{table_name}", summary="List rows from a table")
@limiter.limit("60/minute")
def list_table_rows(
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
    table: TableMeta = Depends(_table_from_path),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order_by: str | None = Query(None, description="Column to sort by. Defaults to the table primary key."),
):
    return list_rows(table, limit, offset, order_by)


@router.get("/{table_name}/{item_id}", summary="Get one row by primary key")
@limiter.limit("60/minute")
def get_table_row(
    request: Request,
    item_id: str,
    current_user: ClerkUser = Depends(require_auth),
    table: TableMeta = Depends(_table_from_path),
):
    return get_row(table, item_id)


@router.post("/{table_name}", status_code=status.HTTP_201_CREATED, summary="Create one row")
@limiter.limit("30/minute")
def create_table_row(
    request: Request,
    current_user: ClerkUser = Depends(require_auth),
    payload: dict[str, Any] = Body(..., examples=[{"code": "starter", "name": "Starter"}]),
    table: TableMeta = Depends(_table_from_path),
):
    return create_row(table, payload)


@router.patch("/{table_name}/{item_id}", summary="Update one row")
@limiter.limit("30/minute")
def update_table_row(
    request: Request,
    item_id: str,
    current_user: ClerkUser = Depends(require_auth),
    payload: dict[str, Any] = Body(..., examples=[{"name": "Starter"}]),
    table: TableMeta = Depends(_table_from_path),
):
    return update_row(table, item_id, payload)


@router.delete("/{table_name}/{item_id}", summary="Delete one row")
@limiter.limit("20/minute")
def delete_table_row(
    request: Request,
    item_id: str,
    current_user: ClerkUser = Depends(require_auth),
    table: TableMeta = Depends(_table_from_path),
):
    return delete_row(table, item_id)
