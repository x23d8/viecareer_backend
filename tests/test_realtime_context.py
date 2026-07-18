"""Tests for realtime interview context persistence."""

import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from schemas.realtime import RealtimeSessionMetadata  # noqa: E402
from services.interview_report_service import save_interview_report  # noqa: E402
from services.realtime_service import _session_update_event  # noqa: E402
from services.realtime_transcript_service import create_live_interview_session  # noqa: E402


def valid_metadata() -> dict:
    return {
        "external_auth_id": "user_123",
        "email": "candidate@example.com",
        "account_full_name": "Nguyen Van An",
        "auth_provider": "clerk",
        "job_id": "11111111-1111-4111-8111-111111111111",
        "candidate": {
            "name": "Nguyen Van An",
            "profile": {"techStack": ["Python", "FastAPI"]},
            "cv_text": "Backend engineer with API experience.",
        },
        "position": "Backend Engineer",
        "job_description": "Build reliable FastAPI services.",
        "language": "vi",
        "voice": "shimmer",
        "blueprint": {
            "questions": [
                {
                    "id": "q1",
                    "group": "role_specific_knowledge",
                    "question": "How do you design a stable API?",
                }
            ]
        },
        "interview_style": "technical",
    }


class _FakeCursor:
    def __init__(self):
        self.calls: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.calls.append((str(query), params))

    def fetchone(self):
        return {"id": "session-123"}


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


class RealtimeSessionMetadataTests(unittest.TestCase):
    def test_all_context_fields_are_required(self):
        required_fields = (
            "candidate",
            "position",
            "job_description",
            "language",
            "voice",
            "blueprint",
        )
        for field in required_fields:
            with self.subTest(field=field):
                payload = valid_metadata()
                payload.pop(field)
                with self.assertRaises(ValidationError):
                    RealtimeSessionMetadata.model_validate(payload)

    def test_legacy_metadata_keys_are_normalized(self):
        payload = valid_metadata()
        payload["jd_summary"] = payload.pop("job_description")
        payload["interview_blueprint"] = payload.pop("blueprint")
        payload["extracted_analysis"] = payload.pop("candidate")["profile"]
        payload["candidate_name"] = "Nguyen Van An"

        metadata = RealtimeSessionMetadata.model_validate(payload).model_dump(mode="json")

        self.assertEqual(metadata["job_description"], "Build reliable FastAPI services.")
        self.assertEqual(metadata["blueprint"]["questions"][0]["id"], "q1")
        self.assertEqual(metadata["candidate"]["name"], "Nguyen Van An")
        self.assertEqual(metadata["candidate"]["profile"]["techStack"], ["Python", "FastAPI"])

    def test_blueprint_must_have_questions(self):
        payload = valid_metadata()
        payload["blueprint"] = {"questions": []}
        with self.assertRaises(ValidationError):
            RealtimeSessionMetadata.model_validate(payload)

    def test_candidate_profile_cannot_be_empty(self):
        payload = valid_metadata()
        payload["candidate"]["profile"] = {}
        with self.assertRaises(ValidationError):
            RealtimeSessionMetadata.model_validate(payload)

    def test_whitespace_only_candidate_name_is_rejected(self):
        payload = valid_metadata()
        payload["candidate"]["name"] = "   "
        with self.assertRaises(ValidationError):
            RealtimeSessionMetadata.model_validate(payload)

    def test_session_instructions_include_candidate_jd_and_voice(self):
        metadata = RealtimeSessionMetadata.model_validate(valid_metadata()).model_dump(mode="json")
        event = _session_update_event(metadata, metadata["blueprint"])
        session = event["session"]

        self.assertEqual(session["audio"]["output"]["voice"], "shimmer")
        self.assertIn("Backend Engineer", session["instructions"])
        self.assertIn("Build reliable FastAPI services.", session["instructions"])
        self.assertIn("Nguyen Van An", session["instructions"])
        self.assertIn("Python", session["instructions"])
        self.assertIn("entire interview in Vietnamese", session["instructions"])

    def test_persisted_session_receives_full_context_snapshot(self):
        metadata = RealtimeSessionMetadata.model_validate(valid_metadata()).model_dump(mode="json")
        cursor = _FakeCursor()
        connection = _FakeConnection(cursor)

        with (
            patch(
                "services.realtime_transcript_service.upsert_auth_user",
                return_value={"id": "user-db-id"},
            ),
            patch(
                "services.realtime_transcript_service.get_connection",
                return_value=connection,
            ),
        ):
            session_id = create_live_interview_session(metadata)

        insert_call = next(call for call in cursor.calls if "insert into interview_sessions" in call[0])
        params = insert_call[1]

        self.assertEqual(session_id, "session-123")
        self.assertEqual(params[2], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(params[3], "Backend Engineer")
        self.assertEqual(params[5].adapted["name"], "Nguyen Van An")
        self.assertEqual(params[6], "Build reliable FastAPI services.")
        self.assertEqual(params[7:9], ("vi", "shimmer"))
        self.assertEqual(params[9].adapted["questions"][0]["id"], "q1")
        self.assertEqual(params[10].adapted["position"], "Backend Engineer")
        self.assertEqual(params[10].adapted["job_id"], "11111111-1111-4111-8111-111111111111")
        self.assertNotIn("email", params[10].adapted)
        self.assertNotIn("external_auth_id", params[10].adapted)
        self.assertTrue(connection.committed)

    def test_final_report_updates_the_live_session_instead_of_creating_another(self):
        cursor = _FakeCursor()
        connection = _FakeConnection(cursor)

        with (
            patch(
                "services.interview_report_service.upsert_auth_user",
                return_value={"id": "user-db-id"},
            ),
            patch(
                "services.interview_report_service.get_connection",
                return_value=connection,
            ),
        ):
            session = save_interview_report(
                external_auth_id="user_123",
                email="candidate@example.com",
                account_full_name="Nguyen Van An",
                position="Backend Engineer",
                duration_seconds=120,
                overall_score=80,
                session_score=78,
                transcript_events=[],
                scored_qa=[],
                interview_dimensions={},
                final_dimensions={},
                phase2_signals=[],
                live_session_id="session-123",
            )

        statements = [query for query, _params in cursor.calls]
        self.assertEqual(session["id"], "session-123")
        self.assertTrue(any("update interview_sessions" in query for query in statements))
        self.assertFalse(any("insert into interview_sessions" in query for query in statements))
        self.assertFalse(any("delete from interview_transcript_events" in query for query in statements))
        self.assertFalse(any("delete from interview_qa_pairs" in query for query in statements))


if __name__ == "__main__":
    unittest.main()
