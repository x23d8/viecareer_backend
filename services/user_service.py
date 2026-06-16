"""User persistence helpers."""

import logging
from typing import Any

logger = logging.getLogger("viecareer")

from fastapi import HTTPException, status

from core.database import get_connection

LEGAL_TERMS_VERSION = "v4.0-2026-06-15"
LEGAL_PRIVACY_POLICY_VERSION = "v1.0-2026-06-15"


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


def get_legal_acceptance_status(
    *,
    external_auth_id: str,
    email: str | None,
) -> dict[str, Any]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_user_auth_columns(cur)
                row = _get_user_by_auth_identity(
                    cur,
                    external_auth_id=external_auth_id,
                    email=email,
                )

        return _legal_status_payload(row)
    except Exception as exc:
        logger.exception("get_legal_acceptance_status failed for external_auth_id=%s", external_auth_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load legal acceptance status. Please try again.",
        ) from exc


def accept_legal_policies(
    *,
    external_auth_id: str,
    email: str | None,
    terms_version: str,
    privacy_policy_version: str,
) -> dict[str, Any]:
    if terms_version != LEGAL_TERMS_VERSION or privacy_policy_version != LEGAL_PRIVACY_POLICY_VERSION:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Legal policy version is outdated. Please refresh and try again.",
        )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_user_auth_columns(cur)
                row = _get_user_by_auth_identity(
                    cur,
                    external_auth_id=external_auth_id,
                    email=email,
                    for_update=True,
                )
                if not row:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND,
                        detail="User profile has not been synced yet.",
                    )

                cur.execute(
                    """
                    update users
                    set
                      terms_accepted_at = now(),
                      terms_version = %s,
                      privacy_policy_accepted_at = now(),
                      privacy_policy_version = %s,
                      updated_at = now()
                    where id = %s
                    returning
                      id,
                      email,
                      terms_accepted_at,
                      terms_version,
                      privacy_policy_accepted_at,
                      privacy_policy_version
                    """,
                    (terms_version, privacy_policy_version, row["id"]),
                )
                accepted = cur.fetchone()

            conn.commit()
            return _legal_status_payload(accepted)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("accept_legal_policies failed for external_auth_id=%s", external_auth_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save legal acceptance. Please try again.",
        ) from exc


def _ensure_user_auth_columns(cur) -> None:
    cur.execute("alter table users add column if not exists external_auth_id varchar(255)")
    cur.execute("alter table users add column if not exists auth_provider varchar(50) null default 'clerk'")
    cur.execute("alter table users add column if not exists terms_accepted_at timestamptz null")
    cur.execute("alter table users add column if not exists terms_version varchar(50) null")
    cur.execute("alter table users add column if not exists privacy_policy_accepted_at timestamptz null")
    cur.execute("alter table users add column if not exists privacy_policy_version varchar(50) null")
    cur.execute(
        """
        create unique index if not exists idx_users_external_auth_id
        on users (external_auth_id)
        where external_auth_id is not null
        """
    )


def _get_user_by_auth_identity(
    cur,
    *,
    external_auth_id: str,
    email: str | None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    lock_clause = "for update" if for_update else ""
    cur.execute(
        f"""
        select
          id,
          email,
          terms_accepted_at,
          terms_version,
          privacy_policy_accepted_at,
          privacy_policy_version
        from users
        where external_auth_id = %s
          or (%s is not null and lower(email) = lower(%s))
        order by case when external_auth_id = %s then 0 else 1 end
        limit 1
        {lock_clause}
        """,
        (external_auth_id, email, email, external_auth_id),
    )
    return cur.fetchone()


def _legal_status_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    terms_accepted = bool(
        row
        and row.get("terms_accepted_at")
        and row.get("terms_version") == LEGAL_TERMS_VERSION
    )
    privacy_policy_accepted = bool(
        row
        and row.get("privacy_policy_accepted_at")
        and row.get("privacy_policy_version") == LEGAL_PRIVACY_POLICY_VERSION
    )

    return {
        "terms_version": LEGAL_TERMS_VERSION,
        "privacy_policy_version": LEGAL_PRIVACY_POLICY_VERSION,
        "terms_accepted": terms_accepted,
        "privacy_policy_accepted": privacy_policy_accepted,
        "requires_acceptance": not (terms_accepted and privacy_policy_accepted),
        "accepted_at": {
            "terms": row.get("terms_accepted_at") if row else None,
            "privacy_policy": row.get("privacy_policy_accepted_at") if row else None,
        },
    }


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
