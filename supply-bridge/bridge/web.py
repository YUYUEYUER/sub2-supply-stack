from __future__ import annotations

import hmac
import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .engine import BridgeEngine
from .http_client import HTTPFailure
from .policy import validate_settings
from .store import Store


STATIC_ROOT = Path(__file__).with_name("static")
ASSETS = {
    "/": "index.html",
    "/index.html": "index.html",
    "/style.css": "style.css",
    "/app.js": "app.js",
}


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        admin_token: str,
        store: Store,
        engine: BridgeEngine,
    ):
        self.admin_token = admin_token
        self.store = store
        self.engine = engine
        super().__init__(address, WebHandler)


class WebHandler(BaseHTTPRequestHandler):
    server: WebServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"status": "ok", "version": "1.0.21"})
            return
        if self.path.split("?", 1)[0] in ASSETS:
            self._static(self.path.split("?", 1)[0])
            return
        self._api()

    def do_POST(self) -> None:  # noqa: N802
        self._api()

    def do_PUT(self) -> None:  # noqa: N802
        self._api()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._json(405, {"error": "cross_origin_requests_are_not_supported"})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _api(self) -> None:
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            self._json(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            if self.command == "GET":
                payload = self._get(path)
            elif self.command in {"POST", "PUT"}:
                payload = self._mutate(path, self._read_json())
            else:
                self._json(405, {"error": "method_not_allowed"})
                return
            self._json(200, payload)
        except HTTPFailure as exc:
            self._json(exc.status if 400 <= exc.status <= 599 else 502, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self.server.store.event("error", "api_error", "Bridge API request failed", {"error": str(exc)[:300]})
            self._json(500, {"error": "internal_server_error"})

    def _get(self, path: str) -> Any:
        if path == "/api/status":
            return _clean_status(self.server.engine.status())
        if path == "/api/settings":
            return _clean_settings(
                self.server.store.settings(),
                self.server.engine.notification_status(),
            )
        if path == "/api/orders":
            return [_without_raw(row) for row in self.server.store.list_orders(200)]
        if path == "/api/deliveries":
            return self.server.store.list_deliveries(500)
        if path == "/api/recoveries":
            return [_without_raw(row) for row in self.server.store.list_recoveries(200)]
        if path == "/api/events":
            return self.server.store.list_events(300)
        if path == "/api/groups":
            return [
                {
                    "id": int(group.get("id", 0)),
                    "name": str(group.get("name", "")),
                    "platform": str(group.get("platform", "")),
                    "is_exclusive": bool(group.get("is_exclusive", False)),
                }
                for group in self.server.engine.sub2.groups()
                if int(group.get("id", 0)) > 0
            ]
        raise HTTPFailure(404, "not_found")

    def _mutate(self, path: str, body: Any) -> Any:
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        if self.command == "PUT" and path == "/api/settings":
            candidate = dict(body)
            for field in ("feishu_webhook_url", "feishu_signing_secret"):
                if field in candidate and not str(candidate[field] or "").strip():
                    candidate.pop(field)
            values = validate_settings(candidate)
            self._validate_groups(values)
            merged = {**self.server.store.settings(), **values}
            if values.get("feishu_enabled") and not (
                merged.get("feishu_webhook_url")
                or self.server.engine.config.notification_webhook_url
            ):
                raise ValueError("开启飞书通知前请先填写 Webhook")
            result = self.server.store.update_settings(values)
            self.server.store.event("info", "settings_updated", "Automation settings updated", {"fields": sorted(values)})
            self.server.engine.wake()
            return _clean_settings(result, self.server.engine.notification_status(result))
        if self.command == "POST" and path == "/api/notifications/test":
            return self.server.engine.test_notification()
        if self.command == "POST" and path == "/api/notifications/clear":
            result = self.server.store.update_settings(
                {
                    "feishu_enabled": False,
                    "webhook_enabled": False,
                    "feishu_webhook_url": "",
                    "feishu_signing_secret": "",
                }
            )
            self.server.store.event("warning", "notification_credentials_cleared", "飞书通知凭据已清除")
            return _clean_settings(result, self.server.engine.notification_status(result))
        if self.command == "POST" and path == "/api/actions/run":
            self.server.store.event("info", "manual_poll", "Manual poll requested")
            return self.server.engine.tick()
        if self.command == "POST" and path == "/api/actions/emergency-stop":
            return self.server.engine.emergency_stop(True)
        if self.command == "POST" and path == "/api/actions/resume":
            return self.server.engine.emergency_stop(False)
        if self.command == "POST" and path == "/api/actions/manual-order":
            product = str(body.get("product", ""))
            quantity = body.get("quantity", 0)
            dry_run = body.get("dry_run")
            if dry_run is not None and not isinstance(dry_run, bool):
                raise ValueError("dry_run must be boolean")
            self.server.store.event(
                "warning" if dry_run is False else "info",
                "manual_order_requested",
                "Manual supplier order requested",
                {"product": product, "quantity": quantity, "dry_run": dry_run},
            )
            return _without_raw(self.server.engine.manual_order(product, int(quantity), dry_run=dry_run))
        if self.command == "POST" and path == "/api/actions/take-order":
            order_id = str(body.get("order_id", ""))
            if not re.fullmatch(r"[0-9a-f-]{36}", order_id):
                raise ValueError("invalid order_id")
            return _without_raw(self.server.engine.take_order(order_id))
        if self.command == "POST" and path == "/api/actions/retry-delivery":
            delivery_id = body.get("delivery_id")
            if isinstance(delivery_id, bool) or not isinstance(delivery_id, int) or delivery_id <= 0:
                raise ValueError("invalid delivery_id")
            return self.server.engine.retry_delivery(delivery_id)
        raise HTTPFailure(404, "not_found")

    def _validate_groups(self, values: dict[str, Any]) -> None:
        fields = {"monitor_group_id", "staging_group_id", "target_group_ids"}
        if not fields.intersection(values):
            return
        group_ids = {int(group.get("id", 0)) for group in self.server.engine.sub2.groups()}
        requested = {
            int(values.get("monitor_group_id", 0)),
            int(values.get("staging_group_id", 0)),
            *(int(value) for value in values.get("target_group_ids", [])),
        }
        invalid = {value for value in requested if value and value not in group_ids}
        if invalid:
            raise ValueError("one or more selected groups do not exist")

    def _authorized(self) -> bool:
        token = self.headers.get("X-Bridge-Token", "")
        return bool(self.server.admin_token) and hmac.compare_digest(token, self.server.admin_token)

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0 or length > 256 * 1024:
            raise ValueError("request body exceeded size limit")
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc

    def _static(self, path: str) -> None:
        target = STATIC_ROOT / ASSETS[path]
        try:
            raw = target.read_bytes()
        except FileNotFoundError:
            self._json(404, {"error": "not_found"})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")


def _without_raw(value: dict[str, Any]) -> dict[str, Any]:
    clean = dict(value)
    clean.pop("raw", None)
    clean.pop("raw_json", None)
    return clean


def _clean_status(value: dict[str, Any]) -> dict[str, Any]:
    clean = dict(value)
    clean["active_orders"] = [_without_raw(row) for row in value.get("active_orders", [])]
    clean["settings"] = _clean_settings(value.get("settings", {}), value.get("notification", {}))
    return clean


def _clean_settings(values: dict[str, Any], notification: dict[str, Any] | None = None) -> dict[str, Any]:
    clean = dict(values)
    clean.pop("feishu_webhook_url", None)
    clean.pop("feishu_signing_secret", None)
    status = notification or {}
    clean["feishu_webhook_configured"] = bool(status.get("webhook_configured"))
    clean["feishu_signing_secret_configured"] = bool(status.get("signing_secret_configured"))
    return clean
