"""Tests for user legal policy acceptance."""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from fastapi.testclient import TestClient  # noqa: E402

from core.clerk_auth import ClerkUser, require_auth  # noqa: E402
from main import app  # noqa: E402
from services.user_service import LEGAL_PRIVACY_POLICY_VERSION, LEGAL_TERMS_VERSION  # noqa: E402


class LegalAcceptanceTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides.clear()
        self.client = TestClient(app)
        self._patches = []

    def tearDown(self):
        app.dependency_overrides.clear()
        for active_patch in self._patches:
            active_patch.stop()

    def authenticate(self):
        app.dependency_overrides[require_auth] = lambda: ClerkUser(
            user_id="user_legal_123",
            email="legal@example.com",
            role="user",
        )

    def patch_user(self, row):
        fake_connection = FakeLegalConnection(row)
        active_patch = patch("services.user_service.get_connection", return_value=fake_connection)
        active_patch.start()
        self._patches.append(active_patch)
        return fake_connection

    def test_legal_status_requires_auth(self):
        response = self.client.get("/api/auth/legal-status")

        self.assertEqual(response.status_code, 401)

    def test_legal_status_requires_acceptance_when_versions_are_missing(self):
        self.authenticate()
        self.patch_user(
            {
                "id": "00000000-0000-0000-0000-000000000002",
                "email": "legal@example.com",
                "terms_accepted_at": None,
                "terms_version": None,
                "privacy_policy_accepted_at": None,
                "privacy_policy_version": None,
            }
        )

        response = self.client.get("/api/auth/legal-status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertTrue(payload["requires_acceptance"])
        self.assertFalse(payload["terms_accepted"])
        self.assertFalse(payload["privacy_policy_accepted"])
        self.assertEqual(payload["terms_version"], LEGAL_TERMS_VERSION)
        self.assertEqual(payload["privacy_policy_version"], LEGAL_PRIVACY_POLICY_VERSION)

    def test_accept_legal_policies_saves_current_versions(self):
        self.authenticate()
        fake_connection = self.patch_user(
            {
                "id": "00000000-0000-0000-0000-000000000002",
                "email": "legal@example.com",
                "terms_accepted_at": None,
                "terms_version": None,
                "privacy_policy_accepted_at": None,
                "privacy_policy_version": None,
            }
        )

        response = self.client.post(
            "/api/auth/accept-legal",
            json={
                "terms_version": LEGAL_TERMS_VERSION,
                "privacy_policy_version": LEGAL_PRIVACY_POLICY_VERSION,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertFalse(payload["requires_acceptance"])
        self.assertTrue(payload["terms_accepted"])
        self.assertTrue(payload["privacy_policy_accepted"])
        self.assertTrue(fake_connection.committed)

    def test_accept_legal_rejects_outdated_versions(self):
        self.authenticate()

        response = self.client.post(
            "/api/auth/accept-legal",
            json={
                "terms_version": "old",
                "privacy_policy_version": LEGAL_PRIVACY_POLICY_VERSION,
            },
        )

        self.assertEqual(response.status_code, 409)


class FakeLegalConnection:
    def __init__(self, row):
        self.row = row
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return FakeLegalCursor(self)

    def commit(self):
        self.committed = True


class FakeLegalCursor:
    def __init__(self, connection):
        self.connection = connection
        self.last_query = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(str(query).lower().split())
        if normalized.startswith("select"):
            self.last_query = "select"
            return

        if normalized.startswith("update users"):
            now = datetime(2026, 6, 15, tzinfo=timezone.utc)
            self.connection.row["terms_version"] = params[0]
            self.connection.row["privacy_policy_version"] = params[1]
            self.connection.row["terms_accepted_at"] = now
            self.connection.row["privacy_policy_accepted_at"] = now
            self.last_query = "update"
            return

        self.last_query = "other"

    def fetchone(self):
        if self.last_query in {"select", "update"}:
            return self.connection.row
        return None


if __name__ == "__main__":
    unittest.main()
