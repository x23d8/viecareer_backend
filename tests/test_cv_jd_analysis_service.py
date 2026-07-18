from services.cv_jd_analysis_service import _public_analysis_row


def test_public_analysis_row_hides_internal_synthetic_markers():
    source = {
        "id": "analysis-id",
        "user_id": "user-id",
        "is_synthetic": True,
        "synthetic_seed": "admin_cv_jd_kpi_v1",
    }

    result = _public_analysis_row(source)

    assert result == {"id": "analysis-id", "user_id": "user-id"}
    assert source["is_synthetic"] is True
