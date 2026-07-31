"""Apply and verify the one-time three-attempt grant for existing users."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.database import get_connection


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "20260731_grant_existing_users_free_attempts.sql"
)


def main() -> int:
    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(migration_sql)
            cur.execute(
                """
                select
                  count(*)::int as total_users,
                  count(*) filter (
                    where free_interview_attempts_granted_at is not null
                  )::int as users_with_grant,
                  count(*) filter (
                    where (
                      select count(*)
                      from interview_sessions s
                      where s.user_id = u.id
                        and s.attempt_consumed = true
                        and coalesce(s.attempt_source, 'included') = 'included'
                        and coalesce(s.attempt_plan_code, 'free') = 'free'
                        and s.created_at >= u.free_interview_attempts_granted_at
                    ) = 0
                  )::int as users_with_all_three_remaining
                from users u
                """
            )
            result = dict(cur.fetchone() or {})
        conn.commit()

    result["ok"] = result.get("total_users") == result.get("users_with_grant")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
