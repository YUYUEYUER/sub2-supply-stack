from __future__ import annotations

import threading
import time
import urllib.parse
import uuid
from typing import Any

from .http_client import HTTPFailure, HTTPResult, build_url, request_json


def _unwrap_sub2(value: Any) -> Any:
    if isinstance(value, dict) and "code" in value:
        if value.get("code") != 0:
            raise HTTPFailure(400, str(value.get("message") or "Sub2 API error"), value)
        return value.get("data")
    return value


class Sub2Client:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any = None,
        idempotency_key: str = "",
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        result = request_json(
            method,
            build_url(self.base_url, path, query),
            headers=headers,
            body=body,
            timeout=self.timeout,
        )
        return _unwrap_sub2(result.data)

    def groups(self) -> list[dict[str, Any]]:
        value = self.request("GET", "/api/v1/admin/groups/all")
        return value if isinstance(value, list) else value.get("items", [])

    def proxy_status(self) -> dict[str, Any]:
        value = self.request("GET", "/proxy/status")
        return value if isinstance(value, dict) else {}

    def capacity(self) -> Any:
        return self.request("GET", "/api/v1/admin/groups/capacity-summary")

    def availability(self, platform: str = "openai", group_id: int = 0) -> dict[str, Any]:
        return self.request(
            "GET",
            "/api/v1/admin/ops/account-availability",
            query={"platform": platform, "group_id": group_id or None},
        )

    def concurrency(self, platform: str = "openai", group_id: int = 0) -> dict[str, Any]:
        return self.request(
            "GET",
            "/api/v1/admin/ops/concurrency",
            query={"platform": platform, "group_id": group_id or None},
        )

    def accounts(self, *, group_id: int = 0, search: str = "", page_size: int = 1000) -> list[dict[str, Any]]:
        data = self.request(
            "GET",
            "/api/v1/admin/accounts",
            query={
                "page": 1,
                "page_size": page_size,
                "platform": "openai",
                "type": "oauth",
                "group": group_id or None,
                "search": search or None,
            },
        )
        if isinstance(data, list):
            return data
        return data.get("items", []) if isinstance(data, dict) else []

    def account(self, account_id: int) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/admin/accounts/{account_id}")

    def batch_usage(self, account_ids: list[int], force: bool = False) -> dict[str, Any]:
        if not account_ids:
            return {"usage": {}, "errors": {}}
        return self.request(
            "POST",
            "/api/v1/admin/accounts/usage/batch",
            body={"account_ids": account_ids, "force": force},
        )

    def create_account(self, account: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/admin/accounts",
            body=account,
            idempotency_key=idempotency_key,
        )

    def update_account(self, account_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", f"/api/v1/admin/accounts/{account_id}", body=changes)

    def test_account(self, account_id: int) -> dict[str, Any]:
        value = self.request("POST", f"/api/v1/admin/accounts/{account_id}/test", body={})
        return value if isinstance(value, dict) else {"success": True, "result": value}

    def set_schedulable(self, account_id: int, enabled: bool) -> dict[str, Any]:
        return self.request(
            "POST", f"/api/v1/admin/accounts/{account_id}/schedulable", body={"schedulable": enabled}
        )


class SupplierClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._token = ""
        self._token_expires = 0.0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password)

    def _login(self) -> str:
        if not self.configured:
            raise HTTPFailure(503, "supplier credentials are not configured")
        result = request_json(
            "POST",
            build_url(self.base_url, "/api/customer/login"),
            body={"account": self.username, "password": self.password},
            timeout=self.timeout,
        ).data
        value = result.get("data", result) if isinstance(result, dict) else {}
        token = (value.get("token") or value.get("customer_token") or "") if isinstance(value, dict) else ""
        if not token:
            raise HTTPFailure(502, "supplier login returned no token", result)
        self._token = str(token)
        # Supplier tokens are valid for 30 days. Refresh one day early.
        self._token_expires = time.monotonic() + 29 * 24 * 60 * 60
        return self._token

    def _get_token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.monotonic() >= self._token_expires:
                return self._login()
            return self._token

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any = None,
        idempotency_key: str = "",
    ) -> Any:
        return self._request_result(
            method,
            build_url(self.base_url, path, query),
            body=body,
            idempotency_key=idempotency_key,
        ).data

    def _request_result(
        self,
        method: str,
        url: str,
        *,
        body: Any = None,
        idempotency_key: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> HTTPResult:
        self._validate_supplier_url(url)
        headers = {"X-Customer-Token": self._get_token()}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if extra_headers:
            headers.update(extra_headers)
        auth_retried = False
        rate_retried = False
        while True:
            try:
                return request_json(
                    method,
                    url,
                    headers=headers,
                    body=body,
                    timeout=self.timeout,
                )
            except HTTPFailure as exc:
                if exc.status == 401 and not auth_retried:
                    auth_retried = True
                    headers["X-Customer-Token"] = self._get_token(force=True)
                    continue
                if exc.status == 429 and not rate_retried:
                    rate_retried = True
                    time.sleep(_retry_after_seconds(exc.headers))
                    continue
                raise

    def _validate_supplier_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        allowed = urllib.parse.urlparse(self.base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != allowed.hostname
            or parsed.port != allowed.port
            or not parsed.path.startswith("/api/customer/")
            or parsed.username
            or parsed.password
        ):
            raise HTTPFailure(400, "supplier URL is not allowed")

    def products(self) -> list[dict[str, Any]]:
        try:
            data = self.request("GET", "/api/customer/products")
        except HTTPFailure as exc:
            if exc.status not in {404, 405}:
                raise
            return [{"product": "team_1h", "enabled": True}]
        value = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(value, dict):
            return value.get("items", value.get("products", []))
        return value if isinstance(value, list) else []

    def balance(self) -> dict[str, Any]:
        data = self.request("GET", "/api/customer/balance")
        return data.get("data", data) if isinstance(data, dict) else {}

    def inventory(self, product: str, quantity: int) -> dict[str, Any]:
        data = self.request(
            "GET", "/api/customer/inventory", query={"product": product, "quantity": quantity}
        )
        return data.get("data", data) if isinstance(data, dict) else {}

    def create_order(self, product: str, quantity: int, idempotency_key: str) -> dict[str, Any]:
        data = self.request(
            "POST",
            "/api/customer/pickup/orders",
            body={"product": product, "quantity": quantity},
            idempotency_key=idempotency_key,
        )
        return data.get("data", data) if isinstance(data, dict) else {}

    def order_status(self, order_id: str) -> dict[str, Any]:
        data = self.request("GET", f"/api/customer/pickup/orders/{urllib.parse.quote(str(order_id))}")
        return data.get("data", data) if isinstance(data, dict) else {}

    def take_order(self, order_id: str) -> dict[str, Any]:
        quoted_id = urllib.parse.quote(str(order_id))
        try:
            result = self._request_result(
                "GET",
                build_url(
                    self.base_url,
                    f"/api/customer/pickup/orders/{quoted_id}/download",
                    {"format": "sub2"},
                ),
                extra_headers={"Accept": "application/json"},
            )
        except HTTPFailure as exc:
            if exc.status not in {404, 405}:
                raise
            result = self._request_result(
                "POST",
                build_url(self.base_url, f"/api/customer/pickup/orders/{quoted_id}/take"),
            )
        data = result.data
        value = data.get("data", data) if isinstance(data, dict) else {}
        if isinstance(value, dict) and result.status == 202:
            value = dict(value)
            value.setdefault("status", "waiting_inventory")
            value["retry_after_seconds"] = _header_seconds(result.headers, "Retry-After", 1)
        return value if isinstance(value, dict) else {}

    def recoveries(self, limit: int = 100, max_pages: int = 20) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        before_id = ""
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            data = self.request(
                "GET",
                "/api/customer/recoveries",
                query={"limit": max(1, min(100, int(limit))), "before_id": before_id or None},
            )
            value = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(value, dict):
                items = value.get("items", value.get("recoveries", []))
                next_before_id = str(
                    value.get("next_before_id")
                    or (data.get("next_before_id") if isinstance(data, dict) else "")
                    or ""
                )
            else:
                items = value if isinstance(value, list) else []
                next_before_id = ""
            output.extend(item for item in items if isinstance(item, dict))
            if not next_before_id or next_before_id in seen_cursors:
                break
            seen_cursors.add(next_before_id)
            before_id = next_before_id
        return output

    def replacement_status(self, status_url: str) -> dict[str, Any]:
        result = self._request_result("GET", status_url)
        return result.data.get("data", result.data) if isinstance(result.data, dict) else {}

    def claim_recovery(
        self, claim_url: str, claim_ticket: str = "", idempotency_key: str = ""
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if claim_ticket:
            headers["X-Recovery-Ticket"] = claim_ticket
        result = self._request_result(
            "POST",
            claim_url,
            idempotency_key=idempotency_key,
            extra_headers=headers,
        )
        return result.data.get("data", result.data) if isinstance(result.data, dict) else {}

    @staticmethod
    def new_idempotency_key(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4()}"


def _retry_after_seconds(headers: dict[str, str]) -> float:
    return float(_header_seconds(headers, "Retry-After", 1))


def _header_seconds(headers: dict[str, str], name: str, default: int) -> int:
    value = next((v for k, v in headers.items() if k.lower() == name.lower()), str(default))
    try:
        return max(1, min(60, int(value)))
    except (TypeError, ValueError):
        return default
