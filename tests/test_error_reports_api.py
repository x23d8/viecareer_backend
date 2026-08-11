import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from fastapi.testclient import TestClient  # noqa: E402

from core.clerk_auth import AdminUser, ClerkUser, require_admin, require_auth  # noqa: E402
from main import app  # noqa: E402
from services.error_report_service import _fingerprint, _redact_context, _redact_text  # noqa: E402


class ErrorReportsApiTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides.clear()
        app.dependency_overrides[require_auth] = lambda: ClerkUser(
            user_id="user_error_123",
            email="error@example.com",
            role="user",
        )
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_submit_error_uses_verified_identity_and_structured_fields(self):
        result = {
            "id": "report-id",
            "external_auth_id": "user_error_123",
            "category": "authentication",
            "location": "bootstrap.token",
        }
        with patch("routers.errors.submit_error_report", return_value=result) as mocked:
            response = self.client.post(
                "/api/errors",
                json={
                    "category": "authentication",
                    "severity": "error",
                    "platform": "android",
                    "location": "bootstrap.token",
                    "route": "/android/auth-redirect",
                    "error_type": "TokenUnavailable",
                    "message": "Could not obtain a session token.",
                    "technical_detail": "Clerk user exists but getToken returned null.",
                    "app_version": "1.0.6",
                    "occurred_at_epoch_ms": 1_786_500_000_000,
                    "context": {"network_connected": True},
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["data"], result)
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["external_auth_id"], "user_error_123")
        self.assertEqual(kwargs["email"], "error@example.com")
        self.assertEqual(kwargs["category"], "authentication")
        self.assertEqual(kwargs["location"], "bootstrap.token")

    def test_submit_error_rejects_unknown_fields_and_sensitive_context(self):
        base = {
            "category": "api",
            "platform": "web",
            "location": "dashboard.load",
            "error_type": "HttpError",
            "message": "Request failed",
        }
        unknown = self.client.post("/api/errors", json={**base, "user_id": "spoofed"})
        sensitive = self.client.post("/api/errors", json={**base, "context": {"token": "secret"}})
        self.assertEqual(unknown.status_code, 422)
        self.assertEqual(sensitive.status_code, 422)

    def test_admin_can_filter_error_reports(self):
        app.dependency_overrides[require_admin] = lambda: AdminUser(
            user_id="admin_123",
            email="admin@example.com",
            role="admin",
            internal_user_id="00000000-0000-0000-0000-000000000001",
            full_name="Admin",
        )
        result = {"items": [], "pagination": {"limit": 10, "offset": 0, "total": 0}}
        with patch("routers.errors.list_admin_error_reports", return_value=result) as mocked:
            response = self.client.get(
                "/api/admin/errors?limit=10&category=authentication&platform=android&status=open"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], result)
        mocked.assert_called_once_with(
            limit=10,
            offset=0,
            search=None,
            category="authentication",
            platform="android",
            severity=None,
            report_status="open",
        )

    def test_redaction_removes_tokens_passwords_and_sensitive_context(self):
        token = "abcdefghijklmnop.qrstuvwxyzABCDEF.ghijklmnop"
        redacted = _redact_text(f"Bearer {token} password=hunter2", 2_000)
        context = _redact_context({"token": token, "screen": "dashboard"})
        self.assertNotIn(token, redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("token", context)
        self.assertEqual(context["screen"], "dashboard")

    def test_fingerprint_groups_numeric_variants_of_the_same_error(self):
        common = {
            "platform": "android",
            "category": "api",
            "location": "http.get./api/jobs",
            "error_type": "Http500",
            "http_status": 500,
        }
        first = _fingerprint(**common, message="Request 123 failed after 10 seconds")
        second = _fingerprint(**common, message="Request 456 failed after 20 seconds")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
