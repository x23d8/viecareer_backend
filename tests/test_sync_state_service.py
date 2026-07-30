from datetime import datetime, timezone

from services.sync_state_service import _analysis_payload, _report_payload, _score


def test_analysis_payload_adds_server_analysis_id():
    payload = _analysis_payload(
        {
            "id": "analysis-1",
            "created_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
            "created_at_epoch_ms": 1234,
            "analysis": {"jriBaseline": 72},
        }
    )

    assert payload is not None
    assert payload["analysis"]["analysisId"] == "analysis-1"
    assert payload["analysis"]["jriBaseline"] == 72


def test_report_payload_is_bounded_and_client_friendly():
    payload = _report_payload(
        {
            "id": "report-1",
            "created_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
            "created_at_epoch_ms": 1234,
            "position": "Backend Engineer",
            "session_type": "technical",
            "duration_seconds": 600,
            "overall_score": 120,
            "session_score": -4,
            "dimensions": {"D1_technical": 81},
            "transcript_events": [{"role": "user", "text": "answer"}],
            "scored_qa": [{"question": "q", "answer": "a", "score": 80}],
        }
    )

    assert payload["type"] == "technical"
    assert payload["overall_score"] == 100
    assert payload["session_score"] == 0
    assert payload["dimensions"]["D1_technical"] == 81


def test_score_handles_invalid_values():
    assert _score(None) == 0
    assert _score("75") == 75
    assert _score("invalid") == 0
