"""Replace generic ss-user roadmaps with one personalized roadmap per unique CV/JD."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.database import get_connection
from services.roadmap_service import (
    _analysis_fingerprint,
    _insert_tasks,
    _load_analysis,
    _roadmap_copy,
    _target_role,
    build_personalized_tasks,
)


def rebuild(*, apply: bool) -> dict[str, int]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                  app_user.external_auth_id,
                  analysis.id as analysis_id
                from cv_jd_analyses analysis
                join ss_users ss_user on ss_user.user_id = analysis.user_id
                join users app_user on app_user.id = analysis.user_id
                where analysis.input_fingerprint is not null
                order by app_user.external_auth_id, analysis.created_at, analysis.id
                """
            )
            analyses = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                select
                  count(distinct roadmap.id)::int as roadmaps,
                  count(task.id)::int as tasks
                from learning_roadmaps roadmap
                join ss_users ss_user on ss_user.user_id = roadmap.user_id
                left join roadmap_tasks task on task.roadmap_id = roadmap.id
                """
            )
            old = dict(cur.fetchone())
            if not apply:
                return {
                    "analyses": len(analyses),
                    "old_roadmaps": int(old["roadmaps"]),
                    "old_tasks": int(old["tasks"]),
                    "new_roadmaps": 0,
                }
            cur.execute(
                """
                delete from learning_roadmaps roadmap
                using ss_users ss_user
                where ss_user.user_id = roadmap.user_id
                """
            )
            created = 0
            for row in analyses:
                cur.execute(
                    "select id from users where external_auth_id = %s",
                    (row["external_auth_id"],),
                )
                user_id = str(cur.fetchone()["id"])
                analysis = _load_analysis(cur, user_id, str(row["analysis_id"]))
                fingerprint = _analysis_fingerprint(cur, analysis)
                title, summary = _roadmap_copy(analysis, "vi")
                cur.execute(
                    """
                    insert into learning_roadmaps (
                      user_id, cv_jd_analysis_id, job_id, input_fingerprint,
                      target_role, title, summary, language, match_percent, status
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, 'vi', %s, 'active')
                    returning id
                    """,
                    (
                        user_id,
                        analysis["id"],
                        analysis.get("job_id"),
                        fingerprint,
                        _target_role(analysis),
                        title,
                        summary,
                        analysis.get("ats_score") or analysis.get("jri_baseline"),
                    ),
                )
                roadmap_id = str(cur.fetchone()["id"])
                _insert_tasks(cur, roadmap_id, build_personalized_tasks(analysis, language="vi"))
                created += 1
        conn.commit()

    return {
        "analyses": len(analyses),
        "old_roadmaps": int(old["roadmaps"]),
        "old_tasks": int(old["tasks"]),
        "new_roadmaps": created,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete existing ss-user roadmaps and create the personalized replacements.",
    )
    args = parser.parse_args()
    print(rebuild(apply=args.apply))
