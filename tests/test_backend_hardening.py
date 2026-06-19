"""Regression tests for the backend hardening pass."""

import inspect
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.clerk_auth import ClerkUser, require_admin, verified_request_email
from main import app
from routers import database
from schemas.analysis import ExtractCVJDRequest
from services.billing_service import PAYMENT_STATUS_PAID, _mark_locked_payment_status
from services.plan_quota_service import _DEFAULT_PLANS


class BackendHardeningTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides.clear()
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_authentication_header_is_preserved(self):
        response = self.client.get("/api/auth/legal-status")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("www-authenticate"), "Bearer")

    def test_verified_email_claim_rejects_body_mismatch(self):
        with self.assertRaises(HTTPException) as raised:
            verified_request_email(
                ClerkUser(user_id="user_123", email="owner@example.com"),
                "attacker@example.com",
            )
        self.assertEqual(raised.exception.status_code, 403)

    def test_database_admin_routes_depend_on_require_admin(self):
        dependency = inspect.signature(database.db_tables).parameters["current_user"].default
        self.assertIs(dependency.dependency, require_admin)

    def test_plan_prices_match_production_catalog(self):
        prices = {code: price for code, _name, price, *_rest in _DEFAULT_PLANS}
        self.assertEqual(prices["standard"], "99000.00")
        self.assertEqual(prices["pro"], "299000.00")
        self.assertEqual(prices["team"], "1199000.00")
        self.assertEqual(prices["usage_based"], "29000.00")

    def test_paid_payment_cannot_be_downgraded(self):
        payment = {
            "id": 1,
            "user_id": "00000000-0000-0000-0000-000000000001",
            "plan_id": 2,
            "provider": "payos",
            "order_code": 20260619000001,
            "amount_vnd": 99000,
            "currency": "VND",
            "status": PAYMENT_STATUS_PAID,
            "subscription_id": 7,
        }
        cursor = unittest.mock.Mock()
        result = _mark_locked_payment_status(cursor, payment, "failed", {}, {})
        self.assertEqual(result["status"], PAYMENT_STATUS_PAID)
        cursor.execute.assert_not_called()

    def test_analysis_payload_has_server_side_size_bounds(self):
        with self.assertRaises(ValidationError):
            ExtractCVJDRequest(cv_text="x" * 60001, jd_text="valid")

    def test_readiness_reports_database_failure(self):
        with patch("routers.health._check_database", side_effect=RuntimeError("down")):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["database"], "unavailable")


if __name__ == "__main__":
    unittest.main()