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
        active_patch = patch("routers.admin.get_feature_metrics", return_value={"features": []})
        active_patch.start()
        self._patches.append(active_patch)
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
        self.assertEqual(payload["function_metrics"], [])

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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params):
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
    def __init__(self):
        self.fetchone_calls = 0
        self.fetchall_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        return None

    def fetchone(self):
        rows = [
            {"total_users": 1, "free_users": 1, "revenue": 0},
            {"cv_jd_analyses": 0},
            {"completed_interviews": 0},
            {"total_ai_cost_usd": 0, "ai_calls": 0},
        ]
        row = rows[self.fetchone_calls]
        self.fetchone_calls += 1
        return row

    def fetchall(self):
        self.fetchall_calls += 1
        return []


if __name__ == "__main__":
    unittest.main()
