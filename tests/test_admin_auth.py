"""Tests for backend admin authorization."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from fastapi.testclient import TestClient  # noqa: E402

from core.clerk_auth import ClerkUser, _extract_role, require_auth  # noqa: E402
from main import app  # noqa: E402


class AdminAuthorizationTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides.clear()
        self.client = TestClient(app)
        self._patches = []

    def tearDown(self):
        app.dependency_overrides.clear()
        for active_patch in self._patches:
            active_patch.stop()

    def authenticate_as(self, role: str | None):
        app.dependency_overrides[require_auth] = lambda: ClerkUser(
            user_id="user_123",
            email="user@example.com",
            role=role,
        )

    def set_db_user(self, role_id: int | None):
        active_patch = patch("core.clerk_auth.get_connection", return_value=FakeConnection(role_id))
        active_patch.start()
        self._patches.append(active_patch)

    def set_feature_metrics(self, payload: dict):
        active_patch = patch("routers.admin.get_feature_metrics_service", return_value=payload)
        active_patch.start()
        self._patches.append(active_patch)

    def test_extracts_role_from_public_metadata_claim(self):
        self.assertEqual(_extract_role({"public_metadata": {"role": "admin"}}), "admin")
        self.assertEqual(_extract_role({"publicMetadata": {"role": "Admin"}}), "admin")
        self.assertEqual(_extract_role({"role": " admin "}), "admin")
        self.assertIsNone(_extract_role({"public_metadata": {"role": ""}}))

    def test_admin_endpoint_rejects_unauthenticated_user(self):
        response = self.client.get("/api/admin/overview")

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("data", response.json())

    def test_admin_endpoint_rejects_authenticated_non_admin_user(self):
        self.authenticate_as("admin")
        self.set_db_user(2)

        response = self.client.get("/api/admin/overview")

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("data", response.json())

    def test_admin_endpoint_allows_admin_user(self):
        FakeOverviewCursor.executed_queries.clear()
        self.authenticate_as("user")
        self.set_db_user(1)
        active_patch = patch("routers.admin.ensure_tracking_tables")
        active_patch.start()
        self._patches.append(active_patch)
        active_patch = patch("routers.admin.get_landing_metrics", return_value={
            "summary": {
                "landing_views": 0,
                "pricing_clicks": 0,
                "signup_clicks": 0,
                "login_clicks": 0,
                "unique_visitors": 0,
            },
            "by_source": [],
            "trend": [],
        })
        active_patch.start()
        self._patches.append(active_patch)
        self.set_feature_metrics(
            {
                "window": {
                    "date_from": "2026-06-01",
                    "date_to": "2026-06-30",
                    "group_by": "day",
                    "days": 30,
                },
                "summary": {"total_events": 12},
                "features": [],
                "trend": [{"period": "2026-06-01", "action_plan": 1}],
                "top_pages": [],
                "dropoffs": {},
            }
        )
        active_patch = patch("routers.admin.get_connection", return_value=FakeOverviewConnection())
        active_patch.start()
        self._patches.append(active_patch)

        response = self.client.get("/api/admin/overview")

        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(payload["current_admin"]["id"], "00000000-0000-0000-0000-000000000001")
        self.assertEqual(payload["current_admin"]["email"], "admin@example.com")
        self.assertEqual(payload["current_admin"]["full_name"], "Admin One")
        self.assertEqual(payload["current_admin"]["role_id"], 1)
        self.assertEqual(payload["kpis"]["total_users"], 1)
        self.assertEqual(payload["kpis"]["free_users"], 1)
        self.assertEqual(payload["kpis"]["free_users_count"], 1)
        self.assertEqual(payload["kpis"]["pro_users_count"], 0)
        self.assertEqual(payload["kpis"]["paid_users"], 0)
        self.assertEqual(payload["total_revenue_usd"], "10.000000")
        self.assertEqual(payload["free_users_count"], 1)
        self.assertEqual(payload["pro_users_count"], 0)
        self.assertEqual(payload["kpis"]["total_revenue_usd"], "10.000000")
        self.assertEqual(payload["charts"]["revenue"], [{"period": "2026-06-01", "revenue": "2.000000"}])
        self.assertEqual(payload["charts"]["revenue_by_month"], payload["charts"]["revenue"])
        self.assertEqual(
            payload["users_by_plan"],
            [
                {
                    "plan_code": "free",
                    "plan_name": "Free / Freemium",
                    "users": 1,
                    "is_paid": False,
                    "monthly_revenue": "0.000000",
                }
            ],
        )
        self.assertEqual(payload["dashboard"]["summary"]["total_events"], 12)
        self.assertEqual(payload["feature_metrics"]["summary"]["total_events"], 12)
        self.assertEqual(payload["function_metrics"], [])
        self.assertEqual(payload["charts"]["feature_trend"], [{"period": "2026-06-01", "action_plan": 1}])
        self.assertEqual(payload["business_report"]["registered_accounts"], 1)
        self.assertEqual(payload["business_report"]["activated_accounts"], 1)
        self.assertEqual(payload["business_report"]["ever_paid_accounts"], 1)
        self.assertEqual(payload["business_report"]["all_time_api_cost_usd"], "0.250000")
        self.assertEqual(payload["business_report"]["feature_usage"][2]["total_uses"], 3)
        overview_sql = "\n".join(FakeOverviewCursor.executed_queries)
        self.assertIn("when 'usage_based' then 200", overview_sql)
        self.assertNotIn("plan_id <> (select id from plans where code = 'usage_based')", overview_sql)

    def test_admin_routes_are_not_registered_twice(self):
        route_keys = [
            (path, tuple(sorted(route.methods or [])))
            for route in self.client.app.routes
            if (path := getattr(route, "path", "")).startswith("/api/admin")
        ]
        self.assertEqual(len(route_keys), len(set(route_keys)))

    def test_legacy_activity_log_routes_are_removed(self):
        self.assertEqual(self.client.get("/api/admin/activity/logs").status_code, 404)
        self.assertEqual(self.client.get("/api/admin/activity/logs.csv").status_code, 404)

    def test_admin_payments_maps_provider_status_and_revenue(self):
        self.authenticate_as("user")
        self.set_db_user(1)
        active_patch = patch("routers.admin.get_connection", return_value=FakePaymentConnection())
        active_patch.start()
        self._patches.append(active_patch)

        response = self.client.get("/api/admin/payments?status=success&search=admin%40example.com")

        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(payload["stats"]["total_revenue_vnd"], 250000)
        self.assertEqual(payload["stats"]["total_revenue_usd"], "10.000000")
        self.assertEqual(payload["items"][0]["status"], "success")
        self.assertEqual(payload["items"][0]["transaction_code"], "20260619000001")

    def test_unified_activity_stream_includes_payment_transactions(self):
        from routers.admin import _unified_activity_base_query

        query = " ".join(_unified_activity_base_query().split()).lower()
        self.assertIn("from payment_transactions pt", query)
        self.assertIn("'payment'::text as log_type", query)

    def test_admin_can_suspend_user(self):
        self.authenticate_as("user")
        self.set_db_user(1)
        active_patch = patch("routers.admin._ensure_activity_logs_schema")
        active_patch.start()
        self._patches.append(active_patch)
        connection = FakeUserStatusConnection()
        active_patch = patch("routers.admin.get_connection", return_value=connection)
        active_patch.start()
        self._patches.append(active_patch)

        response = self.client.patch(
            "/api/admin/users/00000000-0000-0000-0000-000000000002/status",
            json={"status": "suspended"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "suspended")
        self.assertTrue(connection.committed)

    def test_admin_user_status_rejects_unsupported_value(self):
        self.authenticate_as("user")
        self.set_db_user(1)

        response = self.client.patch(
            "/api/admin/users/00000000-0000-0000-0000-000000000002/status",
            json={"status": "pending"},
        )

        self.assertEqual(response.status_code, 422)

    def test_admin_me_returns_current_admin_from_database(self):
        self.authenticate_as("user")
        self.set_db_user(1)

        response = self.client.get("/api/admin/me")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["data"],
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "external_auth_id": "user_123",
                "email": "admin@example.com",
                "full_name": "Admin One",
                "role_id": 1,
            },
        )

    def test_database_metadata_is_not_registered_by_default(self):
        response = self.client.get("/api/db/_tables")

        self.assertEqual(response.status_code, 404)

    def test_database_metadata_stays_hidden_for_authenticated_users_by_default(self):
        self.authenticate_as("user")

        response = self.client.get("/api/db/_tables")

        self.assertEqual(response.status_code, 404)


class FakeConnection:
    def __init__(self, role_id: int | None):
        self.role_id = role_id

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return FakeCursor(self.role_id)


class FakeCursor:
    def __init__(self, role_id: int | None):
        self.role_id = role_id
        self.last_query = ""
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params):
        self.last_query = " ".join(query.split()).lower()
        self.last_params = params
        return None

    def fetchone(self):
        if self.role_id is None:
            return None
        return {
            "id": "00000000-0000-0000-0000-000000000001",
            "email": "admin@example.com",
            "full_name": "Admin One",
            "role_id": self.role_id,
        }


class FakeOverviewConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return FakeOverviewCursor()


class FakeOverviewCursor:
    executed_queries: list[str] = []

    def __init__(self):
        self.fetchone_calls = 0
        self.fetchall_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        self.executed_queries.append(query)
        return None

    def fetchone(self):
        rows = [
            {"total_users": 1, "free_users": 1, "pro_users": 0, "paid_users": 0},
            {"cv_jd_analyses": 0},
            {"completed_interviews": 0},
            {"total_ai_cost_usd": 0, "ai_calls": 0},
            {"total_revenue_vnd": 250000},
            {
                "registered_accounts": 1,
                "activated_accounts": 1,
                "ever_paid_accounts": 1,
                "all_time_api_cost_usd": 0.25,
            },
        ]
        row = rows[self.fetchone_calls]
        self.fetchone_calls += 1
        return row

    def fetchall(self):
        rows = [
            [
                {
                    "plan_code": "free",
                    "plan_name": "Free / Freemium",
                    "users": 1,
                    "is_paid": False,
                    "monthly_revenue": 0,
                }
            ],
            [{"plan_code": "standard", "plan_name": "Standard", "paid_accounts": 1}],
            [
                {"feature_key": "cv_jd_analysis", "label": "CV/JD Analysis", "total_uses": 1},
                {"feature_key": "ai_interview", "label": "AI Interview", "total_uses": 2},
                {"feature_key": "coding_interview", "label": "Coding Interview", "total_uses": 3},
                {
                    "feature_key": "personalized_action_plan",
                    "label": "Personalized Action Plan",
                    "total_uses": 4,
                },
            ],
            [],
            [{"period": "2026-06-01", "revenue_vnd": 50000}],
        ]
        result = rows[self.fetchall_calls]
        self.fetchall_calls += 1
        return result


class FakePaymentConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return FakePaymentCursor()


class FakePaymentCursor:
    def __init__(self):
        self.fetchone_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        return None

    def fetchone(self):
        self.fetchone_calls += 1
        return {"total": 1, "success": 1, "pending": 0, "failed": 0, "total_revenue_vnd": 250000}

    def fetchall(self):
        return [
            {
                "id": 1,
                "user_id": "00000000-0000-0000-0000-000000000002",
                "email": "admin@example.com",
                "full_name": "Admin Example",
                "plan_id": 3,
                "plan_code": "pro",
                "plan_name": "Pro / Premium",
                "provider": "payos",
                "order_code": 20260619000001,
                "payment_link_id": "link_1",
                "amount_vnd": 250000,
                "currency": "VND",
                "status": "paid",
                "description": "VC0000001",
                "paid_at": None,
                "created_at": None,
                "updated_at": None,
            }
        ]


class FakeUserStatusConnection:
    def __init__(self):
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return FakeUserStatusCursor()

    def commit(self):
        self.committed = True


class FakeUserStatusCursor:
    def __init__(self):
        self.execute_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        self.execute_calls += 1

    def fetchone(self):
        return {
            "id": "00000000-0000-0000-0000-000000000002",
            "email": "user@example.com",
            "full_name": "User Example",
            "status": "suspended",
            "updated_at": None,
        }


if __name__ == "__main__":
    unittest.main()
