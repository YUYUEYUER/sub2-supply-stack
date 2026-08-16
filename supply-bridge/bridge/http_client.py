from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class HTTPResult:
    status: int
    headers: dict[str, str]
    data: Any


class HTTPFailure(RuntimeError):
    def __init__(
        self,
        status: int,
        message: str,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.data = data
        self.headers = headers or {}


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: int = 30,
    max_bytes: int = 16 * 1024 * 1024,
) -> HTTPResult:
    request_headers = {"Accept": "application/json", "User-Agent": "Sub2-Supply-Bridge/1.0"}
    request_headers.update(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=payload, headers=request_headers, method=method.upper())
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as response:
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise HTTPFailure(502, "upstream response exceeded size limit")
            data = _decode(raw, response.headers.get("Content-Type", ""))
            return HTTPResult(response.status, dict(response.headers.items()), data)
    except urllib.error.HTTPError as exc:
        raw = exc.read(max_bytes)
        data = _decode(raw, exc.headers.get("Content-Type", ""))
        message = _error_message(data) or f"HTTP {exc.code}"
        raise HTTPFailure(exc.code, message, data, dict(exc.headers.items())) from exc
    except urllib.error.URLError as exc:
        raise HTTPFailure(0, f"network error: {exc.reason}") from exc


def build_url(base: str, path: str, query: dict[str, Any] | None = None) -> str:
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    if query:
        normalized = {k: v for k, v in query.items() if v is not None and v != ""}
        if normalized:
            url = f"{url}?{urllib.parse.urlencode(normalized, doseq=True)}"
    return url


def _decode(raw: bytes, content_type: str) -> Any:
    if not raw:
        return None
    if "json" in content_type.lower() or raw[:1] in (b"{", b"["):
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return raw.decode("utf-8", errors="replace")


def _error_message(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("message", "error", "detail", "reason"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return ""
