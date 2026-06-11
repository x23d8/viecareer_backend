"""User persistence helpers."""

import logging
from typing import Any

logger = logging.getLogger("viecareer")

from fastapi import HTTPException, status

from core.database import get_connection


def upsert_auth_user(
    *,
    external_auth_id: str,
    email: str,
    full_name: str,
    avatar_url: str | None = None,
    auth_provider: str = "clerk",
) -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_user_auth_columns(cur)
                role_id = _ensure_user_role(cur)

                cur.execute(
                    """
                    select id
                    from users
                    where external_auth_id = %s or email = %s
                    for update
                    """,
                    (external_auth_id, email),
                )
                matches = cur.fetchall()

                if len(matches) > 1:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        detail="Clerk user id and email are already linked to different users.",
                    )

                if matches:
                    cur.execute(
                        """
                        update users
                        set
                          external_auth_id = %s,
                          auth_provider = %s,
                          email = %s,
                          full_name = %s,
                          avatar_url = %s,
                          status = case
                            when status = 'suspended' then status
                            else 'active'
                          end,
                          last_active_at = now(),
                          updated_at = now()
                        where id = %s
                        returning *
                        """,
                        (
                            external_auth_id,
                            auth_provider,
                            email,
                            full_name,
                            avatar_url,
                            matches[0]["id"],
                        ),
                    )
                    row = cur.fetchone()
                else:
                    cur.execute(
                        """
                        insert into users (
                          external_auth_id,
                          auth_provider,
                          email,
                          full_name,
                          role_id,
                          status,
                          avatar_url,
                          last_active_at,
                          updated_at
                        )
                        values (%s, %s, %s, %s, %s, 'active', %s, now(), now())
                        returning *
                        """,
                        (
                            external_auth_id,
                            auth_provider,
                            email,
                            full_name,
                            role_id,
                            avatar_url,
                        ),
                    )
                    row = cur.fetchone()

            conn.commit()
            return row
    except HTTPException:
        raise
    except Exception as exc:
        # Log full details server-side; never expose DB internals to the client
        logger.exception("upsert_auth_user failed for external_auth_id=%s", external_auth_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to sync user profile. Please try again.",
        ) from exc


def _ensure_user_auth_columns(cur) -> None:
    cur.execute("alter table users add column if not exists external_auth_id varchar(255)")
    cur.execute("alter table users add column if not exists auth_provider varchar(50) null default 'clerk'")
    cur.execute(
        """
        create unique index if not exists idx_users_external_auth_id
        on users (external_auth_id)
        where external_auth_id is not null
        """
    )


def _ensure_user_role(cur) -> int:
    cur.execute("select id from roles where code = %s", ("user",))
    row = cur.fetchone()
    if row:
        return row["id"]

    cur.execute(
        """
        insert into roles (code, name)
        values (%s, %s)
        on conflict (code) do update set name = excluded.name
        returning id
        """,
        ("user", "User"),
    )
    return cur.fetchone()["id"]
