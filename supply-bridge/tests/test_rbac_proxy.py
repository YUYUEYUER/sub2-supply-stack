from __future__ import annotations

import unittest

from bridge.config import ProxyConfig
from bridge.http_client import HTTPFailure
from bridge.rbac_proxy import (
    RBACServer,
    response_contains_group,
    scope_response,
    validate_query,
    validate_request,
    virtual_group_response,
)


CONFIG = ProxyConfig(
    listen_host="127.0.0.1",
    listen_port=0,
    sub2_base_url="http://sub2api:8080",
    sub2_admin_key="admin-key",
    shared_token="shared-token",
    allowed_group_ids=(11, 12, 13),
    ownership_group_id=13,
    default_import_group_ids=(11, 13),
    allowed_models=("gpt-5.4", "gpt-5.6"),
    max_concurrency=50,
    request_timeout_seconds=5,
)


def account_payload():
    return {
        "name": "team@example.test",
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "email": "team@example.test",
            "refresh_token": "secret",
            "model_mapping": {"gpt-5.6": "gpt-5.6"},
        },
        "extra": {
            "openai_passthrough": False,
            "openai_oauth_responses_websockets_v2_mode": "ctx_pool",
            "openai_oauth_responses_websockets_v2_enabled": True,
        },
        "concurrency": 30,
        "group_ids": [11, 13],
    }


def codex_import_payload():
    return {
        "content": '{"access_token":"secret"}',
        "name": "supplier-team",
        "group_ids": [11, 13],
        "concurrency": 30,
        "credential_extras": {"model_mapping": {"gpt-5.6": "gpt-5.6"}},
        "extra": {
            "openai_passthrough": False,
            "openai_oauth_responses_websockets_v2_mode": "ctx_pool",
            "openai_oauth_responses_websockets_v2_enabled": True,
        },
        "update_existing": True,
    }


