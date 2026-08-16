from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from .http_client import HTTPFailure, request_json


FEISHU_HOSTS = {"open.feishu.cn", "open.larksuite.com"}
LEVEL_TEMPLATES = {
    "info": "green",
    "warning": "orange",
    "error": "red",
}
FIELD_LABELS = {
    "account_id": "账号 ID",
    "attempts": "重试次数",
    "available_accounts": "可用账号",
    "balance_fen": "可用余额",
    "charged_fen": "实扣金额",
    "concurrency_max": "并发容量",
    "concurrency_used": "并发占用",
    "error": "错误",
    "estimated_fen": "预计金额",
    "failed": "失败数量",
    "order_id": "订单 ID",
    "quantity": "数量",
    "request_count": "累计请求",
    "released_fen": "退回金额",
    "status": "状态",
    "success": "成功数量",
}


def validate_feishu_webhook_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in FEISHU_HOSTS
        or parsed.port not in (None, 443)
        or not parsed.path.startswith("/open-apis/bot/v2/hook/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("飞书 Webhook 地址格式不正确")
    token = parsed.path.removeprefix("/open-apis/bot/v2/hook/")
    if not token or "/" in token or len(token) > 160:
        raise ValueError("飞书 Webhook 地址格式不正确")
    return url


def feishu_signature(timestamp: int, secret: str) -> str:
    string_to_sign = f"{int(timestamp)}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def build_feishu_payload(
    level: str,
    title: str,
    metadata: dict[str, Any] | None = None,
    *,
    event_type: str = "system",
    timestamp: int | None = None,
    signing_secret: str = "",
) -> dict[str, Any]:
    sent_at = int(time.time() if timestamp is None else timestamp)
    rows = [f"**事件**  {_display(event_type)}"]
    for key, value in list((metadata or {}).items())[:12]:
        if value is None or value == "":
            continue
        label = FIELD_LABELS.get(str(key), _display(str(key)))
        rows.append(f"**{_escape(label)}**  {_format_value(str(key), value)}")
    local_time = datetime.fromtimestamp(sent_at, UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    rows.append(f"**时间**  {_escape(local_time)}")
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": LEVEL_TEMPLATES.get(level, "blue"),
                "title": {"tag": "plain_text", "content": f"Supply Bridge | {title}"[:120]},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "\n".join(rows)[:3500]},
                }
            ],
        },
    }
    if signing_secret:
        payload["timestamp"] = str(sent_at)
        payload["sign"] = feishu_signature(sent_at, signing_secret)
    return payload


class FeishuNotifier:
    def __init__(self) -> None:
        self._last_sent: dict[str, float] = {}
        self._lock = threading.RLock()

    def send(
        self,
        webhook_url: str,
        signing_secret: str,
        level: str,
        title: str,
        metadata: dict[str, Any] | None = None,
        *,
        event_type: str = "system",
        dedup_key: str = "",
        cooldown_seconds: int = 600,
        force: bool = False,
    ) -> bool:
        webhook = validate_feishu_webhook_url(webhook_url)
        if not webhook:
            raise ValueError("尚未配置飞书 Webhook")
        key = dedup_key or f"{event_type}:{level}:{title}"
        now = time.monotonic()
        with self._lock:
            previous = self._last_sent.get(key, 0.0)
            if not force and previous and now - previous < max(10, int(cooldown_seconds)):
                return False
        payload = build_feishu_payload(
            level,
            title,
            metadata,
            event_type=event_type,
            signing_secret=signing_secret,
        )
        response = request_json("POST", webhook, body=payload, timeout=10, max_bytes=512 * 1024)
        data = response.data
        if isinstance(data, dict):
            code = data.get("code", data.get("StatusCode", 0))
            if code not in (0, "0", None):
                message = str(data.get("msg") or data.get("StatusMessage") or "飞书拒绝了通知")
                raise HTTPFailure(502, message[:300], data)
        with self._lock:
            self._last_sent[key] = now
        return True


def _display(value: str) -> str:
    return _escape(value.replace("_", " ").strip() or "system")


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("*", "\\*").replace("`", "\\`")[:600]


def _format_value(key: str, value: Any) -> str:
    if key.endswith("_fen"):
        try:
            return f"¥{float(value) / 100:.2f}"
        except (TypeError, ValueError):
            pass
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list, tuple, set)):
        return _escape(str(value))
    return _escape(value)
