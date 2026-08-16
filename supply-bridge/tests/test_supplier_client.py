from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from bridge.clients import SupplierClient, _header_seconds
from bridge.engine import _credential_version, replacement_files
from bridge.http_client import HTTPResult


class SupplierClientTests(unittest.TestCase):
    def test_login_token_is_refreshed_one_day_before_expiry(self):
        client = SupplierClient("https://supplier.example", "user", "password")
        before = time.monotonic()
        with patch(
            "bridge.clients.request_json",
            return_value=HTTPResult(200, {}, {"token": "customer-token"}),
        ) as request:
            self.assertEqual(client._login(), "customer-token")
        self.assertEqual(request.call_args.kwargs["body"]["account"], "user")
        self.assertNotIn("username", request.call_args.kwargs["body"])
        self.assertGreaterEqual(client._token_expires, before + 29 * 24 * 60 * 60)

    def test_recoveries_follow_next_before_id(self):
        client = SupplierClient("https://supplier.example", "user", "password")
        calls: list[dict] = []

        def fake_request(method, path, *, query=None, **kwargs):
            calls.append(query or {})
            if not query.get("before_id"):
                return {"items": [{"id": "r2"}], "next_before_id": "r1"}
            return {"items": [{"id": "r1"}], "next_before_id": ""}

        client.request = fake_request  # type: ignore[method-assign]
        self.assertEqual([item["id"] for item in client.recoveries()], ["r2", "r1"])
        self.assertEqual(calls[1]["before_id"], "r1")

    def test_take_202_preserves_retry_after(self):
        client = SupplierClient("https://supplier.example", "user", "password")
        client._request_result = lambda *args, **kwargs: HTTPResult(  # type: ignore[method-assign]
            202,
            {"Retry-After": "7"},
            {"status": "processing"},
        )
        result = client.take_order("order-1")
        self.assertEqual(result["status"], "processing")
        self.assertEqual(result["retry_after_seconds"], 7)

    def test_take_uses_bugteam_sub2_download(self):
        client = SupplierClient("https://bugteam.team", "user", "password")
        calls: list[tuple] = []

        def fake_result(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return HTTPResult(200, {}, {"accounts": [{"credentials": {"refresh_token": "x"}}]})

        client._request_result = fake_result  # type: ignore[method-assign]
        result = client.take_order("order-1")
        self.assertIn("accounts", result)
        self.assertEqual(calls[0][0], "GET")
        self.assertIn("/download?format=sub2", calls[0][1])

    def test_recovery_claim_sends_ticket_and_stable_idempotency_key(self):
        client = SupplierClient("https://bugteam.team", "user", "password")
        calls: list[dict] = []

        def fake_result(method, url, **kwargs):
            calls.append(kwargs)
            return HTTPResult(200, {}, {"accounts": []})

        client._request_result = fake_result  # type: ignore[method-assign]
        client.claim_recovery(
            "https://bugteam.team/api/customer/recoveries/1/claim",
            "ticket-1",
            "recovery-1",
        )
        self.assertEqual(calls[0]["idempotency_key"], "recovery-1")
        self.assertEqual(calls[0]["extra_headers"]["X-Recovery-Ticket"], "ticket-1")

    def test_supplier_urls_reject_other_hosts(self):
        client = SupplierClient("https://supplier.example", "user", "password")
        with self.assertRaisesRegex(Exception, "not allowed"):
            client._validate_supplier_url("https://other.example/api/customer/recoveries/1")

    def test_retry_after_is_bounded(self):
        self.assertEqual(_header_seconds({"retry-after": "999"}, "Retry-After", 1), 60)
        self.assertEqual(_header_seconds({"Retry-After": "bad"}, "Retry-After", 3), 3)

    def test_replacement_files_and_credential_version(self):
        payload = {
            "data": {
                "payload": {
                    "replacement_files": [
                        {"id": "r1", "ready": True, "status_url": "https://supplier.example/api/customer/r1"}
                    ]
                }
            }
        }
        self.assertEqual(replacement_files(payload)[0]["id"], "r1")
        self.assertEqual(_credential_version({"payload": {"credential_version": "4"}}), 4)


if __name__ == "__main__":
    unittest.main()