class RBACValidationTests(unittest.TestCase):
    def test_allows_scoped_account_create(self):
        result = validate_request(CONFIG, "POST", "/api/v1/admin/accounts", account_payload())
        self.assertEqual(result["group_ids"], [11, 13])
        self.assertEqual(result["extra"]["openai_oauth_responses_websockets_v2_mode"], "ctx_pool")
        self.assertTrue(result["extra"]["openai_oauth_responses_websockets_v2_enabled"])
        self.assertEqual(result["extra"]["codex_fingerprint_mode"], "session")

    def test_account_create_injects_pooled_ws_when_supplier_omits_extra(self):
        payload = account_payload()
        payload.pop("extra")
        result = validate_request(CONFIG, "POST", "/api/v1/admin/accounts", payload)
        self.assertEqual(
            result["extra"],
            {
                "openai_oauth_responses_websockets_v2_mode": "ctx_pool",
                "openai_oauth_responses_websockets_v2_enabled": True,
                "codex_fingerprint_mode": "session",
            },
        )

    def test_blocks_group_outside_allowlist(self):
        payload = account_payload()
        payload["group_ids"] = [99]
        with self.assertRaisesRegex(ValueError, "allowlist"):
            validate_request(CONFIG, "POST", "/api/v1/admin/accounts", payload)

    def test_blocks_model_outside_allowlist(self):
        payload = account_payload()
        payload["credentials"]["model_mapping"] = {"gpt-5.6": "unknown"}
        with self.assertRaisesRegex(ValueError, "model"):
            validate_request(CONFIG, "POST", "/api/v1/admin/accounts", payload)

    def test_blocks_excess_concurrency(self):
        payload = account_payload()
        payload["concurrency"] = 51
        with self.assertRaisesRegex(ValueError, "concurrency"):
            validate_request(CONFIG, "POST", "/api/v1/admin/accounts", payload)

    def test_only_allows_pooled_ws_mode(self):
        payload = account_payload()
        payload["extra"]["openai_oauth_responses_websockets_v2_mode"] = "passthrough"
        with self.assertRaisesRegex(ValueError, "ctx_pool"):
            validate_request(CONFIG, "POST", "/api/v1/admin/accounts", payload)
        payload = account_payload()
        payload["extra"]["openai_oauth_responses_websockets_v2_enabled"] = False
        with self.assertRaisesRegex(ValueError, "enabled"):
            validate_request(CONFIG, "POST", "/api/v1/admin/accounts", payload)

    def test_allows_scoped_codex_session_import(self):
        result = validate_request(
            CONFIG,
            "POST",
            "/api/v1/admin/accounts/import/codex-session",
            codex_import_payload(),
        )
        self.assertEqual(result["group_ids"], [11, 13])
        self.assertTrue(result["skip_default_group_bind"])
        self.assertTrue(result["confirm_mixed_channel_risk"])
        self.assertEqual(result["extra"]["openai_oauth_responses_websockets_v2_mode"], "ctx_pool")
        self.assertEqual(result["extra"]["codex_fingerprint_mode"], "session")

    def test_codex_import_injects_trusted_groups_when_supplier_omits_them(self):
        payload = codex_import_payload()
        payload.pop("group_ids")
        result = validate_request(
            CONFIG,
            "POST",
            "/api/v1/admin/accounts/import/codex-session",
            payload,
        )
        self.assertEqual(result["group_ids"], [11, 13])

    def test_codex_import_requires_ownership_group_and_allowed_models(self):
        payload = codex_import_payload()
        payload["group_ids"] = [11]
        with self.assertRaisesRegex(ValueError, "ownership group"):
            validate_request(CONFIG, "POST", "/api/v1/admin/accounts/import/codex-session", payload)
        payload = codex_import_payload()
        payload["credential_extras"] = {"model_mapping": {"gpt-5.6": "unknown"}}
        with self.assertRaisesRegex(ValueError, "model"):
            validate_request(CONFIG, "POST", "/api/v1/admin/accounts/import/codex-session", payload)

    def test_codex_import_blocks_proxy_and_strips_unknown_extras(self):
        payload = codex_import_payload()
        payload["proxy_id"] = 9
        with self.assertRaisesRegex(ValueError, "proxy"):
            validate_request(CONFIG, "POST", "/api/v1/admin/accounts/import/codex-session", payload)
        payload = codex_import_payload()
        payload["credential_extras"] = {"access_token": "override"}
        result = validate_request(CONFIG, "POST", "/api/v1/admin/accounts/import/codex-session", payload)
        self.assertEqual(result["credential_extras"], {})
        payload = codex_import_payload()
        payload["extra"] = {"openai_passthrough": False, "supplier_metadata": "ignored"}
        result = validate_request(CONFIG, "POST", "/api/v1/admin/accounts/import/codex-session", payload)
        self.assertEqual(
            result["extra"],
            {
                "openai_passthrough": False,
                "openai_oauth_responses_websockets_v2_mode": "ctx_pool",
                "openai_oauth_responses_websockets_v2_enabled": True,
                "codex_fingerprint_mode": "session",
            },
        )

    def test_blocks_delete_route(self):
        with self.assertRaises(HTTPFailure) as raised:
            validate_request(CONFIG, "DELETE", "/api/v1/admin/accounts/1", None)
        self.assertEqual(raised.exception.status, 403)

    def test_validates_schedulable_shape(self):
        result = validate_request(
            CONFIG,
            "POST",
            "/api/v1/admin/accounts/42/schedulable",
            {"schedulable": False},
        )
        self.assertEqual(result, {"schedulable": False})
        with self.assertRaises(ValueError):
            validate_request(CONFIG, "POST", "/api/v1/admin/accounts/42/schedulable", {"enabled": False})

    def test_query_allowlist(self):
        query = validate_query(
            "GET",
            "/api/v1/admin/accounts",
            "page=1&page_size=100&group=13&lite=true&sort_by=id&sort_order=desc",
            CONFIG,
        )
        self.assertIn("group=13", query)
        with self.assertRaisesRegex(ValueError, "query field"):
            validate_query("GET", "/api/v1/admin/accounts", "include_credentials=true")

    def test_account_query_requires_ownership_group(self):
        with self.assertRaisesRegex(ValueError, "ownership group"):
            validate_query("GET", "/api/v1/admin/accounts", "group=11&lite=true", CONFIG)
        query = validate_query("GET", "/api/v1/admin/accounts", "", CONFIG)
        self.assertIn("group=13", query)
        self.assertIn("lite=true", query)

    def test_bugteam_limit_query_is_scoped_to_ownership_group(self):
        query = validate_query("GET", "/api/v1/admin/accounts", "limit=500", CONFIG)
        self.assertIn("page_size=500", query)
        self.assertIn("group=13", query)
        self.assertNotIn("limit=", query)

    def test_bugteam_group_probe_maps_to_virtual_ownership_group(self):
        body = validate_request(CONFIG, "POST", "/api/v1/admin/groups", {"name": "BugTeam Monitor"})
        response = virtual_group_response(CONFIG, body)
        self.assertEqual(response["data"]["id"], 13)
        self.assertEqual(response["data"]["name"], "BugTeam Monitor")

    def test_account_query_rejects_duplicate_and_oversized_parameters(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_query("GET", "/api/v1/admin/accounts", "group=11&group=13&lite=true", CONFIG)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            validate_query("GET", "/api/v1/admin/accounts", "group=13&lite=true&page_size=1001", CONFIG)

    def test_operational_queries_require_ownership_group(self):
        query = validate_query(
            "GET", "/api/v1/admin/ops/concurrency", "platform=openai&group_id=13", CONFIG
        )
        self.assertEqual(query, "platform=openai&group_id=13")
        with self.assertRaisesRegex(ValueError, "ownership group"):
            validate_query(
                "GET", "/api/v1/admin/ops/account-availability", "platform=openai&group_id=11", CONFIG
            )

    def test_account_create_requires_ownership_group(self):
        payload = account_payload()
        payload["group_ids"] = [11]
        with self.assertRaisesRegex(ValueError, "ownership group"):
            validate_request(CONFIG, "POST", "/api/v1/admin/accounts", payload)

    def test_account_detail_ownership_shape(self):
        self.assertTrue(response_contains_group({"code": 0, "data": {"group_ids": [11, 13]}}, 13))
        self.assertTrue(response_contains_group({"groups": [{"id": 13}]}, 13))
        self.assertFalse(response_contains_group({"code": 0, "data": {"group_ids": [11]}}, 13))

    def test_external_request_status_tracks_recent_authenticated_traffic(self):
        server = RBACServer(("127.0.0.1", 0), CONFIG)
        try:
            self.assertFalse(server.external_status()["external_connected"])
            server.record_external_request("/api/v1/admin/accounts", 200, "upstream")
            status = server.external_status()
            self.assertTrue(status["external_connected"])
            self.assertEqual(status["external_last_path"], "/api/v1/admin/accounts")
            self.assertEqual(status["external_last_status"], 200)
            self.assertEqual(status["external_last_source"], "upstream")
            self.assertEqual(status["external_request_count"], 1)
        finally:
            server.server_close()

    def test_usage_fault_pattern_is_exactly_two_per_ten(self):
        server = RBACServer(("127.0.0.1", 0), CONFIG)
        try:
            pattern = [server.should_inject_usage_fault() for _ in range(20)]
            self.assertEqual(pattern[:10].count(True), 2)
            self.assertEqual(pattern[10:].count(True), 2)
            self.assertEqual([index + 1 for index, value in enumerate(pattern[:10]) if value], [4, 9])
        finally:
            server.server_close()

    def test_allows_owned_account_usage_route(self):
        self.assertIsNone(validate_request(CONFIG, "GET", "/api/v1/admin/accounts/42/usage", None))
        with self.assertRaises(HTTPFailure):
            validate_request(CONFIG, "POST", "/api/v1/admin/accounts/42/usage", {})

    def test_allows_only_valid_usage_query_fields(self):
        query = validate_query(
            "GET",
            "/api/v1/admin/accounts/42/usage",
            "force=true&source=customer_hub",
            CONFIG,
        )
        self.assertEqual(query, "force=true&source=customer_hub")
        with self.assertRaisesRegex(ValueError, "query field"):
            validate_query("GET", "/api/v1/admin/accounts/42/usage", "include_credentials=true", CONFIG)
        with self.assertRaisesRegex(ValueError, "force must be boolean"):
            validate_query("GET", "/api/v1/admin/accounts/42/usage", "force=maybe", CONFIG)
        with self.assertRaisesRegex(ValueError, "source is invalid"):
            validate_query("GET", "/api/v1/admin/accounts/42/usage", "source=../../admin", CONFIG)

    def test_allows_paginated_group_route(self):
        self.assertIsNone(validate_request(CONFIG, "GET", "/api/v1/admin/groups", None))
        query = validate_query("GET", "/api/v1/admin/groups", "page=1&page_size=100")
        self.assertEqual(query, "page=1&page_size=100")
        all_query = validate_query("GET", "/api/v1/admin/groups/all", "platform=openai")
        self.assertEqual(all_query, "platform=openai")

    def test_scopes_group_responses_to_allowlist(self):
        payload = {
            "code": 0,
            "data": {
                "items": [{"id": 11}, {"id": 99}, {"id": 13}],
                "total": 3,
            },
        }
        result = scope_response(CONFIG, "/api/v1/admin/groups", payload)
        self.assertEqual([item["id"] for item in result["data"]["items"]], [11, 13])
        self.assertEqual(result["data"]["total"], 2)


if __name__ == "__main__":
    unittest.main()
