from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from core.clerk_auth import ClerkUser, require_auth
from main import app


class SyncStateApiTests(TestCase):
    def setUp(self):
        app.dependency_overrides.clear()
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_sync_state_uses_authenticated_subject(self):
        app.dependency_overrides[require_auth] = lambda: ClerkUser(user_id="user_sync_123")
        state = {
            "latest_analysis": None,
            "reports": [],
            "dashboard": {"jri_score": 0, "baseline_score": 0, "completed_interviews": 0},
            "synced_at": "2026-07-30T00:00:00+00:00",
        }

        with patch("routers.sync.get_sync_state", return_value=state) as load_state:
            response = self.client.get("/api/sync/state?report_limit=25")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], state)
        load_state.assert_called_once_with(
            external_auth_id="user_sync_123",
            report_limit=25,
        )

    def test_sync_state_rejects_out_of_range_limit(self):
        app.dependency_overrides[require_auth] = lambda: ClerkUser(user_id="user_sync_123")

        response = self.client.get("/api/sync/state?report_limit=51")

        self.assertEqual(response.status_code, 422)
