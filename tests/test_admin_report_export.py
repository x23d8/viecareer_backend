import csv
import io
import os
import unittest
from datetime import date
from unittest.mock import patch

from openpyxl import load_workbook

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CLERK_JWKS_URL", "https://example.com/.well-known/jwks.json")

from fastapi.testclient import TestClient  # noqa: E402

from core.clerk_auth import AdminUser, require_admin  # noqa: E402
from main import app  # noqa: E402
from services.admin_report_export_service import build_admin_report_workbook, csv_bytes  # noqa: E402


class AdminReportExportTests(unittest.TestCase):
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

    def test_export_selected_pages_returns_downloadable_workbook(self):
        workbook = b"PK\x03\x04test-workbook"
        with (
            patch("routers.admin.ensure_tracking_tables"),
            patch("routers.admin.build_admin_report_workbook", return_value=workbook) as mocked,
        ):
            response = self.client.post(
                "/api/admin/reports/export",
                json={
                    "pages": ["users", "sessions"],
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-19",
                    "group_by": "day",
                    "plan_filter": "paid",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, workbook)
        self.assertEqual(
            response.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn("viecareer-admin-reports-", response.headers["content-disposition"])
        self.assertIn(".xlsx", response.headers["content-disposition"])
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args.kwargs["pages"], ["users", "sessions"])
        self.assertEqual(mocked.call_args.kwargs["generated_by"], "admin@example.com")
        self.assertEqual(mocked.call_args.kwargs["plan_filter"], "paid")

    def test_export_rejects_empty_or_duplicate_page_selection(self):
        empty = self.client.post("/api/admin/reports/export", json={"pages": []})
        duplicate = self.client.post(
            "/api/admin/reports/export",
            json={"pages": ["users", "users"]},
        )

        self.assertEqual(empty.status_code, 422)
        self.assertEqual(duplicate.status_code, 422)

    def test_export_rejects_inverted_date_range(self):
        response = self.client.post(
            "/api/admin/reports/export",
            json={"pages": ["activity"], "date_from": "2026-07-20", "date_to": "2026-07-19"},
        )

        self.assertEqual(response.status_code, 422)

    def test_csv_is_utf8_and_neutralizes_spreadsheet_formulas(self):
        content = csv_bytes(
            [{"name": "Nguyễn Văn A", "comment": "=HYPERLINK(\"bad\")", "score": -1}],
            ["name", "comment", "score"],
        )
        decoded = content.decode("utf-8-sig")
        row = next(csv.DictReader(io.StringIO(decoded)))

        self.assertEqual(row["name"], "Nguyễn Văn A")
        self.assertTrue(row["comment"].startswith("'="))
        self.assertEqual(row["score"], "-1")

    def test_overview_workbook_contains_guide_and_readable_sheets(self):
        content = build_admin_report_workbook(
            pages=["overview"],
            date_from=date(2026, 7, 1),
            date_to=date(2026, 7, 19),
            generated_by="admin@example.com",
            overview_data={
                "window": {"date_from": "2026-07-01", "date_to": "2026-07-19", "group_by": "day"},
                "kpis": {"total_users": 12, "ai_calls": 34},
                "users_by_plan": [{"plan_code": "free", "users": 10}],
                "function_metrics": [{"feature_key": "chatbot", "events": 9}],
                "landing_metrics": {"summary": {"landing_views": 50}, "by_source": [], "trend": []},
                "charts": {"feature_trend": [], "ai_cost_by_day": [], "revenue": []},
            },
        )

        workbook = load_workbook(io.BytesIO(content))
        self.assertEqual(workbook.sheetnames[0], "Export Guide")
        self.assertIn("Overview KPIs", workbook.sheetnames)
        self.assertIn("Function Metrics", workbook.sheetnames)
        kpis = workbook["Overview KPIs"]
        self.assertEqual(kpis.freeze_panes, "A2")
        self.assertIsNotNone(kpis.auto_filter.ref)
        values = [[cell.value for cell in row] for row in kpis.iter_rows()]
        self.assertTrue(any("total_users" in row for row in values))
        self.assertTrue(any(12 in row for row in values))


if __name__ == "__main__":
    unittest.main()
