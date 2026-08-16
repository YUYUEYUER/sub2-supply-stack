from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from bridge.policy import Metrics, decide, schedule_is_due, validate_settings


class PolicyTests(unittest.TestCase):
    def metrics(self, **values):
        base = Metrics(captured_at=datetime.now(UTC).isoformat(), sub2_connected=True)
        for key, value in values.items():
            setattr(base, key, value)
        return base

    def test_empty_pool_orders_to_target(self):
        decision = decide(
            {
                "auto_enabled": True,
                "emergency_stop": False,
                "replenish_on_empty": True,
                "replenish_on_low_stock": True,
                "low_watermark": 2,
                "target_available": 6,
                "min_order_units": 1,
                "max_order_units": 5,
            },
            self.metrics(available_accounts=0),
        )
        self.assertTrue(decision.should_order)
        self.assertEqual(decision.quantity, 5)
        self.assertIn("empty_hub", decision.reasons)

    def test_emergency_stop_overrides_all_triggers(self):
        decision = decide(
            {"auto_enabled": True, "emergency_stop": True, "replenish_on_empty": True},
            self.metrics(available_accounts=0),
        )
        self.assertFalse(decision.should_order)
        self.assertEqual(decision.reasons, ("emergency_stop",))

    def test_concurrency_trigger_calculates_required_accounts(self):
        decision = decide(
            {
                "auto_enabled": True,
                "replenish_on_empty": False,
                "replenish_on_low_stock": False,
                "replenish_on_eta": False,
                "replenish_on_concurrency": True,
                "concurrency_threshold_percent": 80,
                "account_concurrency": 10,
                "min_order_units": 1,
                "max_order_units": 10,
            },
            self.metrics(concurrency_used=90, concurrency_max=100, concurrency_utilization=0.9),
        )
        self.assertTrue(decision.should_order)
        self.assertEqual(decision.quantity, 2)

    def test_settings_reject_inverted_limits(self):
        with self.assertRaisesRegex(ValueError, "max_order_units"):
            validate_settings({"min_order_units": 5, "max_order_units": 2})

    def test_settings_reject_invalid_models(self):
        with self.assertRaisesRegex(ValueError, "invalid model"):
            validate_settings({"models": ["gpt-5.6", "bad model"]})

    def test_settings_reject_non_feishu_webhook(self):
        with self.assertRaisesRegex(ValueError, "Webhook"):
            validate_settings({"feishu_webhook_url": "https://example.test/hook"})

    def test_settings_validate_feishu_alert_ranges(self):
        values = validate_settings(
            {
                "feishu_enabled": True,
                "feishu_balance_threshold_fen": 500,
                "feishu_cooldown_seconds": 600,
            }
        )
        self.assertTrue(values["feishu_enabled"])

    def test_schedule_due(self):
        now = datetime.now(UTC)
        settings = {"replenish_on_schedule": True, "schedule_interval_minutes": 60}
        self.assertTrue(schedule_is_due(settings, (now - timedelta(minutes=61)).isoformat(), now))
        self.assertFalse(schedule_is_due(settings, (now - timedelta(minutes=30)).isoformat(), now))


if __name__ == "__main__":
    unittest.main()
