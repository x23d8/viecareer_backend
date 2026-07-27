import unittest

from fastapi import HTTPException

from services.interview_attempt_service import (
    INTERVIEW_ATTEMPT_PRICE_VND,
    InterviewAttemptStatus,
    _status_for_user,
    consume_interview_attempt,
)


class SequenceCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((" ".join(str(query).split()).lower(), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class InterviewAttemptTests(unittest.TestCase):
    def test_status_combines_three_initial_and_paid_attempts(self):
        cursor = SequenceCursor([
            None,
            {"purchased": 2, "included_used": 3, "purchased_used": 1},
        ])

        result = _status_for_user(cursor, "user-id")

        self.assertEqual(result.total, 5)
        self.assertEqual(result.remaining, 1)
        self.assertEqual(result.price_vnd, 29_000)
        self.assertIn("pt.status = 'paid'", cursor.queries[1][0])
        self.assertIn("s.attempt_consumed = true", cursor.queries[1][0])

    def test_attempt_payload_is_non_refundable(self):
        payload = InterviewAttemptStatus(
            plan_code="free",
            plan_name="Free",
            included=3,
            included_used=1,
            purchased=0,
            purchased_used=0,
            remaining=2,
        ).payload()

        self.assertFalse(payload["refundable"])
        self.assertEqual(payload["price_vnd"], INTERVIEW_ATTEMPT_PRICE_VND)

    def test_standard_plan_has_distinct_quota_and_session_limit(self):
        cursor = SequenceCursor([
            {
                "code": "standard",
                "name": "Standard",
                "start_at": None,
                "end_at": None,
            },
            {"purchased": 0, "included_used": 4, "purchased_used": 0},
        ])

        result = _status_for_user(cursor, "user-id")

        self.assertEqual(result.included, 10)
        self.assertEqual(result.included_remaining, 6)
        self.assertEqual(result.plan_max_session_minutes, 25)

    def test_purchased_attempt_is_used_after_plan_quota_and_has_15_minute_limit(self):
        cursor = SequenceCursor([
            {"id": "user-id"},
            {
                "code": "pro",
                "name": "Premium",
                "start_at": None,
                "end_at": None,
            },
            {"purchased": 2, "included_used": 20, "purchased_used": 1},
        ])

        reservation = consume_interview_attempt(cursor, user_id="user-id")

        self.assertEqual(reservation.source, "purchased")
        self.assertEqual(reservation.max_session_minutes, 15)

    def test_consumption_rejects_when_wallet_is_empty(self):
        # Schema DDL has no fetch, then SELECT ... FOR UPDATE returns the user,
        # and the aggregate query returns an exhausted wallet.
        cursor = SequenceCursor([
            {"id": "user-id"},
            None,
            {"purchased": 0, "included_used": 3, "purchased_used": 0},
        ])

        with self.assertRaises(HTTPException) as raised:
            consume_interview_attempt(cursor, user_id="user-id")

        self.assertEqual(raised.exception.status_code, 402)
        self.assertEqual(raised.exception.detail["code"], "INTERVIEW_ATTEMPTS_EXHAUSTED")
        self.assertEqual(raised.exception.detail["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
