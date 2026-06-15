"""Tests for backend admin authorization."""

import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from fastapi.testclient import TestClient  # noqa: E402

from core.clerk_auth import ClerkUser, _extract_role, require_auth  # noqa: E402
from main import app  # noqa: E402


class AdminAuthorizationTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides.clear()
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def authenticate_as(self, role: str | None):
        app.dependency_overrides[require_auth] = lambda: ClerkUser(
            user_id="user_123",
            email="user@example.com",
            role=role,
        )

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
        self.authenticate_as("user")

        response = self.client.get("/api/admin/overview")

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("data", response.json())

    def test_admin_endpoint_allows_admin_user(self):
        self.authenticate_as("admin")

        response = self.client.get("/api/admin/overview")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {})

    def test_database_analytics_tables_do_not_leak_without_admin(self):
        for path, expected_status in (
            ("/api/db/cv_jd_analyses", 401),
            ("/api/db/interview_sessions", 401),
        ):
            with self.subTest(path=path, expected_status=expected_status):
                response = self.client.get(path)

                self.assertEqual(response.status_code, expected_status)
                self.assertNotIn("data", response.json())

        self.authenticate_as("user")
        for path, expected_status in (
            ("/api/db/cv_jd_analyses", 403),
            ("/api/db/interview_sessions", 403),
        ):
            with self.subTest(path=path, expected_status=expected_status):
                response = self.client.get(path)

                self.assertEqual(response.status_code, expected_status)
                self.assertNotIn("data", response.json())

    def test_database_metadata_allows_admin_without_querying_rows(self):
        self.authenticate_as("admin")

        response = self.client.get("/api/db/_tables")

        self.assertEqual(response.status_code, 200)
        table_names = {table["name"] for table in response.json()["data"]}
        self.assertIn("cv_jd_analyses", table_names)
        self.assertIn("interview_sessions", table_names)


if __name__ == "__main__":
    unittest.main()
