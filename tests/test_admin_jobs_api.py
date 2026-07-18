import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from fastapi.testclient import TestClient  # noqa: E402

from core.clerk_auth import AdminUser, require_admin  # noqa: E402
from main import app  # noqa: E402
from schemas.jobs import JobIngestionPayload  # noqa: E402
from services import admin_job_crawler_service as crawler_service  # noqa: E402
from services.job_ingestion_service import SourceAccessBlocked  # noqa: E402


class AdminJobsApiTests(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides.clear()
        app.dependency_overrides[require_admin] = lambda: AdminUser(
            user_id="admin_external",
            email="admin@example.com",
            role="admin",
            internal_user_id="46a3e598-ea47-4bbb-9a36-035fd4c12493",
        )
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        crawler_service._sessions.clear()

    def test_sources_endpoint_requires_admin_and_returns_status(self):
        statuses = [{"source": "topcv", "available": True}]
        with patch("routers.admin_jobs.get_crawler_sources", return_value=statuses):
            response = self.client.get("/api/admin/job-crawler/sources")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], statuses)

    def test_preview_forwards_filters_without_committing(self):
        preview = {
            "preview_id": "8e439e75-5f8d-4ad0-895c-490e3d59d12a",
            "items": [],
            "available_count": 0,
        }
        with patch("routers.admin_jobs.create_crawl_preview", return_value=preview) as mocked:
            response = self.client.post(
                "/api/admin/job-crawler/preview",
                json={
                    "sources": ["topcv", "itviec"],
                    "job_type": "AI Engineer",
                    "level": "senior",
                    "max_results": 10,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], preview)
        mocked.assert_called_once_with(
            admin_id="46a3e598-ea47-4bbb-9a36-035fd4c12493",
            sources=["topcv", "itviec"],
            job_type="AI Engineer",
            level="senior",
            max_results=10,
        )

    def test_preview_rejects_internal_source(self):
        response = self.client.post(
            "/api/admin/job-crawler/preview",
            json={"sources": ["viecareer"], "job_type": "IT", "max_results": 10},
        )
        self.assertEqual(response.status_code, 422)

    def test_commit_upserts_only_selected_preview_jobs(self):
        payload = JobIngestionPayload(
            external_id="1234",
            source_url="https://itviec.com/it-jobs/ai-engineer-acme-1234",
            title="AI Engineer",
            company_name="Acme",
            description="Build AI products",
        )
        preview_id = "8e439e75-5f8d-4ad0-895c-490e3d59d12a"
        crawler_service._sessions[preview_id] = crawler_service.PreviewSession(
            owner_id="46a3e598-ea47-4bbb-9a36-035fd4c12493",
            expires_at=crawler_service.datetime.now(crawler_service.UTC) + crawler_service.PREVIEW_TTL,
            items=[crawler_service.PreviewEntry(key="itviec:1234", source="itviec", payload=payload)],
        )
        with patch("services.admin_job_crawler_service.upsert_ingested_job", return_value="job-1") as mocked:
            response = self.client.post(
                "/api/admin/job-crawler/commit",
                json={"preview_id": preview_id, "quantity": 1, "selected_keys": ["itviec:1234"]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["upserted_count"], 1)
        mocked.assert_called_once()

    def test_preview_marks_explicit_source_rejection_as_blocked(self):
        with patch(
            "services.admin_job_crawler_service.preview_source_jobs",
            side_effect=SourceAccessBlocked("topcv.vn returned HTTP 403"),
        ):
            result = crawler_service.create_crawl_preview(
                admin_id="46a3e598-ea47-4bbb-9a36-035fd4c12493",
                sources=["topcv"],
                job_type="AI Engineer",
                level=None,
                max_results=10,
            )
        self.assertEqual(result["source_results"][0]["status"], "blocked")
        self.assertEqual(result["available_count"], 0)


if __name__ == "__main__":
    unittest.main()
