from __future__ import annotations

import hmac
import json
import re
import signal
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import ProxyConfig
from .http_client import HTTPFailure, request_json


ACCOUNT_PATH = re.compile(r"^/api/v1/admin/accounts/(\d+)$")
GROUP_PATH = re.compile(r"^/api/v1/admin/groups/(\d+)$")
TEST_PATH = re.compile(r"^/api/v1/admin/accounts/(\d+)/test$")
SCHEDULABLE_PATH = re.compile(r"^/api/v1/admin/accounts/(\d+)/schedulable$")
USAGE_PATH = re.compile(r"^/api/v1/admin/accounts/(\d+)/usage$")
CODEX_IMPORT_PATH = "/api/v1/admin/accounts/import/codex-session"
OPS_OVERVIEW_PATH = "/api/v1/admin/ops/dashboard/overview"
STATIC_ROUTES = {
    ("GET", "/api/v1/admin/groups"),
    ("POST", "/api/v1/admin/groups"),
    ("GET", "/api/v1/admin/groups/all"),
    ("GET", "/api/v1/admin/groups/capacity-summary"),
    ("GET", "/api/v1/admin/ops/concurrency"),
    ("GET", "/api/v1/admin/ops/account-availability"),
    ("GET", OPS_OVERVIEW_PATH),
    ("GET", "/api/v1/admin/accounts"),
    ("POST", "/api/v1/admin/accounts"),
    ("POST", CODEX_IMPORT_PATH),
    ("POST", "/api/v1/admin/accounts/usage/batch"),
}
CREATE_FIELDS = {
    "name",
    "notes",
    "platform",
    "type",
    "credentials",
    "extra",
    "concurrency",
    "priority",
    "rate_multiplier",
    "load_factor",
    "group_ids",
    "expires_at",
    "auto_pause_on_expired",
    "upstream_billing_probe_enabled",
    "confirm_mixed_channel_risk",
}
UPDATE_FIELDS = CREATE_FIELDS | {"status"}
CREDENTIAL_FIELDS = {
    "access_token",
    "refresh_token",
    "id_token",
    "email",
    "chatgpt_account_id",
    "chatgpt_user_id",
    "organization_id",
    "plan_type",
    "expires_at",
    "token_type",
    "scope",
    "model_mapping",
}
OPENAI_WS_MODE_KEY = "openai_oauth_responses_websockets_v2_mode"
OPENAI_WS_ENABLED_KEY = "openai_oauth_responses_websockets_v2_enabled"
CODEX_FINGERPRINT_MODE_KEY = "codex_fingerprint_mode"
EXTRA_FIELDS = {
    "openai_passthrough",
    OPENAI_WS_MODE_KEY,
    OPENAI_WS_ENABLED_KEY,
    CODEX_FINGERPRINT_MODE_KEY,
}
CODEX_IMPORT_FIELDS = {
    "content",
    "contents",
    "name",
    "notes",
    "group_ids",
    "proxy_id",
    "concurrency",
    "priority",
    "rate_multiplier",
    "load_factor",
    "expires_at",
    "auto_pause_on_expired",
    "credential_extras",
    "extra",
    "update_existing",
    "skip_default_group_bind",
    "confirm_mixed_channel_risk",
}
QUERY_FIELDS = {
    "/api/v1/admin/groups": {"page", "page_size", "platform", "status", "search"},
    "/api/v1/admin/groups/all": {"platform", "status"},
    "/api/v1/admin/ops/concurrency": {"platform", "group_id"},
    "/api/v1/admin/ops/account-availability": {"platform", "group_id"},
    OPS_OVERVIEW_PATH: {"range", "start", "end", "platform", "group_id", "mode"},
    "/api/v1/admin/accounts": {
        "page",
        "page_size",
        "platform",
        "type",
        "group",
        "search",
        "lite",
        "sort_by",
        "sort_order",
        "status",
        "limit",
    },
}
USAGE_QUERY_FIELDS = {"force", "source"}


class RBACServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: ProxyConfig):
        self.config = config
        self._activity_lock = threading.Lock()
        self._external_last_seen_at = ""
        self._external_last_seen_monotonic = 0.0
        self._external_last_path = ""
        self._external_last_status = 0
        self._external_last_source = ""
        self._external_request_count = 0
        self._chaos_usage_counter = 0
        super().__init__(address, RBACHandler)

    def should_inject_usage_fault(self) -> bool:
        with self._activity_lock:
            self._chaos_usage_counter += 1
            return self._chaos_usage_counter % 10 in {4, 9}

    def record_external_request(self, path: str, status: int, source: str = "") -> None:
        with self._activity_lock:
            self._external_last_seen_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._external_last_seen_monotonic = time.monotonic()
            self._external_last_path = path
            self._external_last_status = status
            self._external_last_source = source
            self._external_request_count += 1

    def external_status(self) -> dict[str, Any]:
        with self._activity_lock:
            age = (
                time.monotonic() - self._external_last_seen_monotonic
                if self._external_last_seen_monotonic
                else None
            )
            return {
                "external_connected": age is not None and age <= 60,
                "external_last_seen_at": self._external_last_seen_at,
                "external_last_path": self._external_last_path,
                "external_last_status": self._external_last_status,
                "external_last_source": self._external_last_source,
                "external_request_count": self._external_request_count,
            }


class RBACHandler(BaseHTTPRequestHandler):
    server: RBACServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._json(405, {"error": "method_not_allowed"})

    def do_PATCH(self) -> None:  # noqa: N802
        self._json(405, {"error": "method_not_allowed"})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if self.command == "GET" and path == "/health":
            self._json(200, {"status": "ok"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        if self.command == "GET" and path == "/proxy/status":
            self._json(200, self.server.external_status())
            return
        try:
            body = self._read_json() if self.command in {"POST", "PUT"} else None
            clean_body = validate_request(self.server.config, self.command, path, body)
            query = validate_query(self.command, path, parsed.query, self.server.config)
            if self.command == "POST" and path == "/api/v1/admin/groups":
                self._audit(200, path, source="virtual_ownership_group")
                self._safe_json(200, virtual_group_response(self.server.config, clean_body))
                return
            if self.command == "PUT" and GROUP_PATH.fullmatch(path):
                self._audit(200, path, source="virtual_ownership_group_update")
                self._safe_json(200, virtual_group_response(self.server.config, clean_body))
                return
            if path == "/api/v1/admin/accounts/usage/batch" and isinstance(clean_body, dict):
                for account_id in clean_body.get("account_ids", []):
                    self._require_owned_account(account_id)
            usage_match = USAGE_PATH.fullmatch(path)
            account_match = (
                ACCOUNT_PATH.fullmatch(path)
                or TEST_PATH.fullmatch(path)
                or SCHEDULABLE_PATH.fullmatch(path)
                or usage_match
            )
            if account_match and not (self.command == "GET" and ACCOUNT_PATH.fullmatch(path)):
                self._require_owned_account(int(account_match.group(1)))
            if (
                usage_match
                and self.headers.get("X-RBAC-Chaos-Test") == "2-of-10"
                and self.server.should_inject_usage_fault()
            ):
                self._audit(403, path, source="chaos_test")
                self._json(403, {"error": "injected_usage_fault"})
                return
            target = f"{self.server.config.sub2_base_url}{path}"
            if query:
                target = f"{target}?{query}"
            headers = {"x-api-key": self.server.config.sub2_admin_key}
            idempotency = self.headers.get("Idempotency-Key", "").strip()
            if idempotency and re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", idempotency):
                headers["Idempotency-Key"] = idempotency
            response = request_json(
                self.command,
                target,
                headers=headers,
                body=clean_body,
                timeout=self.server.config.request_timeout_seconds,
                max_bytes=16 * 1024 * 1024,
            )
            scoped = scope_response(self.server.config, path, response.data)
            self._audit(response.status, path)
            self._safe_json(response.status, scoped)
        except HTTPFailure as exc:
            self._audit(exc.status, path, source="upstream_or_policy")
            self._safe_json(
                exc.status if 400 <= exc.status <= 599 else 502,
                exc.data or {"error": str(exc)},
            )
        except ValueError as exc:
            query_keys = sorted({key for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)})
            self._audit(
                400,
                path,
                source="validation",
                query_keys=query_keys,
                error=str(exc)[:300],
            )
            self._safe_json(400, {"error": str(exc)})
        except Exception as exc:
            self._audit(502, path, source="proxy", error=type(exc).__name__)
            self._safe_json(502, {"error": "upstream_request_failed"})

    def _audit(self, status: int, path: str, **details: Any) -> None:
        if self.headers.get("CF-Connecting-IP") or self.headers.get("CF-Ray"):
            self.server.record_external_request(path, status, str(details.get("source") or ""))
        event = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": self.command,
            "path": path,
            "status": status,
        }
        event.update(details)
        print(json.dumps(event, separators=(",", ":")), flush=True)

    def _safe_json(self, status: int, payload: Any) -> bool:
        try:
            self._json(status, payload)
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _authorized(self) -> bool:
        expected = self.server.config.shared_token
        supplied = self.headers.get("Authorization", "")
        api_key = self.headers.get("x-api-key", "")
        return bool(expected) and (
            hmac.compare_digest(supplied, f"Bearer {expected}")
            or hmac.compare_digest(api_key, expected)
        )

    def _require_owned_account(self, account_id: int) -> None:
        result = request_json(
            "GET",
            f"{self.server.config.sub2_base_url}/api/v1/admin/accounts/{account_id}",
            headers={"x-api-key": self.server.config.sub2_admin_key},
            timeout=self.server.config.request_timeout_seconds,
            max_bytes=2 * 1024 * 1024,
        )
        if not response_contains_group(result.data, self.server.config.ownership_group_id):
            raise HTTPFailure(404, "account_not_found")

    def _read_json(self) -> Any:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0 or length > 2 * 1024 * 1024:
            raise ValueError("request body exceeded size limit")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc

    def _json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)


