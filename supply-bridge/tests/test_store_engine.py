from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from bridge.engine import (
    BridgeEngine,
    _credential_version,
    _looks_unauthorized,
    _redact_supplier_payload,
    _sub2_openai_extra,
    account_fingerprint,
    normalize_accounts,
    replacement_files,
    validate_supplier_account,
)
from bridge.store import Store


def valid_account():
    return {
        "email": "team@example.test",
        "credentials": {
            "email": "team@example.test",
            "refresh_token": "refresh-secret",
            "plan_type": "team",
        },
        "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
    }


class StoreAndEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / "bridge.db"))
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_start_in_dry_run(self):
        settings = self.store.settings()
        self.assertTrue(settings["auto_enabled"])
        self.assertTrue(settings["dry_run"])
        self.assertFalse(settings["emergency_stop"])

    def test_events_redact_nested_credentials(self):
        self.store.event(
            "info",
            "test",
            "redaction",
            {"credentials": {"refresh_token": "secret"}, "nested": {"access_token": "secret"}},
        )
        metadata = self.store.list_events(1)[0]["metadata"]
        self.assertEqual(metadata["credentials"], "[redacted]")
        self.assertEqual(metadata["nested"]["access_token"], "[redacted]")

    def test_normalize_and_fingerprint_are_stable(self):
        payload = {"data": {"payload": {"accounts": [{"email": "team@example.test", "refresh_token": "r"}]}}}
        accounts = normalize_accounts(payload)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(account_fingerprint(accounts[0]), account_fingerprint(accounts[0]))

    def test_account_validation_rejects_expired_and_non_team(self):
        account = valid_account()
        validate_supplier_account(account)
        account["credentials"]["plan_type"] = "plus"
        with self.assertRaisesRegex(ValueError, "Team"):
            validate_supplier_account(account)
        account = valid_account()
        account["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "expired"):
            validate_supplier_account(account)

    def test_supplier_payload_redacts_tokens_and_accounts(self):
        source = {"status": "ready", "accounts": [{"refresh_token": "secret"}], "token": "secret"}
        redacted = _redact_supplier_payload(source)
        self.assertEqual(redacted["accounts"], "[redacted]")
        self.assertEqual(redacted["token"], "[redacted]")
        self.assertNotIn("secret", json.dumps(redacted))

    def test_sub2_account_defaults_to_pooled_websocket(self):
        extra = _sub2_openai_extra({"openai_passthrough": False})
        self.assertEqual(extra["openai_oauth_responses_websockets_v2_mode"], "ctx_pool")
        self.assertTrue(extra["openai_oauth_responses_websockets_v2_enabled"])
        self.assertEqual(extra["codex_fingerprint_mode"], "session")

    def test_unauthorized_detection(self):
        self.assertTrue(_looks_unauthorized({"last_error": "HTTP 401 Unauthorized"}))
        self.assertFalse(_looks_unauthorized({"status": "active"}))

    def test_replacement_file_helpers(self):
        payload = {"payload": {"replacement_files": [{"id": "r1", "ready": True}]}}
        self.assertEqual(replacement_files(payload), [{"id": "r1", "ready": True}])
        self.assertEqual(_credential_version({"account": {"credential_version": 2}}), 2)

    def test_order_status_captures_replacement_files_before_terminal_exit(self):
        self.store.upsert_order(
            {
                "id": "local-1",
                "supplier_order_id": "supplier-1",
                "product": "oauth_7d",
                "quantity": 1,
                "status": "processing",
                "trigger_type": "empty",
                "estimated_fen": 300,
                "charged_fen": 0,
                "released_fen": 0,
                "idempotency_key": "idem-1",
                "attempts": 0,
                "last_error": "",
                "raw_json": "{}",
            }
        )
        supplier = MagicMock()
        supplier.order_status.return_value = {
            "order": {"status": "completed"},
            "replacement_files": [{"id": "r1", "ready": False}],
        }
        supplier.take_order.return_value = {"status": "completed", "accounts": []}
        engine = BridgeEngine(MagicMock(), self.store, MagicMock(), supplier)

        with patch.object(engine, "_capture_replacement_files") as capture:
            engine._poll_orders({})

            self.assertGreaterEqual(capture.call_count, 1)
            self.assertEqual(capture.call_args_list[0].args[1]["replacement_files"][0]["id"], "r1")
        self.assertEqual(self.store.order("local-1")["status"], "completed")


if __name__ == "__main__":
    unittest.main()
