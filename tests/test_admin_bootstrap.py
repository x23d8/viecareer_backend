"""Tests for the one-request admin workspace bootstrap."""

import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from fastapi.testclient import TestClient  # noqa: E402

from core.clerk_auth import AdminUser, require_admin  # noqa: E402
from main import app  # noqa: E402


class AdminBootstrapTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides.clear()
        app.dependency_overrides[require_admin] = lambda: AdminUser(
            user_id="admin_external",
            email="admin@example.com",
            internal_user_id="00000000-0000-0000-0000-000000000001",
            full_name="Admin",
            role_id=1,
            role="admin",
        )
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_bootstrap_rejects_unauthenticated_requests(self):
        app.dependency_overrides.clear()

        response = self.client.get("/api/admin/bootstrap")

        self.assertEqual(response.status_code, 401)

    def test_bootstrap_loads_and_shapes_every_initial_admin_resource(self):
        customer_items = [{"id": str(index)} for index in range(12)]
        customers = {
            "stats": {"total": 12, "active": 12, "suspended": 0, "pending": 0},
            "items": customer_items,
            "pagination": {"limit": 100, "offset": 0, "total": 12},
        }
        route_payloads = {
            "get_admin_overview": {"section": "overview"},
            "list_admin_users": customers,
            "list_admin_sessions": {"section": "sessions"},
            "list_admin_payments": {"section": "payments"},
            "get_admin_activity_logs": {"section": "activity"},
            "get_admin_ai_cost_summary": {"section": "ai-summary"},
            "get_admin_ai_cost_events": {"section": "ai-events"},
        }

        with ExitStack() as stack:
            mocks = {
                name: stack.enter_context(patch(f"routers.admin.{name}", return_value={"data": payload}))
                for name, payload in route_payloads.items()
            }
            feedback = stack.enter_context(
                patch("routers.admin.list_admin_feedback", return_value={"items": [], "pagination": {"total": 0}})
            )
            crawler = stack.enter_context(
                patch("routers.admin.get_crawler_sources", return_value=[{"source": "linkedin"}])
            )

            response = self.client.get("/api/admin/bootstrap?date_to=2026-08-11")

        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(payload["defaults"]["date_from"], "2026-07-13")
        self.assertEqual(payload["defaults"]["payment_date_from"], "2026-05-14")
        self.assertEqual(payload["defaults"]["date_to"], "2026-08-11")
        self.assertEqual(payload["overview"], route_payloads["get_admin_overview"])
        self.assertEqual(payload["customers"], customers)
        self.assertEqual(len(payload["users"]["items"]), 10)
        self.assertEqual(payload["users"]["pagination"]["limit"], 10)
        self.assertEqual(payload["feedback"]["items"], [])
        self.assertEqual(payload["crawler_sources"], [{"source": "linkedin"}])
        for loader in mocks.values():
            loader.assert_called_once()
        feedback.assert_called_once()
        crawler.assert_called_once()


if __name__ == "__main__":
    unittest.main()
