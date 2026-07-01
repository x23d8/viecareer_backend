import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from fastapi.testclient import TestClient  # noqa: E402

from core.clerk_auth import ClerkUser, require_auth  # noqa: E402
from main import app  # noqa: E402


class FeedbackApiTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides.clear()
        app.dependency_overrides[require_auth] = lambda: ClerkUser(
            user_id="user_feedback_123",
            email="feedback@example.com",
            role="user",
        )
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_submit_feedback_validates_and_forwards_authenticated_identity(self):
        result = {
            "id": "feedback-id",
            "session_id": "00000000-0000-0000-0000-000000000010",
            "source": "report",
            "emotion": "good",
            "rating": 4,
            "comment": None,
            "tags": ["useful"],
            "is_negative": False,
            "created_at": None,
            "updated_at": None,
        }
        with patch("routers.feedback.submit_session_feedback", return_value=result) as mocked:
            response = self.client.post(
                "/api/feedback",
                json={
                    "session_id": "00000000-0000-0000-0000-000000000010",
                    "source": "report",
                    "emotion": "good",
                    "rating": 4,
                    "comment": "   ",
                    "tags": ["useful", "useful"],
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["data"], result)
        mocked.assert_called_once_with(
            external_auth_id="user_feedback_123",
            email="feedback@example.com",
            session_id="00000000-0000-0000-0000-000000000010",
            source="report",
            emotion="good",
            rating=4,
            comment=None,
            tags=["useful"],
        )

    def test_submit_feedback_rejects_invalid_rating_and_tag(self):
        response = self.client.post(
            "/api/feedback",
            json={
                "session_id": "00000000-0000-0000-0000-000000000010",
                "source": "report",
                "emotion": "good",
                "rating": 6,
                "tags": ["unknown"],
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_submit_feedback_rejects_cost_fields(self):
        response = self.client.post(
            "/api/feedback",
            json={
                "session_id": "00000000-0000-0000-0000-000000000010",
                "source": "report",
                "emotion": "good",
                "rating": 4,
                "cost": 1,
            },
        )

        self.assertEqual(response.status_code, 422)

    def test_create_coding_session_returns_session_id(self):
        result = {
            "id": "coding-session-id",
            "position": "Backend Engineer",
            "company": None,
            "session_type": "coding",
            "status": "completed",
            "duration_seconds": None,
            "overall_score": None,
            "created_at": None,
        }
        with patch("routers.feedback.create_completed_coding_session", return_value=result):
            response = self.client.post(
                "/api/feedback/coding-sessions",
                json={"position": "Backend Engineer"},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["data"]["id"], "coding-session-id")


if __name__ == "__main__":
    unittest.main()
