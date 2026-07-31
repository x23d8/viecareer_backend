import unittest
from unittest.mock import AsyncMock, patch

from core.rate_limit import check_daily_quota
from services.plan_quota_service import _quota_decision_sync


class SequenceCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((" ".join(str(query).split()).lower(), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class AdminUnlimitedAccessTests(unittest.IsolatedAsyncioTestCase):
    def test_admin_bypasses_feature_plan_quota(self):
        cursor = SequenceCursor([{"is_admin": True}])

        decision = _quota_decision_sync(
            cursor,
            external_auth_id="admin-external-id",
            feature_code="cv_jd_analysis",
            metric="count",
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.plan_code, "admin")
        self.assertIsNone(decision.limit_value)
        self.assertIsNone(decision.remaining)
        self.assertIsNone(decision.reset_at)

    async def test_admin_does_not_consume_backend_daily_quota(self):
        shared_increment = AsyncMock()
        with (
            patch("core.rate_limit.APP_ENV", "production"),
            patch("services.access_control_service.is_active_admin", new=AsyncMock(return_value=True)),
            patch("core.rate_limit._shared_increment", new=shared_increment),
        ):
            await check_daily_quota("admin-external-id")

        shared_increment.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