def validate_request(config: ProxyConfig, method: str, path: str, body: Any) -> Any:
    if (method, path) in STATIC_ROUTES:
        if method == "GET":
            return None
        if path == "/api/v1/admin/groups":
            if not isinstance(body, dict):
                raise ValueError("group body must be an object")
            return body
        if path == "/api/v1/admin/accounts":
            return _validate_account(config, body, CREATE_FIELDS, require_core=True)
        if path == CODEX_IMPORT_PATH:
            return _validate_codex_import(config, body)
        if path == "/api/v1/admin/accounts/usage/batch":
            return _validate_usage(body)
    if method == "GET" and ACCOUNT_PATH.fullmatch(path):
        return None
    if method == "GET" and USAGE_PATH.fullmatch(path):
        return None
    group_match = GROUP_PATH.fullmatch(path)
    if method == "GET" and group_match:
        if int(group_match.group(1)) != config.ownership_group_id:
            raise HTTPFailure(403, "group_not_allowed")
        return None
    if method == "PUT" and group_match:
        if int(group_match.group(1)) != config.ownership_group_id:
            raise HTTPFailure(403, "group_not_allowed")
        if not isinstance(body, dict):
            raise ValueError("group body must be an object")
        return body
    if method == "PUT" and ACCOUNT_PATH.fullmatch(path):
        return _validate_account(config, body, UPDATE_FIELDS, require_core=False)
    if method == "POST" and TEST_PATH.fullmatch(path):
        if body not in ({}, None):
            raise ValueError("account test body must be empty")
        return {}
    if method == "POST" and SCHEDULABLE_PATH.fullmatch(path):
        if not isinstance(body, dict) or set(body) != {"schedulable"} or not isinstance(body["schedulable"], bool):
            raise ValueError("schedulable body must contain one boolean field")
        return body
    raise HTTPFailure(403, "route_not_allowed")


