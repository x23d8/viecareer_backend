from services.cv_jd_analysis_service import cv_jd_input_fingerprint
from services.roadmap_service import build_personalized_tasks


def _analysis(**overrides):
    value = {
        "id": "analysis-a",
        "input_fingerprint": "a" * 64,
        "target_position": "Backend Engineer",
        "job_title": "Backend Engineer - Payments",
        "company_name": "Acme Fintech",
        "jd_text_snapshot": "Own payment reconciliation and improve API reliability.",
        "key_responsibilities": ["Reduce reconciliation failures across payment partners"],
        "job_skills": ["Python", "PostgreSQL", "Kafka"],
        "gap_items": [
            {
                "category": "Domain & Technical",
                "skill": "Kafka",
                "status": "missing",
                "cv_evidence": "No production event-streaming example",
                "jd_requirement": "Operate Kafka pipelines for payment events",
                "score": 35,
            }
        ],
    }
    value.update(overrides)
    return value


def test_cv_jd_fingerprint_ignores_case_and_whitespace():
    first = cv_jd_input_fingerprint(" Alice  CV\nPython ", " Backend JD ")
    second = cv_jd_input_fingerprint("alice cv python", "backend jd")

    assert first == second
    assert cv_jd_input_fingerprint("", "backend jd") is None


def test_personalized_tasks_include_exact_job_and_gap_context():
    tasks = build_personalized_tasks(_analysis(), language="vi")
    combined = " ".join(f"{task['title']} {task['description']}" for task in tasks)

    assert len(tasks) >= 3
    assert "Acme Fintech" in combined
    assert "Backend Engineer - Payments" in combined
    assert "Operate Kafka pipelines for payment events" in combined
    assert len({task["client_key"] for task in tasks}) == len(tasks)


def test_different_job_context_produces_a_different_task_set():
    first = build_personalized_tasks(_analysis(), language="en")
    second = build_personalized_tasks(
        _analysis(
            id="analysis-b",
            input_fingerprint="b" * 64,
            job_title="Data Engineer - Risk",
            company_name="Beta Bank",
            jd_text_snapshot="Build fraud-risk batch pipelines with Spark.",
            key_responsibilities=["Own daily fraud feature pipelines"],
            job_skills=["Spark", "Airflow"],
            gap_items=[],
        ),
        language="en",
    )

    assert [task["client_key"] for task in first] != [task["client_key"] for task in second]
    assert [task["description"] for task in first] != [task["description"] for task in second]
