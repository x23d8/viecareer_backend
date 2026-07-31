"""Server-owned account access checks shared by quota services."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.database import get_connection

logger = logging.getLogger("viecareer")


def is_active_admin_cursor(
    cur,
    *,
    external_auth_id: str | None = None,
    user_id: Any | None = None,
) -> bool:
    """Return whether the database account has the active admin role.

    Client/JWT role metadata is deliberately ignored. At least one server-owned
    user identifier must match the active row.
    """

    if not external_auth_id and user_id is None:
        return False
    cur.execute(
        """
        select exists (
          select 1
          from users u
          join roles r on r.id = u.role_id
          where u.status = 'active'
            and lower(r.code) = 'admin'
            and (%s::varchar is null or u.external_auth_id = %s)
            and (%s::uuid is null or u.id = %s)
        ) as is_admin
        """,
        (external_auth_id, external_auth_id, user_id, user_id),
    )
    row = cur.fetchone()
    return bool(row and row.get("is_admin"))


def is_active_admin_sync(*, external_auth_id: str) -> bool:
    if not external_auth_id:
        return False
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                return is_active_admin_cursor(cur, external_auth_id=external_auth_id)
    except Exception:
        # Privileged bypasses fail closed. A database outage must never grant
        # admin access, while normal users can continue through standard
        # capacity checks where the endpoint otherwise permits it.
        logger.exception("Could not verify admin quota bypass for user=%s", external_auth_id)
        return False


async def is_active_admin(*, external_auth_id: str) -> bool:
    return await asyncio.to_thread(
        is_active_admin_sync,
        external_auth_id=external_auth_id,
    )
