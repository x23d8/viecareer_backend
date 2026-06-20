"""Regression tests for realtime quota and session lifecycle."""

import unittest
from unittest.mock import patch

from services.plan_quota_service import _realtime_session_count
from services.realtime_transcript_service import complete_live_interview_session


class FakeCursor:
    def __init__(self, row=None):
        self.row = row
        self.query = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.query = " ".join(str(query).split()).lower()
        self.params = params

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True


class RealtimeQuotaLifecycleTests(unittest.TestCase):
    def test_quota_counts_only_realtime_sessions_with_qa(self):
        cursor = FakeCursor({"used": 2})
        used = _realtime_session_count(cursor, "user_123", None)

        self.assertEqual(used, 2)
        self.assertIn("s.context_json is not null", cursor.query)
        self.assertIn("from interview_qa_pairs", cursor.query)
        self.assertIn("exists", cursor.query)

    def test_empty_session_is_cancelled_instead_of_completed(self):
        cursor = FakeCursor()
        connection = FakeConnection(cursor)

        with patch("services.realtime_transcript_service.get_connection", return_value=connection):
            complete_live_interview_session("session-123", 5)

        self.assertIn("then 'completed'", cursor.query)
        self.assertIn("else 'cancelled'", cursor.query)
        self.assertIn("from interview_qa_pairs", cursor.query)
        self.assertTrue(connection.committed)


if __name__ == "__main__":
    unittest.main()