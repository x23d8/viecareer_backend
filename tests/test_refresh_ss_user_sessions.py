from scripts.refresh_ss_user_sessions import _clean_source_body, build_job_snapshot


def _job(title: str, company: str, url: str) -> dict:
    return {
        "title": title,
        "company_name": company,
        "location": "Ho Chi Minh City",
        "workplace_type": "hybrid",
        "experience_level": "mid",
        "skills": ["Python", "PostgreSQL", "AWS"],
        "description": (
            "Top 3 Reasons To Join Us\n"
            "Competitive salary\n"
            "The Job\n"
            f"Build production systems for {title}."
        ),
        "source": "itviec",
        "source_url": url,
    }


def test_clean_source_body_removes_benefit_boilerplate():
    cleaned = _clean_source_body(_job("Data Engineer", "Acme", "https://example/1")["description"])

    assert cleaned.startswith("The Job")
    assert "Top 3 Reasons" not in cleaned


def test_job_snapshots_have_distinct_role_first_lines():
    first = build_job_snapshot(_job("Data Engineer", "Acme", "https://example/1"), "en")
    second = build_job_snapshot(_job("Backend Engineer", "Beta", "https://example/2"), "en")

    assert first.splitlines()[0] == "ROLE: Data Engineer — Acme"
    assert second.splitlines()[0] == "ROLE: Backend Engineer — Beta"
    assert first != second