def validate_query(
    method: str, path: str, raw_query: str, config: ProxyConfig | None = None
) -> str:
    if not raw_query:
        if config and path == "/api/v1/admin/accounts":
            return urllib.parse.urlencode(
                {
                    "page": 1,
                    "page_size": 100,
                    "platform": "openai",
                    "type": "oauth",
                    "group": config.ownership_group_id,
                    "lite": "true",
                }
            )
        if config and path in {"/api/v1/admin/ops/concurrency", "/api/v1/admin/ops/account-availability"}:
            raise ValueError("operational queries must target the ownership group")
        if config and path == OPS_OVERVIEW_PATH:
            return urllib.parse.urlencode(
                {"platform": "openai", "group_id": config.ownership_group_id}
            )
        return ""
    if method == "GET" and USAGE_PATH.fullmatch(path):
        allowed = USAGE_QUERY_FIELDS
    else:
        allowed = QUERY_FIELDS.get(path, set()) if method == "GET" else set()
    values = urllib.parse.parse_qsl(raw_query, keep_blank_values=False, max_num_fields=20)
    if any(key not in allowed for key, _ in values):
        raise ValueError("query field is not allowed")
    keys = [key for key, _ in values]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate query fields are not allowed")
    params = dict(values)
    if USAGE_PATH.fullmatch(path):
        if "force" in params and params["force"].lower() not in {"true", "false", "1", "0"}:
            raise ValueError("usage query force must be boolean")
        if "source" in params and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", params["source"]):
            raise ValueError("usage query source is invalid")
    if config and path == "/api/v1/admin/accounts":
        if "group" in params and params["group"] != str(config.ownership_group_id):
            raise ValueError("account queries must target the ownership group")
        if params.get("platform", "openai") != "openai" or params.get("type", "oauth") != "oauth":
            raise ValueError("account query platform or type is not allowed")
        if params.get("lite", "true").lower() not in {"true", "1"}:
            raise ValueError("account queries must use lite mode")
        if "status" in params and params["status"] not in {"active", "inactive", "error", "disabled"}:
            raise ValueError("account query status is not allowed")
        if "sort_by" in params and params["sort_by"] not in {"id", "name", "status", "created_at", "updated_at"}:
            raise ValueError("account query sort field is not allowed")
        if "sort_order" in params and params["sort_order"].lower() not in {"asc", "desc"}:
            raise ValueError("account query sort order is not allowed")
        if "limit" in params:
            if "page_size" in params:
                raise ValueError("account query cannot combine limit and page_size")
            params["page_size"] = params.pop("limit")
        for key in ("page", "page_size"):
            if key in params and (not params[key].isdigit() or int(params[key]) < 1):
                raise ValueError("account query pagination is invalid")
        if int(params.get("page", "1")) > 100000 or int(params.get("page_size", "100")) > 1000:
            raise ValueError("account query pagination exceeds the limit")
        params.setdefault("page", "1")
        params.setdefault("page_size", "100")
        params["platform"] = "openai"
        params["type"] = "oauth"
        params["group"] = str(config.ownership_group_id)
        params["lite"] = "true"
    if config and path in {"/api/v1/admin/ops/concurrency", "/api/v1/admin/ops/account-availability"}:
        if params.get("group_id") != str(config.ownership_group_id):
            raise ValueError("operational queries must target the ownership group")
        if params.get("platform", "openai") != "openai":
            raise ValueError("operational query platform is not allowed")
    if config and path == OPS_OVERVIEW_PATH:
        if "group_id" in params and params["group_id"] != str(config.ownership_group_id):
            raise ValueError("dashboard overview must target the ownership group")
        if params.get("platform", "openai") != "openai":
            raise ValueError("dashboard overview platform is not allowed")
        params["platform"] = "openai"
        params["group_id"] = str(config.ownership_group_id)
    return urllib.parse.urlencode(params)


def virtual_group_response(config: ProxyConfig, body: Any) -> dict[str, Any]:
    requested_name = str(body.get("name") or "Supply Monitor").strip() if isinstance(body, dict) else ""
    return {
        "code": 0,
        "message": "success",
        "data": {
            "id": config.ownership_group_id,
            "name": requested_name[:200] or "Supply Monitor",
            "platform": "openai",
            "status": "active",
        },
    }


def scope_response(config: ProxyConfig, path: str, payload: Any) -> Any:
    if ACCOUNT_PATH.fullmatch(path):
        if not response_contains_group(payload, config.ownership_group_id):
            raise HTTPFailure(404, "account_not_found")
        return payload
    if path not in {"/api/v1/admin/groups", "/api/v1/admin/groups/all"}:
        return payload

    def allowed(item: Any) -> bool:
        return isinstance(item, dict) and item.get("id") in config.allowed_group_ids

    if not isinstance(payload, dict) or "data" not in payload:
        return payload
    result = dict(payload)
    data = payload.get("data")
    if isinstance(data, list):
        result["data"] = [item for item in data if allowed(item)]
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        scoped_data = dict(data)
        scoped_items = [item for item in data["items"] if allowed(item)]
        scoped_data["items"] = scoped_items
        for key in ("total", "total_count"):
            if key in scoped_data:
                scoped_data[key] = len(scoped_items)
        result["data"] = scoped_data
    return result


def response_contains_group(payload: Any, group_id: int) -> bool:
    if group_id <= 0:
        return False
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return False
    groups = data.get("group_ids", data.get("groups", []))
    if not isinstance(groups, list):
        return False
    normalized = {
        item.get("id") if isinstance(item, dict) else item
        for item in groups
    }
    return group_id in normalized


