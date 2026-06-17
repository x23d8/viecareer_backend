"""Tests for CV/JD spam heuristics."""

import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from services.cvjd_spam_guard import CVJDSpamRejected, validate_cvjd_request  # noqa: E402


class CVJDSpamGuardTests(unittest.TestCase):
    def test_rejects_short_cv_text(self):
        with self.assertRaises(CVJDSpamRejected) as raised:
            validate_cvjd_request("short cv", _jd_text())

        self.assertEqual(raised.exception.code, "CV_TEXT_TOO_SHORT")

    def test_rejects_repetitive_cv_text(self):
        repeated_cv = "\n".join(["Built the same project with the same stack repeatedly."] * 12)

        with self.assertRaises(CVJDSpamRejected) as raised:
            validate_cvjd_request(repeated_cv, _jd_text())

        self.assertEqual(raised.exception.code, "CV_TEXT_REPETITIVE")

    def test_hash_normalizes_case_and_whitespace(self):
        first = validate_cvjd_request(_cv_text("Python  FastAPI"), _jd_text("Backend Engineer"))
        second = validate_cvjd_request(_cv_text("python fastapi"), _jd_text("backend   engineer"))

        self.assertEqual(first.input_hash, second.input_hash)


def _cv_text(extra: str = "Python FastAPI") -> str:
    return (
        f"Software engineer with three years of experience building production APIs using {extra}. "
        "Delivered authentication, database integration, monitoring, and deployment workflows. "
        "Collaborated with designers and product managers to ship reliable user-facing features. "
        "Improved performance, wrote tests, and maintained clear documentation for backend services."
    )


def _jd_text(extra: str = "Backend Engineer") -> str:
    return (
        f"We are hiring a {extra} to build REST APIs, integrate PostgreSQL, write automated tests, "
        "collaborate with product teams, monitor production systems, and communicate technical tradeoffs clearly."
    )


if __name__ == "__main__":
    unittest.main()
