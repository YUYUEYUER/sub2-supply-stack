from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.http_client import HTTPResult
from bridge.notifications import (
    FeishuNotifier,
    build_feishu_payload,
    feishu_signature,
    validate_feishu_webhook_url,
)
from bridge.store import Store
from bridge.web import _clean_settings


WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000"


class FeishuNotificationTests(unittest.TestCase):
    def test_webhook_validation_only_accepts_official_bot_endpoints(self):
        self.assertEqual(validate_feishu_webhook_url(WEBHOOK), WEBHOOK)
        with self.assertRaises(ValueError):
            validate_feishu_webhook_url("http://open.feishu.cn/open-apis/bot/v2/hook/id")
        with self.assertRaises(ValueError):
            validate_feishu_webhook_url("https://example.test/open-apis/bot/v2/hook/id")
        with self.assertRaises(ValueError):
            validate_feishu_webhook_url(f"{WEBHOOK}?redirect=https://example.test")

    def test_signature_matches_official_algorithm_shape(self):
        signature = feishu_signature(1599360473, "demo")
        self.assertEqual(signature, "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=")

    def test_card_payload_contains_signature_and_human_readable_amount(self):
        payload = build_feishu_payload(
            "warning",
            "余额不足",
            {"balance_fen": 123, "available_accounts": 2},
            event_type="balance_low",
            timestamp=1599360473,
            signing_secret="demo",
        )
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertEqual(payload["timestamp"], "1599360473")
        self.assertEqual(payload["card"]["header"]["template"], "orange")
        content = payload["card"]["elements"][0]["text"]["content"]
        self.assertIn("¥1.23", content)
        self.assertIn("可用账号", content)

    def test_card_labels_external_request_count_correctly(self):
        payload = build_feishu_payload(
            "error",
            "接口异常",
            {"request_count": 489},
            event_type="external_supplier_request_failed",
            timestamp=1599360473,
        )
        content = payload["card"]["elements"][0]["text"]["content"]
        self.assertIn("累计请求", content)
        self.assertNotIn("重试次数", content)

    @patch("bridge.notifications.request_json")
    def test_notifier_deduplicates_repeated_events(self, request):
        request.return_value = HTTPResult(200, {}, {"code": 0, "msg": "success"})
        notifier = FeishuNotifier()
        first = notifier.send(WEBHOOK, "", "error", "断供", {}, dedup_key="pool", cooldown_seconds=600)
        second = notifier.send(WEBHOOK, "", "error", "断供", {}, dedup_key="pool", cooldown_seconds=600)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(request.call_count, 1)

    def test_public_settings_mask_feishu_credentials(self):
        values = {
            "feishu_enabled": True,
            "feishu_webhook_url": WEBHOOK,
            "feishu_signing_secret": "secret",
        }
        clean = _clean_settings(
            values,
            {"webhook_configured": True, "signing_secret_configured": True},
        )
        self.assertNotIn("feishu_webhook_url", clean)
        self.assertNotIn("feishu_signing_secret", clean)
        self.assertTrue(clean["feishu_webhook_configured"])
        self.assertTrue(clean["feishu_signing_secret_configured"])

    def test_store_persists_feishu_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "bridge.db"))
            store.initialize()
            saved = store.update_settings(
                {
                    "feishu_enabled": True,
                    "feishu_webhook_url": WEBHOOK,
                    "feishu_signing_secret": "secret",
                }
            )
            self.assertTrue(saved["feishu_enabled"])
            self.assertEqual(saved["feishu_webhook_url"], WEBHOOK)


if __name__ == "__main__":
    unittest.main()
