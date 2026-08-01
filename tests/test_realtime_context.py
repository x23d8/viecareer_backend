"""Tests for realtime interview context persistence."""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from schemas.realtime import RealtimeSessionMetadata  # noqa: E402
from services.interview_report_service import save_interview_report  # noqa: E402
from services.realtime_service import (  # noqa: E402
    _RelayState,
    _candidate_is_ready,
    _candidate_turn_classification,
    _interview_questions_complete,
    _opening_readiness_prompt,
    _persist_realtime_event,
    _realtime_session_config,
    _response_instructions,
    _resolve_spoken_question_id,
    _session_update_event,
)
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

    def test_session_instructions_truncate_large_cv_and_jd_context(self):
        payload = valid_metadata()
        payload["job_description"] = "J" * 50_000
        payload["candidate"]["cv_text"] = "C" * 50_000
        payload["candidate"]["profile"] = {"summary": "P" * 20_000}
        metadata = RealtimeSessionMetadata.model_validate(payload).model_dump(mode="json")
        event = _session_update_event(metadata, metadata["blueprint"])
        instructions = event["session"]["instructions"]

        self.assertIn("[truncated for realtime]", instructions)
        self.assertLess(len(instructions), 25_000)
        self.assertNotIn("J" * 10_000, instructions)
        self.assertNotIn("C" * 10_000, instructions)

    def test_direct_realtime_session_uses_validated_question_progress_tool(self):
        metadata = RealtimeSessionMetadata.model_validate(valid_metadata()).model_dump(mode="json")
        config = _realtime_session_config(metadata, metadata["blueprint"], create_response=True)

        self.assertEqual(config["tool_choice"], "auto")
        tool = config["tools"][0]
        self.assertEqual(tool["name"], "mark_interview_question_answered")
        self.assertEqual(tool["parameters"]["properties"]["question_id"]["enum"], ["q1"])
        self.assertIn("Never call", tool["description"])

    def test_clarification_and_repeat_requests_do_not_count_as_answers(self):
        for transcript in (
            "Could you repeat the question?",
            "I didn't understand, can you clarify?",
            "Bạn có thể nhắc lại câu hỏi không?",
        ):
            with self.subTest(transcript=transcript):
                turn_type, counts = _candidate_turn_classification(
                    transcript,
                    {"turn_type": "answer", "counts_as_answer": True},
                )
                self.assertEqual(turn_type, "clarification")
                self.assertFalse(counts)

    def test_mobile_opening_mentions_candidate_and_position_before_questions(self):
        opening = _opening_readiness_prompt(valid_metadata())

        self.assertEqual(
            opening,
            "Chào Van An, đây là buổi phỏng vấn cho vị trí Backend Engineer. Bạn đã sẵn sàng chưa?",
        )

    def test_readiness_confirmation_accepts_natural_vietnamese_and_english(self):
        for transcript in ("Có", "Mình sẵn sàng", "Ok bắt đầu nhé", "Yes, let's go"):
            with self.subTest(transcript=transcript):
                self.assertTrue(_candidate_is_ready(transcript))
        for transcript in ("Mình chưa sẵn sàng", "Không có", "Not ready yet", "No", "Đợi mình một chút"):
            with self.subTest(transcript=transcript):
                self.assertFalse(_candidate_is_ready(transcript))

    def test_first_mobile_question_uses_transition_without_repeating_greeting(self):
        metadata = valid_metadata()
        state = _RelayState(
            metadata=metadata,
            blueprint=metadata["blueprint"],
            opening_started=True,
            readiness_confirmed=True,
        )
        instructions = _response_instructions(state, state.blueprint["questions"][0])

        self.assertIn("Ok, vậy chúng ta đến với câu hỏi đầu tiên.", instructions)
        self.assertNotIn("Chào Văn An", instructions)

    def test_opening_and_readiness_do_not_increment_interview_question_count(self):
        metadata = valid_metadata()
        state = _RelayState(
            metadata=metadata,
            blueprint=metadata["blueprint"],
            response_kind="readiness",
        )
        event = json.dumps(
            {
                "type": "response.output_audio_transcript.done",
                "transcript": _opening_readiness_prompt(metadata),
            }
        )

        with patch("services.realtime_service._save_transcript_event_safe"):
            asyncio.run(_persist_realtime_event(event, state, None, None))

        self.assertEqual(state.asked_question_ids, [])
        self.assertEqual(state.question_order, 0)
        self.assertIsNone(state.pending_question_id)

    def test_only_a_blueprint_question_increments_question_count(self):
        metadata = valid_metadata()
        first_question = metadata["blueprint"]["questions"][0]
        state = _RelayState(
            metadata=metadata,
            blueprint=metadata["blueprint"],
            response_kind="question",
            active_question_id=first_question["id"],
            pending_question_id=first_question["id"],
        )
        event = json.dumps(
            {
                "type": "response.output_audio_transcript.done",
                "transcript": first_question["question"],
            }
        )

        with patch("services.realtime_service._save_transcript_event_safe"):
            asyncio.run(_persist_realtime_event(event, state, None, None))

        self.assertEqual(state.asked_question_ids, [first_question["id"]])
        self.assertEqual(state.question_order, 1)

    def test_unique_question_ids_control_completion(self):
        state = _RelayState(
            blueprint={
                "questions": [
                    {"id": "q1", "question": "First?"},
                    {"id": "q2", "question": "Second?"},
                    {"id": "q2", "question": "Repeated second?"},
                ]
            },
            answered_question_ids=["q1"],
        )
        self.assertFalse(_interview_questions_complete(state))
        state.answered_question_ids.append("q2")
        self.assertTrue(_interview_questions_complete(state))

    def test_repeated_spoken_question_keeps_original_question_id(self):
        state = _RelayState(
            blueprint={
                "questions": [
                    {"id": "q1", "question": "Describe a difficult production incident you resolved."},
                    {"id": "q2", "question": "How do you evaluate engineering tradeoffs?"},
                ]
            },
            asked_question_ids=["q1"],
            active_question_id="q2",
        )

        resolved = _resolve_spoken_question_id(
            state,
            "Let me ask that again: Describe a difficult production incident you resolved.",
        )

        self.assertEqual(resolved, "q1")

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
        self.assertEqual(params[4], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(params[6].adapted["name"], "Nguyen Van An")
        self.assertEqual(params[7], "Build reliable FastAPI services.")
        self.assertEqual(params[8:10], ("vi", "shimmer"))
        self.assertEqual(params[10].adapted["questions"][0]["id"], "q1")
        self.assertEqual(params[11].adapted["position"], "Backend Engineer")
        self.assertEqual(params[11].adapted["job_id"], "11111111-1111-4111-8111-111111111111")
        self.assertNotIn("email", params[11].adapted)
        self.assertNotIn("external_auth_id", params[11].adapted)
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
