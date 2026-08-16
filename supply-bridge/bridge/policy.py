from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from .notifications import validate_feishu_webhook_url


@dataclass
class Metrics:
    captured_at: str
    total_accounts: int = 0
    available_accounts: int = 0
    rate_limited_accounts: int = 0
    error_accounts: int = 0
    concurrency_used: int = 0
    concurrency_max: int = 0
    concurrency_utilization: float = 0.0
    waiting_in_queue: int = 0
    effective_quota_usd: float = 0.0
    consumption_per_minute: float = 0.0
    planning_rate_usd_per_minute: float = 0.0
    eta_minutes: float | None = None
    supplier_balance_fen: int = 0
    supplier_held_fen: int = 0
    supplier_available_fen: int = 0
    supplier_connected: bool = False
    external_supplier_connected: bool = False
    external_supplier_last_seen_at: str = ""
    external_supplier_last_path: str = ""
    external_supplier_last_status: int = 0
    external_supplier_last_source: str = ""
    external_supplier_request_count: int = 0
    sub2_connected: bool = False
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    should_order: bool
    trigger_type: str = ""
    quantity: int = 0
    reasons: tuple[str, ...] = ()


def decide(settings: dict[str, Any], metrics: Metrics, *, schedule_due: bool = False) -> Decision:
    if not settings.get("auto_enabled", False):
        return Decision(False, reasons=("automation_disabled",))
    if settings.get("emergency_stop", False):
        return Decision(False, reasons=("emergency_stop",))
    if not metrics.sub2_connected:
        return Decision(False, reasons=("sub2_unavailable",))

    reasons: list[str] = []
    quantities: list[int] = []

    if settings.get("replenish_on_empty", True) and metrics.available_accounts <= 0:
        reasons.append("empty_hub")
        quantities.append(int(settings.get("target_available", 1)))

    low = int(settings.get("low_watermark", 0))
    target = max(low, int(settings.get("target_available", low)))
    if settings.get("replenish_on_low_stock", True) and metrics.available_accounts < low:
        reasons.append("low_stock")
        quantities.append(max(1, target - metrics.available_accounts))

    eta = metrics.eta_minutes
    lead = float(settings.get("forecast_lead_minutes", 10))
    if settings.get("replenish_on_eta", True) and eta is not None and eta <= lead:
        reasons.append("eta")
        quantities.append(max(1, target - metrics.available_accounts))

    threshold = float(settings.get("concurrency_threshold_percent", 80)) / 100
    if (
        settings.get("replenish_on_concurrency", True)
        and metrics.concurrency_max > 0
        and metrics.concurrency_utilization >= threshold
    ):
        reasons.append("concurrency")
        per_account = max(1, int(settings.get("account_concurrency", 1)))
        desired_max = math.ceil(metrics.concurrency_used / max(0.01, threshold))
        quantities.append(max(1, math.ceil((desired_max - metrics.concurrency_max) / per_account)))

    if settings.get("replenish_on_schedule", False) and schedule_due:
        reasons.append("schedule")
        quantities.append(max(1, int(settings.get("schedule_quantity", 1))))

    if not reasons:
        return Decision(False, reasons=("thresholds_healthy",))

    minimum = max(1, int(settings.get("min_order_units", 1)))
    maximum = max(minimum, int(settings.get("max_order_units", minimum)))
    quantity = min(maximum, max(minimum, max(quantities or [minimum])))
    trigger = "_or_".join(reasons)
    return Decision(True, trigger_type=trigger, quantity=quantity, reasons=tuple(reasons))


def validate_settings(values: dict[str, Any]) -> dict[str, Any]:
    result = dict(values)
    bool_fields = {
        "auto_enabled",
        "dry_run",
        "emergency_stop",
        "replenish_on_eta",
        "replenish_on_concurrency",
        "replenish_on_empty",
        "replenish_on_low_stock",
        "replenish_on_schedule",
        "openai_passthrough",
        "webhook_enabled",
        "feishu_enabled",
        "feishu_notify_pool",
        "feishu_notify_balance",
        "feishu_notify_orders",
        "feishu_notify_recoveries",
    }
    for field in bool_fields & result.keys():
        if not isinstance(result[field], bool):
            raise ValueError(f"{field} must be boolean")

    ranges = {
        "low_watermark": (0, 10000),
        "target_available": (1, 10000),
        "min_order_units": (1, 100),
        "max_order_units": (1, 100),
        "daily_spend_cap_fen": (0, 100_000_000),
        "cooldown_seconds": (10, 86400),
        "forecast_lead_minutes": (1, 43200),
        "schedule_interval_minutes": (5, 43200),
        "schedule_quantity": (1, 100),
        "concurrency_threshold_percent": (1, 100),
        "account_concurrency": (1, 1000),
        "monitor_group_id": (0, 2_147_483_647),
        "staging_group_id": (0, 2_147_483_647),
        "poll_interval_seconds": (5, 3600),
        "feishu_balance_threshold_fen": (0, 100_000_000),
        "feishu_cooldown_seconds": (10, 86400),
    }
    for field, (minimum, maximum) in ranges.items():
        if field not in result:
            continue
        value = result[field]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")

    if "target_group_ids" in result:
        result["target_group_ids"] = _positive_unique_ints(result["target_group_ids"], "target_group_ids")
    if "products" in result:
        if not isinstance(result["products"], list):
            raise ValueError("products must be an array")
        allowed = {"team_1h", "oauth_30d", "oauth_7d"}
        result["products"] = [str(v) for v in result["products"] if str(v) in allowed]
        if not result["products"]:
            raise ValueError("at least one supported product is required")
    if "models" in result:
        if not isinstance(result["models"], list):
            raise ValueError("models must be an array")
        cleaned = []
        for value in result["models"]:
            model = str(value).strip()
            if not model or len(model) > 128 or not all(c.isalnum() or c in "._-:" for c in model):
                raise ValueError(f"invalid model name: {model}")
            if model not in cleaned:
                cleaned.append(model)
        if not cleaned:
            raise ValueError("at least one model is required")
        result["models"] = cleaned

    if "feishu_webhook_url" in result:
        if not isinstance(result["feishu_webhook_url"], str):
            raise ValueError("feishu_webhook_url must be a string")
        result["feishu_webhook_url"] = validate_feishu_webhook_url(result["feishu_webhook_url"])
    if "feishu_signing_secret" in result:
        secret = result["feishu_signing_secret"]
        if not isinstance(secret, str) or len(secret.strip()) > 256:
            raise ValueError("feishu_signing_secret must be a string up to 256 characters")
        result["feishu_signing_secret"] = secret.strip()

    minimum = int(result.get("min_order_units", 1))
    maximum = int(result.get("max_order_units", minimum))
    if maximum < minimum:
        raise ValueError("max_order_units must be at least min_order_units")
    low = int(result.get("low_watermark", 0))
    target = int(result.get("target_available", max(1, low)))
    if target < low:
        raise ValueError("target_available must be at least low_watermark")
    return result


def schedule_is_due(settings: dict[str, Any], latest_order_at: str | None, now: datetime | None = None) -> bool:
    if not settings.get("replenish_on_schedule", False):
        return False
    now = now or datetime.now(UTC)
    if not latest_order_at:
        return True
    try:
        previous = datetime.fromisoformat(latest_order_at)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=UTC)
    except ValueError:
        return True
    return (now - previous).total_seconds() >= int(settings.get("schedule_interval_minutes", 60)) * 60


def _positive_unique_ints(value: Any, field: str) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ValueError(f"{field} must contain positive integers")
        if item not in result:
            result.append(item)
    return result
