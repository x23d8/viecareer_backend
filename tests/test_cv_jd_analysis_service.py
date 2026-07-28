from unittest.mock import patch

from services.cv_jd_analysis_service import _public_analysis_row, save_cv_jd_analysis


def test_public_analysis_row_returns_a_copy():
    source = {
        "id": "analysis-id",
        "user_id": "user-id",
        "target_position": "Backend Engineer",
    }

    result = _public_analysis_row(source)

    assert result == source
    assert result is not source


class _FakeCursor:
    def __init__(self):
        self.insert_params = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, query, params=None):
        if "insert into cv_jd_analyses" in query:
            self.insert_params = params

    def fetchone(self):
        return {
            "id": "analysis-id",
            "user_id": "user-id",
            "full_name": self.insert_params[2],
        }


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def cursor(self):
        return self._cursor

    def commit(self):
        return None


def test_save_uses_authoritative_user_full_name():
    cursor = _FakeCursor()
    connection = _FakeConnection(cursor)
    analysis = {
        "fullName": "Name extracted from CV",
        "atsComponents": {},
        "gapAnalysis": [],
        "readinessSignals": [],
        "baselineDimensions": {},
    }

    with (
        patch(
            "services.cv_jd_analysis_service.upsert_auth_user",
            return_value={"id": "user-id", "full_name": "Account User Name"},
        ),
        patch("services.cv_jd_analysis_service.get_connection", return_value=connection),
    ):
        result = save_cv_jd_analysis(
            external_auth_id="clerk-user",
            email="user@example.com",
            account_full_name="Account User Name",
            analysis=analysis,
        )

    assert cursor.insert_params[2] == "Account User Name"
    assert result["full_name"] == "Account User Name"
