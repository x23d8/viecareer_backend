import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from services.interview_simulation_service import _normalize_blueprint


class InterviewBlueprintLimitTests(unittest.TestCase):
    def test_blueprint_is_capped_to_selected_unique_question_count(self):
        blueprint = {
            "questions": [
                {"id": "duplicate", "question": "One"},
                {"id": "duplicate", "question": "Two"},
                {"id": "q3", "question": "Three"},
                {"id": "q4", "question": "Four"},
            ]
        }

        normalized = _normalize_blueprint(blueprint, 3)
        ids = [question["id"] for question in normalized["questions"]]

        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        self.assertNotIn("q4", ids)

    def test_blueprint_is_padded_to_selected_question_count(self):
        normalized = _normalize_blueprint(
            {"questions": [{"id": "q1", "question": "Câu hỏi đầu tiên"}]},
            3,
            "vi",
        )

        self.assertEqual(len(normalized["questions"]), 3)
        self.assertEqual(normalized["questions"][1]["id"], "fallback_question_002")
        self.assertIn("Hãy", normalized["questions"][1]["question"])


if __name__ == "__main__":
    unittest.main()
