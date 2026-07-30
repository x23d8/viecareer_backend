from datetime import datetime, timezone

from services.sync_state_service import (
    _analysis_payload,
    _empty_state,
    _profile_completion,
    _progress_payload,
    _report_payload,
    _score,
    _uploaded_cv_payload,
)


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


def test_uploaded_cv_payload_keeps_cross_device_text():
    payload = _uploaded_cv_payload(
        {
            "id": "file-1",
            "file_name": "resume.pdf",
            "file_type": "pdf",
            "mime_type": "application/pdf",
            "file_size_bytes": 123,
            "storage_url": "database://uploaded-files/extracted-text",
            "extracted_text": "Backend engineer",
            "text_truncated": False,
            "file_purpose": "cv",
            "linked_to_analysis": True,
            "created_at_epoch_ms": 1234,
        }
    )

    assert payload["extracted_text"] == "Backend engineer"
    assert payload["linked_to_analysis"] is True
    assert payload["purpose"] == "cv"
    assert payload["text_truncated"] is False


def test_progress_payload_uses_real_skill_and_roadmap_state():
    payload = _progress_payload(
        profile={"full_name": "Lan", "email": "lan@example.com"},
        skills=[
            {
                "id": 1,
                "skill_name": "Python",
                "progress_percent": 80,
                "stage": "advanced",
                "trend": "up",
            }
        ],
        roadmaps=[
            {
                "id": "roadmap-1",
                "target_role": "Backend Engineer",
                "match_percent": 70,
                "status": "active",
            }
        ],
        roadmap_tasks=[
            {
                "id": 1,
                "roadmap_id": "roadmap-1",
                "task_title": "FastAPI",
                "status": "completed",
                "display_order": 1,
            },
            {
                "id": 2,
                "roadmap_id": "roadmap-1",
                "task_title": "PostgreSQL",
                "status": "in_progress",
                "display_order": 2,
            },
        ],
        metrics={"completed_interviews": 61, "interview_seconds": 3660},
    )

    assert payload["summary"]["average_skill_progress"] == 80
    assert payload["summary"]["roadmap_completion_percent"] == 50
    assert payload["summary"]["completed_interviews"] == 61
    assert payload["summary"]["interview_minutes"] == 61
    assert payload["roadmaps"][0]["completion_percent"] == 50


def test_profile_completion_and_empty_state_include_new_sync_sections():
    assert _profile_completion({"full_name": "Lan", "email": "lan@example.com"}) == 22
    state = _empty_state()
    assert state["profile"] is None
    assert state["uploaded_cvs"] == []
    assert state["progress"]["summary"]["completed_interviews"] == 0