def _validate_account(
    config: ProxyConfig, body: Any, allowed_fields: set[str], *, require_core: bool
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("account body must be an object")
    unknown = set(body) - allowed_fields
    if unknown:
        raise ValueError(f"account fields are not allowed: {', '.join(sorted(unknown))}")
    result = dict(body)
    if require_core:
        required = {"name", "platform", "type", "credentials", "group_ids"}
        missing = required - set(result)
        if missing:
            raise ValueError(f"missing account fields: {', '.join(sorted(missing))}")
    if "platform" in result and result["platform"] != "openai":
        raise ValueError("only the openai platform is allowed")
    if "type" in result and result["type"] != "oauth":
        raise ValueError("only oauth accounts are allowed")
    if "status" in result and result["status"] not in {"active", "inactive"}:
        raise ValueError("account status is not allowed")
    if "concurrency" in result:
        concurrency = result["concurrency"]
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= config.max_concurrency:
            raise ValueError("account concurrency exceeds the RBAC limit")
    if "group_ids" in result:
        result["group_ids"] = _validate_groups(result["group_ids"], config.allowed_group_ids)
        if config.ownership_group_id not in result["group_ids"]:
            raise ValueError("account must remain in the ownership group")
    if "credentials" in result:
        result["credentials"] = _validate_credentials(result["credentials"], config.allowed_models)
    if "extra" in result:
        if not isinstance(result["extra"], dict) or set(result["extra"]) - EXTRA_FIELDS:
            raise ValueError("account extra fields are not allowed")
        _validate_extra(result["extra"])
        result["extra"] = _with_account_defaults(result["extra"])
    elif require_core:
        result["extra"] = _with_account_defaults(None)
    return result


def _validate_groups(value: Any, allowed: tuple[int, ...]) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ValueError("group_ids must be a non-empty array")
    groups: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item not in allowed:
            raise ValueError("group_id is outside the RBAC allowlist")
        if item not in groups:
            groups.append(item)
    return groups


def _validate_extra(value: dict[str, Any]) -> None:
    if "openai_passthrough" in value and not isinstance(value["openai_passthrough"], bool):
        raise ValueError("openai_passthrough must be boolean")
    if OPENAI_WS_MODE_KEY in value and value[OPENAI_WS_MODE_KEY] != "ctx_pool":
        raise ValueError("OpenAI OAuth WS mode must be ctx_pool")
    if OPENAI_WS_ENABLED_KEY in value and value[OPENAI_WS_ENABLED_KEY] is not True:
        raise ValueError("OpenAI OAuth WS must be enabled")
    if CODEX_FINGERPRINT_MODE_KEY in value and value[CODEX_FINGERPRINT_MODE_KEY] != "session":
        raise ValueError("Codex fingerprint mode must be session")


def _with_account_defaults(value: dict[str, Any] | None) -> dict[str, Any]:
    extra = dict(value or {})
    extra[OPENAI_WS_MODE_KEY] = "ctx_pool"
    extra[OPENAI_WS_ENABLED_KEY] = True
    extra[CODEX_FINGERPRINT_MODE_KEY] = "session"
    return extra


def _validate_credentials(value: Any, allowed_models: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("credentials must be an object")
    unknown = set(value) - CREDENTIAL_FIELDS
    if unknown:
        raise ValueError(f"credential fields are not allowed: {', '.join(sorted(unknown))}")
    result = dict(value)
    mapping = result.get("model_mapping")
    if mapping is not None:
        if not isinstance(mapping, dict):
            raise ValueError("model_mapping must be an object")
        if any(key not in allowed_models or value not in allowed_models for key, value in mapping.items()):
            raise ValueError("model_mapping contains a model outside the RBAC allowlist")
    return result


def _validate_usage(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) - {"account_ids", "force"}:
        raise ValueError("usage body is invalid")
    ids = body.get("account_ids")
    if not isinstance(ids, list) or len(ids) > 1000:
        raise ValueError("account_ids must be an array of at most 1000 IDs")
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in ids):
        raise ValueError("account_ids must contain positive integers")
    force = body.get("force", False)
    if not isinstance(force, bool):
        raise ValueError("force must be boolean")
    return {"account_ids": list(dict.fromkeys(ids)), "force": force}


def _validate_codex_import(config: ProxyConfig, body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("codex import body must be an object")
    unknown = set(body) - CODEX_IMPORT_FIELDS
    if unknown:
        raise ValueError(f"codex import fields are not allowed: {', '.join(sorted(unknown))}")
    result = dict(body)
    content = result.get("content")
    contents = result.get("contents")
    has_content = isinstance(content, str) and bool(content.strip())
    has_contents = isinstance(contents, list) and bool(contents)
    if has_content == has_contents:
        raise ValueError("codex import must contain exactly one of content or contents")
    if has_content:
        if len(content.encode("utf-8")) > 1024 * 1024:
            raise ValueError("codex import content exceeds the size limit")
    else:
        if len(contents) > 10 or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item.encode("utf-8")) > 1024 * 1024
            for item in contents
        ):
            raise ValueError("codex import contents are invalid")
    for key, limit in (("name", 200), ("notes", 2000)):
        value = result.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise ValueError(f"codex import {key} is invalid")
    requested_groups = result.get("group_ids")
    if requested_groups in (None, []):
        requested_groups = list(config.default_import_group_ids)
    result["group_ids"] = _validate_groups(requested_groups, config.allowed_group_ids)
    if config.ownership_group_id not in result["group_ids"]:
        raise ValueError("codex import must remain in the ownership group")
    concurrency = result.get("concurrency")
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= config.max_concurrency
    ):
        raise ValueError("codex import concurrency exceeds the RBAC limit")
    priority = result.get("priority")
    if priority is not None and (
        isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100000
    ):
        raise ValueError("codex import priority is invalid")
    rate = result.get("rate_multiplier")
    if rate is not None and (
        isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 100
    ):
        raise ValueError("codex import rate_multiplier is invalid")
    load_factor = result.get("load_factor")
    if load_factor is not None and (
        isinstance(load_factor, bool)
        or not isinstance(load_factor, int)
        or not 0 <= load_factor <= 10000
    ):
        raise ValueError("codex import load_factor is invalid")
    expires_at = result.get("expires_at")
    if expires_at is not None and (
        isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= 0
    ):
        raise ValueError("codex import expires_at is invalid")
    if result.get("proxy_id") is not None:
        raise ValueError("codex import cannot select a proxy")
    for key in (
        "auto_pause_on_expired",
        "update_existing",
        "skip_default_group_bind",
        "confirm_mixed_channel_risk",
    ):
        if key in result and not isinstance(result[key], bool):
            raise ValueError(f"codex import {key} must be boolean")
    credential_extras = result.get("credential_extras")
    if credential_extras is not None:
        if not isinstance(credential_extras, dict):
            raise ValueError("codex import credential extras must be an object")
        safe_credential_extras: dict[str, Any] = {}
        if "model_mapping" in credential_extras:
            safe_credential_extras = _validate_credentials(
                {"model_mapping": credential_extras["model_mapping"]},
                config.allowed_models,
            )
        result["credential_extras"] = safe_credential_extras
    extra = result.get("extra")
    if extra is not None:
        if not isinstance(extra, dict):
            raise ValueError("codex import extra must be an object")
        safe_extra = {key: extra[key] for key in EXTRA_FIELDS if key in extra}
        _validate_extra(safe_extra)
        result["extra"] = _with_account_defaults(safe_extra)
    else:
        result["extra"] = _with_account_defaults(None)
    result["skip_default_group_bind"] = True
    result["confirm_mixed_channel_risk"] = True
    return result


def main() -> None:
    config = ProxyConfig.from_env()
    if not config.sub2_admin_key or not config.shared_token:
        raise SystemExit("SUB2_ADMIN_API_KEY and SUB2_RBAC_TOKEN are required")
    if not config.allowed_group_ids or not config.allowed_models:
        raise SystemExit("RBAC group and model allowlists are required")
    if config.ownership_group_id not in config.allowed_group_ids:
        raise SystemExit("RBAC_OWNERSHIP_GROUP_ID must be in RBAC_ALLOWED_GROUP_IDS")
    if (
        not config.default_import_group_ids
        or config.ownership_group_id not in config.default_import_group_ids
        or any(group not in config.allowed_group_ids for group in config.default_import_group_ids)
    ):
        raise SystemExit("RBAC_IMPORT_GROUP_IDS must be allowed and include the ownership group")
    server = RBACServer((config.listen_host, config.listen_port), config)

    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
