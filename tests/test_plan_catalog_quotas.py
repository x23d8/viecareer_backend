import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from services.billing_service import _limit_payload, _plan_payload
from services.plan_quota_service import _DEFAULT_LIMITS, _Plan, _window_for_period


class PlanCatalogQuotaTests(unittest.TestCase):
    def test_free_catalogue_is_three_uses_per_feature_for_lifetime(self):
        free_counted_limits = {
            (feature, metric): (period, value)
            for plan, feature, metric, period, value in _DEFAULT_LIMITS
            if plan == "free" and period != "session"
        }

        self.assertEqual(len(free_counted_limits), 5)
        self.assertTrue(all(item == ("lifetime", 3) for item in free_counted_limits.values()))

    def test_standard_catalogue_uses_weekly_and_daily_windows(self):
        standard_limits = {
            (feature, metric): (period, value)
            for plan, feature, metric, period, value in _DEFAULT_LIMITS
            if plan == "standard"
        }

        self.assertEqual(standard_limits[("cv_jd_analysis", "count")], ("week", 15))
        self.assertEqual(standard_limits[("realtime_interview", "session_count")], ("week", 10))
        self.assertEqual(standard_limits[("chatbot", "message_count")], ("day", 50))

    def test_lifetime_window_has_no_reset_and_week_resets_on_monday(self):
        granted_at = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)
        lifetime_start, lifetime_reset = _window_for_period(
            "lifetime",
            _Plan(None, "free", "Free", free_usage_started_at=granted_at),
        )
        week_start, week_reset = _window_for_period("week", _Plan(None, "standard", "Standard"))

        self.assertEqual(lifetime_start, granted_at)
        self.assertIsNone(lifetime_reset)
        self.assertEqual(week_start.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).weekday(), 0)
        self.assertEqual((week_reset - week_start).days, 7)

    def test_billing_catalogue_explains_units_consumption_and_reset(self):
        limit = _limit_payload(
            {
                "feature_code": "realtime_interview",
                "metric": "session_count",
                "period": "week",
                "limit_value": 10,
            }
        )
        plan = _plan_payload(
            {
                "id": 2,
                "code": "standard",
                "name": "Standard",
                "monthly_price": 99_000,
                "billing_period": "month",
                "included_members": 1,
                "quota_total": 15,
                "rate_limit_per_minute": 50,
            }
        )

        self.assertEqual(limit["period_label"], "mỗi tuần")
        self.assertIn("không hoàn lượt", limit["consumption_rule"])
        self.assertEqual(plan["quota_scope"], "per_member")
        self.assertIn("thứ Hai", plan["reset_note"])


if __name__ == "__main__":
    unittest.main()
