import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from fastapi.testclient import TestClient  # noqa: E402

from core.clerk_auth import ClerkUser, optional_auth, require_auth  # noqa: E402
from core.openai_client import get_openai_client  # noqa: E402
from main import app  # noqa: E402
from schemas.jobs import JobIngestionPayload  # noqa: E402
from services.job_ingestion_service import (  # noqa: E402
    SOURCES,
    SourceAccessBlocked,
    _extract_detail_urls,
    _extract_job_payload,
    _extract_pagination_urls,
    _fetch,
    _infer_experience_level,
    build_keyword_listing_url,
    preview_source_jobs,
)
from services.job_market_service import (  # noqa: E402
    _load_interaction_states,
    get_job_practice_context,
)


class JobsApiTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides.clear()
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_list_jobs_forwards_filters_and_authenticated_identity(self):
        app.dependency_overrides[optional_auth] = lambda: ClerkUser(user_id="user_jobs_123")
        result = {"items": [], "pagination": {"page": 1, "page_size": 20, "total": 0, "has_more": False}}
        with patch("routers.jobs.search_jobs", return_value=result) as mocked:
            response = self.client.get(
                "/api/jobs?q=python&location=Hanoi&workplace_type=hybrid&source=itviec&sort=newest"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], result)
        mocked.assert_called_once_with(
            external_auth_id="user_jobs_123",
            query="python",
            location="Hanoi",
            workplace_types=["hybrid"],
            employment_types=[],
            experience_levels=[],
            experience_years_min=None,
            experience_years_max=None,
            sources=["itviec"],
            posted_within_days=None,
            sort="newest",
            page=1,
            page_size=20,
        )

    def test_list_jobs_forwards_experience_filters(self):
        result = {"items": [], "pagination": {"page": 1, "page_size": 20, "total": 0, "has_more": False}}
        with patch("routers.jobs.search_jobs", return_value=result) as mocked:
            response = self.client.get(
                "/api/jobs?experience_level=junior&experience_level=middle"
                "&experience_years_min=1&experience_years_max=4"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocked.call_args.kwargs["experience_levels"], ["junior", "middle"])
        self.assertEqual(mocked.call_args.kwargs["experience_years_min"], 1)
        self.assertEqual(mocked.call_args.kwargs["experience_years_max"], 4)

    def test_job_event_uses_authenticated_user(self):
        app.dependency_overrides[require_auth] = lambda: ClerkUser(user_id="user_jobs_123")
        result = {"id": 10, "job_id": "job-1", "event_type": "save"}
        with patch("routers.jobs.record_job_interaction", return_value=result) as mocked:
            response = self.client.post(
                "/api/jobs/job-1/events",
                json={"event_type": "save", "context": "recommendation", "source": "itviec"},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["data"], result)
        mocked.assert_called_once_with(
            external_auth_id="user_jobs_123",
            job_id="job-1",
            event_type="save",
            context="recommendation",
            source="itviec",
        )

    def test_practice_context_uses_authenticated_user(self):
        app.dependency_overrides[require_auth] = lambda: ClerkUser(user_id="user_jobs_123")
        result = {
            "job_id": "job-1",
            "title": "Backend Engineer",
            "company_name": "Acme",
            "job_description": "Build reliable services. " * 10,
        }
        with patch("routers.jobs.get_job_practice_context", return_value=result) as mocked:
            response = self.client.get("/api/jobs/job-1/practice-context")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], result)
        mocked.assert_called_once_with(job_id="job-1", external_auth_id="user_jobs_123")

    def test_practice_context_normalizes_the_authoritative_job(self):
        description = "  Build reliable FastAPI services.  \n\n Work with PostgreSQL.  " * 4
        with patch(
            "services.job_market_service.get_job",
            return_value={
                "id": "job-1",
                "title": "Backend Engineer",
                "company_name": "Acme",
                "description": description,
                "location": "Ho Chi Minh City",
                "experience_level": "middle",
                "skills": ["Python", "PostgreSQL"],
                "primary_source": "itviec",
                "sources": [{"source": "itviec", "source_url": "https://itviec.com/job-1"}],
                "last_seen_at": "2026-07-18T00:00:00Z",
            },
        ) as mocked:
            result = get_job_practice_context(job_id="job-1", external_auth_id="user_jobs_123")

        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(result["source"], "itviec")
        self.assertEqual(result["source_url"], "https://itviec.com/job-1")
        self.assertNotIn("\n\n", result["job_description"])
        mocked.assert_called_once_with(job_id="job-1", external_auth_id="user_jobs_123")

    def test_sync_requires_configured_secret(self):
        with patch("routers.jobs.JOB_SYNC_SECRET", None):
            response = self.client.post("/api/jobs/sync", headers={"X-Job-Sync-Secret": "wrong"})
        self.assertEqual(response.status_code, 503)

    def test_itviec_listing_only_extracts_allowed_detail_links(self):
        html = """
        <a href="/it-jobs/featured-engineer-acme-9999">Featured job</a>
        <h3 data-search--job-selection-target="jobTitle"
            data-url="/viec-lam-it/backend-engineer-acme-1420?lab_feature=preview_jd_page">Job</h3>
        <h3 data-search--job-selection-target="jobTitle"
            data-url="https://evil.example/viec-lam-it/fake-9999">Bad card</h3>
        <a href="/it-jobs/microsoft-dynamics-365">Skill category</a>
        <a href="https://evil.example/it-jobs/fake-9999">Bad</a>
        <a href="/it-jobs">Listing</a>
        """
        urls = _extract_detail_urls(SOURCES["itviec"], "https://itviec.com/it-jobs", html)
        self.assertEqual(urls, ["https://itviec.com/viec-lam-it/backend-engineer-acme-1420"])

    def test_itviec_pagination_keeps_only_same_listing_numbered_pages(self):
        html = """
        <a href="/viec-lam-it/data-analysis?locale=vi&page=3&source=search_job">3</a>
        <a href="/viec-lam-it/data-analysis?page=2&source=search_job">2</a>
        <a href="/viec-lam-it/python?page=2">Other keyword</a>
        <a href="https://evil.example/viec-lam-it/data-analysis?page=4">Bad</a>
        """
        urls = _extract_pagination_urls(
            SOURCES["itviec"],
            "https://itviec.com/viec-lam-it/data-analysis",
            html,
        )
        self.assertEqual(
            urls,
            [
                "https://itviec.com/viec-lam-it/data-analysis?page=2&source=search_job",
                "https://itviec.com/viec-lam-it/data-analysis?page=3&source=search_job",
            ],
        )

    def test_itviec_preview_follows_pages_until_requested_count(self):
        first_page = """
        <h3 data-search--job-selection-target="jobTitle" data-url="/it-jobs/data-one-acme-1001">One</h3>
        <h3 data-search--job-selection-target="jobTitle" data-url="/it-jobs/data-two-acme-1002">Two</h3>
        <a href="/it-jobs/data-analysis?page=2&source=search_job">2</a>
        """
        second_page = """
        <h3 data-search--job-selection-target="jobTitle" data-url="/it-jobs/data-three-acme-1003">Three</h3>
        """

        def fetched(_client, url):
            return second_page if "page=2" in url else first_page

        def payload(_definition, url, _html):
            external_id = url.rsplit("-", 1)[-1]
            return JobIngestionPayload(
                external_id=external_id,
                source_url=url,
                title=f"Data job {external_id}",
                company_name="Acme",
            )

        client = MagicMock()
        client.__enter__.return_value = client
        with (
            patch("services.job_ingestion_service._job_source_enabled", return_value=True),
            patch("services.job_ingestion_service._job_source_authorized", return_value=True),
            patch("services.job_ingestion_service.httpx.Client", return_value=client),
            patch("services.job_ingestion_service._require_robots_permission"),
            patch("services.job_ingestion_service._fetch", side_effect=fetched),
            patch("services.job_ingestion_service._extract_job_payload", side_effect=payload),
            patch("services.job_ingestion_service.time.sleep"),
            patch("services.job_ingestion_service.JOB_SOURCE_MAX_DETAILS", 10),
        ):
            result = preview_source_jobs("itviec", job_type="data analysis", limit=3)

        self.assertEqual(result["normalized_count"], 3)
        self.assertEqual(result["pages_crawled"], 2)
        self.assertEqual(result["discovered_count"], 3)
        self.assertFalse(result["has_more"])
        self.assertEqual(result["remaining_status"], "No more jobs left for this keyword.")

    def test_itviec_preview_reports_more_jobs_when_limit_is_reached(self):
        html = """
        <h3 data-search--job-selection-target="jobTitle" data-url="/it-jobs/data-one-acme-1001">One</h3>
        <h3 data-search--job-selection-target="jobTitle" data-url="/it-jobs/data-two-acme-1002">Two</h3>
        <a href="/it-jobs/data-analysis?page=2&source=search_job">2</a>
        """
        client = MagicMock()
        client.__enter__.return_value = client
        with (
            patch("services.job_ingestion_service._job_source_enabled", return_value=True),
            patch("services.job_ingestion_service._job_source_authorized", return_value=True),
            patch("services.job_ingestion_service.httpx.Client", return_value=client),
            patch("services.job_ingestion_service._require_robots_permission"),
            patch("services.job_ingestion_service._fetch", return_value=html),
            patch(
                "services.job_ingestion_service._extract_job_payload",
                return_value=JobIngestionPayload(
                    external_id="1001",
                    source_url="https://itviec.com/it-jobs/data-one-acme-1001",
                    title="Data job",
                    company_name="Acme",
                ),
            ),
            patch("services.job_ingestion_service.time.sleep"),
            patch("services.job_ingestion_service.JOB_SOURCE_MAX_DETAILS", 10),
        ):
            result = preview_source_jobs("itviec", job_type="data analysis", limit=1)

        self.assertEqual(result["normalized_count"], 1)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["remaining_status"], "More jobs are available for this keyword.")

    def test_jobposting_jsonld_is_normalized(self):
        html = """
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "JobPosting",
          "title": "Senior Python Engineer",
          "hiringOrganization": {"@type": "Organization", "name": "Acme", "logo": "https://cdn.example/logo.png"},
          "jobLocation": {"address": {"addressLocality": "Ho Chi Minh", "addressCountry": "VN"}},
          "jobLocationType": "TELECOMMUTE",
          "employmentType": "FULL_TIME",
          "datePosted": "2026-07-17T00:00:00Z",
          "description": "<p>Build APIs</p>",
          "skills": "Python, FastAPI, PostgreSQL",
          "baseSalary": {"currency": "VND", "value": {"minValue": 30000000, "maxValue": 50000000, "unitText": "MONTH"}}
        }
        </script>
        """
        payload = _extract_job_payload(
            SOURCES["itviec"],
            "https://itviec.com/it-jobs/senior-python-engineer-acme-1420",
            html,
        )
        self.assertEqual(payload.external_id, "1420")
        self.assertEqual(payload.company_name, "Acme")
        self.assertEqual(payload.workplace_type, "remote")
        self.assertEqual(payload.employment_type, "full-time")
        self.assertEqual(payload.skills, ["Python", "FastAPI", "PostgreSQL"])
        self.assertEqual(payload.salary_text, "30,000,000 - 50,000,000 VND MONTH")
        self.assertEqual(payload.experience_level, "senior")

    def test_keyword_listing_urls_use_known_public_routes(self):
        self.assertEqual(build_keyword_listing_url("itviec", "IT"), "https://itviec.com/it-jobs")
        self.assertEqual(
            build_keyword_listing_url("topcv", "Kinh tế"),
            "https://www.topcv.vn/tim-viec-lam-kinh-te",
        )

    def test_experience_level_is_inferred_from_vietnamese_and_years(self):
        self.assertEqual(_infer_experience_level("Thực tập sinh Python", ""), "internship")
        self.assertEqual(_infer_experience_level("Backend Engineer", "Yêu cầu 3 năm kinh nghiệm"), "middle")

    def test_interaction_state_query_casts_bound_job_ids_to_uuid_array(self):
        class FakeCursor:
            def __init__(self):
                self.queries = []

            def execute(self, query, params):
                self.queries.append((query, params))

            def fetchall(self):
                return []

        cursor = FakeCursor()
        _load_interaction_states(
            cursor,
            "70b21e9b-ebbc-4b01-b23c-a0ee6934bd5d",
            [{"id": "b57e3f4a-d12e-41d1-ab1e-ca5ec9702fe2"}],
        )
        self.assertEqual(len(cursor.queries), 2)
        self.assertTrue(all("any(%s::uuid[])" in query for query, _ in cursor.queries))

    def test_job_match_analysis_uses_authenticated_profile(self):
        app.dependency_overrides[require_auth] = lambda: ClerkUser(user_id="user_jobs_123")
        app.dependency_overrides[get_openai_client] = lambda: object()
        context = {"job_id": "b57e3f4a-d12e-41d1-ab1e-ca5ec9702fe2", "match_score": 72}
        analysis = {**context, "summary": "Good fit", "strengths": [], "gaps": [], "actions": []}
        with (
            patch("routers.jobs.get_job_match_context", return_value=context) as load_context,
            patch("routers.jobs.analyze_job_match", return_value=analysis) as analyze,
            patch("routers.jobs.check_user_limit_ai", new=AsyncMock()),
            patch("routers.jobs.check_daily_quota", new=AsyncMock()),
        ):
            response = self.client.post(
                "/api/jobs/b57e3f4a-d12e-41d1-ab1e-ca5ec9702fe2/match-analysis?language=vi"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], analysis)
        load_context.assert_called_once_with(
            job_id="b57e3f4a-d12e-41d1-ab1e-ca5ec9702fe2",
            external_auth_id="user_jobs_123",
        )
        analyze.assert_called_once()

    def test_job_match_chat_passes_only_authenticated_job_context(self):
        app.dependency_overrides[require_auth] = lambda: ClerkUser(user_id="user_jobs_123")
        app.dependency_overrides[get_openai_client] = lambda: object()
        context = {"job_id": "b57e3f4a-d12e-41d1-ab1e-ca5ec9702fe2", "title": "AI Engineer"}
        with (
            patch("routers.jobs.get_job_match_context", return_value=context),
            patch("routers.jobs.chat_about_job", return_value="Focus on Python projects.") as chat,
            patch("routers.jobs.check_user_limit_ai", new=AsyncMock()),
            patch("routers.jobs.check_daily_quota", new=AsyncMock()),
        ):
            response = self.client.post(
                "/api/jobs/b57e3f4a-d12e-41d1-ab1e-ca5ec9702fe2/match-chat?language=en",
                json={"message": "What should I improve?", "history": []},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["reply"], "Focus on Python projects.")
        self.assertEqual(chat.call_args.kwargs["message"], "What should I improve?")

    def test_fetch_reports_source_403_as_explicit_access_block(self):
        class Response:
            status_code = 403

        class Client:
            def get(self, url):
                del url
                return Response()

        with self.assertRaises(SourceAccessBlocked) as raised:
            _fetch(Client(), "https://www.topcv.vn/tim-viec-lam-ai-engineer")
        self.assertIn("request crawler allow-listing", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
