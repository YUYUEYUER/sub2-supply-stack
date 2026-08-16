from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from .clients import Sub2Client, SupplierClient
from .config import AppConfig
from .http_client import HTTPFailure
from .notifications import FeishuNotifier
from .policy import Decision, Metrics, decide, schedule_is_due
from .store import Store, utcnow


TERMINAL_ORDER_STATES = {"completed", "partial", "cancelled", "failed", "dry_run"}
READY_ORDER_STATES = {"ready", "ready_partial"}
WAITING_ORDER_STATES = {"queued", "pending", "processing", "waiting_inventory", "preparing", "delivering"}


class BridgeEngine:
    def __init__(self, config: AppConfig, store: Store, sub2: Sub2Client, supplier: SupplierClient):
        self.config = config
        self.store = store
        self.sub2 = sub2
        self.supplier = supplier
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._tick_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._latest_metrics = Metrics(captured_at=utcnow())
        self._last_tick_at = ""
        self._last_tick_error = ""
        self._thread: threading.Thread | None = None
        self._notifier = FeishuNotifier()
        self._health_states: dict[str, bool] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="bridge-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        self.store.event("info", "engine_started", "补货引擎已启动")
        while not self._stop.is_set():
            settings = self.store.settings()
            interval = max(5, int(settings.get("poll_interval_seconds", self.config.poll_interval_seconds)))
            try:
                self.tick()
            except Exception as exc:
                self._last_tick_error = _safe_error(exc)
                self.store.event("error", "tick_failed", "补货轮询失败", {"error": self._last_tick_error})
            self._wake.wait(interval)
            self._wake.clear()
        self.store.event("info", "engine_stopped", "补货引擎已停止")

    def tick(self) -> dict[str, Any]:
        if not self._tick_lock.acquire(blocking=False):
            return {"skipped": "tick_in_progress"}
        try:
            settings = self.store.settings()
            self._poll_orders(settings)
            self._poll_account_health(settings)
            self._poll_recoveries(settings)
            metrics = self._collect_metrics(settings)
            self._monitor_health(settings, metrics)
            self.store.add_snapshot(metrics.to_dict())
            with self._state_lock:
                self._latest_metrics = metrics
                self._last_tick_at = utcnow()
                self._last_tick_error = metrics.last_error

            active = self.store.active_orders()
            if active:
                return {"metrics": metrics.to_dict(), "active_orders": len(active)}

            orders = self.store.list_orders(1)
            latest_order_at = orders[0]["created_at"] if orders else None
            decision = decide(
                settings,
                metrics,
                schedule_due=schedule_is_due(settings, latest_order_at),
            )
            if decision.should_order:
                if not self.supplier.configured:
                    return {
                        "metrics": metrics.to_dict(),
                        "decision": decision.__dict__,
                        "blocked": "supplier_credentials_not_configured",
                    }
                self._create_order_from_decision(settings, metrics, decision)
            return {"metrics": metrics.to_dict(), "decision": decision.__dict__}
        finally:
            self._tick_lock.release()

    def status(self) -> dict[str, Any]:
        settings = self.store.settings()
        with self._state_lock:
            metrics = self._latest_metrics.to_dict()
            last_tick_at = self._last_tick_at
            last_error = self._last_tick_error
        return {
                "version": "1.0.22",
            "metrics": metrics,
            "settings": settings,
            "last_tick_at": last_tick_at,
            "last_error": last_error,
            "supplier_configured": self.supplier.configured,
            "active_orders": self.store.active_orders(),
            "daily_spend_fen": self.store.daily_spend_fen(),
            "notification": self.notification_status(settings),
        }

    def notification_status(self, settings: dict[str, Any] | None = None) -> dict[str, bool]:
        values = settings or self.store.settings()
        webhook = str(values.get("feishu_webhook_url") or self.config.notification_webhook_url or "")
        secret = str(values.get("feishu_signing_secret") or self.config.notification_signing_secret or "")
        return {
            "enabled": bool(values.get("feishu_enabled") or values.get("webhook_enabled")),
            "webhook_configured": bool(webhook),
            "signing_secret_configured": bool(secret),
        }

    def test_notification(self) -> dict[str, Any]:
        settings = self.store.settings()
        webhook, secret = self._notification_credentials(settings)
        sent = self._notifier.send(
            webhook,
            secret,
            "info",
            "飞书通知测试成功",
            {
                "available_accounts": self._latest_metrics.available_accounts,
                "status": "自动运维通知链路正常",
            },
            event_type="notification_test",
            dedup_key=f"notification_test:{uuid.uuid4()}",
            force=True,
        )
        self.store.event("info", "notification_test_sent", "飞书测试通知已发送")
        return {"sent": sent, "message": "飞书测试通知已发送"}

    def manual_order(self, product: str, quantity: int, *, dry_run: bool | None = None) -> dict[str, Any]:
        settings = self.store.settings()
        if product not in {"team_1h", "oauth_7d", "oauth_30d"}:
            raise ValueError("unsupported product")
        quantity = int(quantity)
        if quantity < 1 or quantity > int(settings.get("max_order_units", 5)):
            raise ValueError("quantity exceeds configured order limit")
        if dry_run is not None:
            settings["dry_run"] = bool(dry_run)
        metrics = self._latest_metrics
        decision = Decision(True, "manual", quantity, ("manual",))
        return self._create_order_from_decision(settings, metrics, decision, product_override=product)

    def emergency_stop(self, enabled: bool) -> dict[str, Any]:
        settings = self.store.update_settings({"emergency_stop": bool(enabled)})
        self.store.event(
            "warning" if enabled else "info",
            "emergency_stop_changed",
            "紧急停止已开启" if enabled else "紧急停止已解除",
        )
        self.wake()
        return settings

    def take_order(self, order_id: str) -> dict[str, Any]:
        order = self.store.order(order_id)
        if not order or not order.get("supplier_order_id"):
            raise ValueError("order is not available for pickup")
        if order.get("status") in TERMINAL_ORDER_STATES:
            raise ValueError("order is already complete")
        status_response = self.supplier.order_status(str(order["supplier_order_id"]))
        payload = status_response.get("order", status_response) if isinstance(status_response, dict) else {}
        status = str(
            payload.get("state")
            or payload.get("status")
            or status_response.get("state")
            or status_response.get("status")
            or order["status"]
        )
        if status not in READY_ORDER_STATES and status != "completed":
            self.store.update_order(order_id, status=status)
            return {"status": status, "ready": False}
        response = self.supplier.take_order(str(order["supplier_order_id"]))
        self._fulfill_order(order, response, self.store.settings())
        self.store.event("info", "manual_pickup", "Supplier order picked up manually", {"order_id": order_id})
        return self.store.order(order_id) or {"id": order_id}

    def retry_delivery(self, delivery_id: int) -> dict[str, Any]:
        delivery = self.store.delivery(delivery_id)
        if not delivery or not delivery.get("sub2_account_id"):
            raise ValueError("delivery is not available for retry")
        account_id = int(delivery["sub2_account_id"])
        settings = self.store.settings()
        test = self.sub2.test_account(account_id)
        if not _test_success(test):
            attempts = int(delivery.get("attempts", 0)) + 1
            self.sub2.set_schedulable(account_id, False)
            self.store.update_delivery(
                delivery_id,
                status="quarantined",
                attempts=attempts,
                last_error=str(test.get("message") or test.get("error") or "account test failed")[:500],
            )
            raise RuntimeError("account test failed")
        target_groups = _unique_positive(
            [int(settings.get("monitor_group_id", 0)), *list(settings.get("target_group_ids", []))]
        )
        if not target_groups:
            raise ValueError("target groups are not configured")
        self.sub2.update_account(account_id, {"group_ids": target_groups, "status": "active"})
        self.sub2.set_schedulable(account_id, True)
        self.store.update_delivery(delivery_id, status="active", attempts=0, last_error="")
        self.store.event("info", "delivery_retried", "Account delivery retried manually", {"delivery_id": delivery_id})
        return self.store.delivery(delivery_id) or delivery

    def _collect_metrics(self, settings: dict[str, Any]) -> Metrics:
        metrics = Metrics(captured_at=utcnow())
        errors: list[str] = []
        group_id = int(settings.get("monitor_group_id", 0))
        try:
            availability = self.sub2.availability("openai", group_id)
            concurrency = self.sub2.concurrency("openai", group_id)
            metrics.sub2_connected = True
            a = _select_metric(availability, "group", str(group_id)) if group_id else None
            a = a or _select_metric(availability, "platform", "openai") or {}
            metrics.total_accounts = _int(a, "total_accounts")
            metrics.available_accounts = _int(a, "available_count")
            metrics.rate_limited_accounts = _int(a, "rate_limit_count")
            metrics.error_accounts = _int(a, "error_count")

            c = _select_metric(concurrency, "group", str(group_id)) if group_id else None
            c = c or _select_metric(concurrency, "platform", "openai") or {}
            metrics.concurrency_used = _int(c, "current_in_use")
            metrics.concurrency_max = _int(c, "max_capacity")
            metrics.waiting_in_queue = _int(c, "waiting_in_queue")
            if metrics.concurrency_max > 0:
                metrics.concurrency_utilization = metrics.concurrency_used / metrics.concurrency_max
            else:
                raw = float(c.get("load_percentage", 0) or 0)
                metrics.concurrency_utilization = raw / 100 if raw > 1 else raw
        except Exception as exc:
            errors.append(f"Sub2: {_safe_error(exc)}")

        try:
            proxy = self.sub2.proxy_status()
            metrics.external_supplier_connected = bool(proxy.get("external_connected"))
            metrics.external_supplier_last_seen_at = str(proxy.get("external_last_seen_at") or "")
            metrics.external_supplier_last_path = str(proxy.get("external_last_path") or "")
            metrics.external_supplier_last_status = _int(proxy, "external_last_status")
            metrics.external_supplier_last_source = str(proxy.get("external_last_source") or "")
            metrics.external_supplier_request_count = _int(proxy, "external_request_count")
        except Exception as exc:
            errors.append(f"External push: {_safe_error(exc)}")

        if self.supplier.configured:
            try:
                balance = self.supplier.balance()
                metrics.supplier_connected = True
                metrics.supplier_balance_fen = _int(balance, "balance_fen")
                metrics.supplier_held_fen = _int(balance, "held_fen")
                metrics.supplier_available_fen = _int(
                    balance,
                    "available_fen",
                    default=max(0, metrics.supplier_balance_fen - metrics.supplier_held_fen),
                )
            except Exception as exc:
                errors.append(f"供应商: {_safe_error(exc)}")

        try:
            effective = self._effective_quota()
            metrics.effective_quota_usd = effective
            rate = self._estimate_consumption_rate(effective)
            metrics.consumption_per_minute = rate
            metrics.planning_rate_usd_per_minute = rate * 1.2 if rate > 0 else 0
            if metrics.planning_rate_usd_per_minute > 0:
                metrics.eta_minutes = effective / metrics.planning_rate_usd_per_minute
        except Exception as exc:
            errors.append(f"额度: {_safe_error(exc)}")

        metrics.last_error = " | ".join(errors)
        return metrics

    def _effective_quota(self) -> float:
        deliveries = [
            d for d in self.store.list_deliveries(1000) if d.get("sub2_account_id") and d.get("status") == "active"
        ]
        if not deliveries:
            return 0.0
        ids = [int(d["sub2_account_id"]) for d in deliveries]
        response = self.sub2.batch_usage(ids)
        usage_map = response.get("usage", {}) if isinstance(response, dict) else {}
        total = 0.0
        now = datetime.now(UTC)
        for delivery in deliveries:
            quota = max(0.0, float(delivery.get("quota_usd") or 0))
            info = usage_map.get(str(delivery["sub2_account_id"])) or usage_map.get(delivery["sub2_account_id"]) or {}
            progress = info.get("five_hour") or info.get("seven_day") or {}
            utilization = float(progress.get("utilization") or 0)
            remaining = quota * max(0.0, 1 - utilization / 100)
            expires_at = delivery.get("expires_at")
            if expires_at:
                try:
                    expiry = datetime.fromisoformat(str(expires_at))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=UTC)
                    if expiry <= now:
                        remaining = 0.0
                except ValueError:
                    pass
            total += remaining
        return round(total, 6)

    def _estimate_consumption_rate(self, current_quota: float) -> float:
        snapshots = self.store.latest_snapshots(180)
        now = datetime.now(UTC)
        rates: list[float] = []
        for snap in snapshots:
            previous = float(snap.get("effective_quota_usd") or 0)
            if previous <= current_quota:
                continue
            try:
                captured = datetime.fromisoformat(snap["captured_at"])
                if captured.tzinfo is None:
                    captured = captured.replace(tzinfo=UTC)
            except (KeyError, ValueError):
                continue
            minutes = (now - captured).total_seconds() / 60
            if 1 <= minutes <= 60:
                rates.append((previous - current_quota) / minutes)
        if not rates:
            return 0.0
        rates.sort()
        median = rates[len(rates) // 2]
        return round(max(0.0, median), 6)

    def _create_order_from_decision(
        self,
        settings: dict[str, Any],
        metrics: Metrics,
        decision: Decision,
        *,
        product_override: str = "",
    ) -> dict[str, Any]:
        if self.store.active_orders():
            raise RuntimeError("an order is already active")
        if settings.get("emergency_stop"):
            raise RuntimeError("emergency stop is enabled")
        product, quote, product_info = self._select_product(
            settings, decision.quantity, product_override=product_override
        )
        unit_fen = _int(quote, "estimated_unit_price_fen", default=_int(product_info, "price_fen"))
        estimated = _int(quote, "estimated_total_fen", default=unit_fen * decision.quantity)
        daily_spend = self.store.daily_spend_fen()
        cap = int(settings.get("daily_spend_cap_fen", 0))
        if cap > 0 and daily_spend + estimated > cap:
            remaining = max(0, cap - daily_spend)
            allowed = remaining // max(1, unit_fen)
            if allowed < int(settings.get("min_order_units", 1)):
                self.store.event("warning", "spend_cap_blocked", "每日费用上限阻止了补货", {"estimated_fen": estimated})
                return {"blocked": "daily_spend_cap"}
            decision.quantity = min(decision.quantity, allowed)
            quote = self.supplier.inventory(product, decision.quantity)
            estimated = _int(quote, "estimated_total_fen", default=unit_fen * decision.quantity)

        if not settings.get("dry_run") and metrics.supplier_available_fen < estimated:
            self.store.event("warning", "balance_blocked", "供应商余额不足", {"estimated_fen": estimated})
            self._notify(
                "warning",
                "供应商余额不足",
                {"estimated_fen": estimated, "balance_fen": metrics.supplier_available_fen},
                event_type="balance_blocked",
                category="balance",
                dedup_key="balance_blocked",
            )
            return {"blocked": "insufficient_balance"}

        local_id = str(uuid.uuid4())
        idem = f"sub2-bridge-{local_id}"
        now = utcnow()
        order = {
            "id": local_id,
            "supplier_order_id": None,
            "product": product,
            "quantity": decision.quantity,
            "status": "dry_run" if settings.get("dry_run") else "creating",
            "trigger_type": decision.trigger_type,
            "estimated_fen": estimated,
            "charged_fen": 0,
            "released_fen": 0,
            "idempotency_key": idem,
            "attempts": 0,
            "last_error": "",
            "raw_json": json.dumps({"quote": quote, "product": product_info}, ensure_ascii=True),
            "created_at": now,
            "updated_at": now,
        }
        self.store.upsert_order(order)
        if settings.get("dry_run"):
            self.store.event(
                "info",
                "dry_run_order",
                "模拟补货计划已生成",
                {"product": product, "quantity": decision.quantity, "trigger": decision.trigger_type},
            )
            return self.store.order(local_id) or order

        try:
            response = self.supplier.create_order(product, decision.quantity, idem)
            supplier_order = response.get("order", response) if isinstance(response, dict) else {}
            supplier_id = supplier_order.get("id") or response.get("order_id")
            if not supplier_id:
                raise HTTPFailure(502, "supplier order response has no order id", response)
            status = str(supplier_order.get("status") or response.get("status") or "queued")
            order["supplier_order_id"] = str(supplier_id)
            order["status"] = status
            order["raw_json"] = json.dumps(
                {"quote": quote, "product": product_info, "supplier": _redact_supplier_payload(response)},
                ensure_ascii=True,
            )
            order["updated_at"] = utcnow()
            self.store.upsert_order(order)
            self.store.event(
                "info",
                "order_created",
                "自动补货订单已创建",
                {"order_id": local_id, "supplier_order_id": str(supplier_id), "quantity": decision.quantity},
            )
            return self.store.order(local_id) or order
        except Exception as exc:
            self.store.update_order(local_id, status="failed", attempts=1, last_error=_safe_error(exc))
            self.store.event("error", "order_create_failed", "自动补货下单失败", {"error": _safe_error(exc)})
            self._notify(
                "error",
                "自动补货下单失败",
                {"order_id": local_id, "error": _safe_error(exc)},
                event_type="order_create_failed",
                category="orders",
                dedup_key=f"order_create_failed:{local_id}",
            )
            raise

    def _select_product(
        self, settings: dict[str, Any], quantity: int, *, product_override: str = ""
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        if not self.supplier.configured:
            raise HTTPFailure(503, "supplier credentials are not configured")
        products = self.supplier.products()
        product_map = {str(p.get("product") or p.get("id")): p for p in products}
        candidates = [product_override] if product_override else list(settings.get("products", []))
        last_error: Exception | None = None
        for product in candidates:
            if not product:
                continue
            info = product_map.get(product, {"product": product})
            if info.get("enabled") is False or info.get("available") is False:
                continue
            try:
                quote = self.supplier.inventory(product, quantity)
                if quote.get("product_available") is False:
                    continue
                return product, quote, info
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise HTTPFailure(409, "no configured supplier product is currently available")

    def _poll_orders(self, settings: dict[str, Any]) -> None:
        for order in self.store.active_orders():
            supplier_id = order.get("supplier_order_id")
            if not supplier_id:
                self.store.update_order(order["id"], status="failed", last_error="missing supplier order id")
                continue
            try:
                response = self.supplier.order_status(str(supplier_id))
                payload = response.get("order", response) if isinstance(response, dict) else {}
                status = str(
                    payload.get("state")
                    or payload.get("status")
                    or response.get("state")
                    or response.get("status")
                    or order["status"]
                )
                charged = _int(payload, "charged_fen", default=_int(response, "charged_fen"))
                released = _int(payload, "released_fen", default=_int(response, "released_fen"))
                merged_raw = dict(order.get("raw") or {})
                merged_raw["supplier"] = _redact_supplier_payload(response)
                delivery_ready = status in READY_ORDER_STATES or status == "completed"
                stored_status = "ready" if status == "completed" else status
                self.store.update_order(
                    order["id"],
                    status=stored_status,
                    charged_fen=charged,
                    released_fen=released,
                    raw_json=json.dumps(merged_raw, ensure_ascii=True),
                    attempts=int(order.get("attempts", 0)),
                    last_error="",
                )
                # The supplier may attach reauthorization files to status responses
                # after the initial payload has already been delivered.
                self._capture_replacement_files(order, response, settings)
                if delivery_ready:
                    taken = self.supplier.take_order(str(supplier_id))
                    self._fulfill_order(self.store.order(order["id"]) or order, taken, settings)
                elif status in TERMINAL_ORDER_STATES:
                    continue
                elif status not in WAITING_ORDER_STATES:
                    self.store.event("warning", "unknown_order_status", "供应商返回未知订单状态", {"status": status})
            except Exception as exc:
                attempts = int(order.get("attempts", 0)) + 1
                self.store.update_order(order["id"], attempts=attempts, last_error=_safe_error(exc))
                if attempts in {1, 3, 10}:
                    self.store.event("warning", "order_poll_failed", "订单状态查询失败，将自动重试", {"error": _safe_error(exc)})
                if attempts in {3, 10}:
                    self._notify(
                        "warning",
                        "订单状态查询持续失败",
                        {"order_id": order["id"], "attempts": attempts, "error": _safe_error(exc)},
                        event_type="order_poll_failed",
                        category="orders",
                        dedup_key=f"order_poll_failed:{order['id']}:{attempts}",
                    )

    def _fulfill_order(self, order: dict[str, Any], response: dict[str, Any], settings: dict[str, Any]) -> None:
        accounts = normalize_accounts(response)
        if not accounts:
            self._capture_replacement_files(order, response, settings)
            status = str(response.get("status") or "waiting_inventory")
            merged_raw = dict(order.get("raw") or {})
            merged_raw["supplier"] = _redact_supplier_payload(response)
            self.store.update_order(order["id"], status=status, raw_json=json.dumps(merged_raw, ensure_ascii=True))
            return
        raw = order.get("raw", {})
        quota = float(((raw.get("product") or {}).get("quota_usd") or 0))
        success = 0
        failed = 0
        for account in accounts:
            try:
                if self._import_account(order, account, settings, quota):
                    success += 1
            except Exception as exc:
                failed += 1
                self.store.event("error", "delivery_failed", "账号导入或验货失败", {"error": _safe_error(exc)})
                self._notify(
                    "error",
                    "账号导入或验货失败",
                    {"order_id": order["id"], "error": _safe_error(exc)},
                    event_type="delivery_failed",
                    category="orders",
                    dedup_key=f"delivery_failed:{order['id']}:{failed}",
                )
        self._capture_replacement_files(order, response, settings)
        final_status = "completed" if success and not failed else "partial" if success else "failed"
        settlement = response.get("order", response) if isinstance(response, dict) else {}
        charged = _int(
            settlement,
            "charged_fen",
            default=_int(response, "charged_fen", default=int(order.get("charged_fen", 0))),
        )
        released = _int(
            settlement,
            "released_fen",
            default=_int(response, "released_fen", default=int(order.get("released_fen", 0))),
        )
        self.store.update_order(
            order["id"],
            status=final_status,
            charged_fen=charged,
            released_fen=released,
            raw_json=json.dumps(
                {
                    "product": raw.get("product", {}),
                    "quote": raw.get("quote", {}),
                    "supplier": _redact_supplier_payload(response),
                    "delivery_summary": {"success": success, "failed": failed, "received": len(accounts)},
                },
                ensure_ascii=True,
            ),
            last_error="" if success else "no account passed validation",
        )
        self.store.event(
            "info" if final_status == "completed" else "warning",
            "order_fulfilled",
            "补货订单处理完成",
            {"success": success, "failed": failed, "status": final_status},
        )
        self._notify(
            "info" if final_status == "completed" else "warning",
            "补货订单处理完成",
            {
                "order_id": order["id"],
                "success": success,
                "failed": failed,
                "charged_fen": charged,
                "released_fen": released,
                "status": final_status,
            },
            event_type="order_fulfilled",
            category="orders",
            dedup_key=f"order_fulfilled:{order['id']}",
        )
        if released > 0:
            self._notify(
                "info",
                "供应商退款已入账",
                {"order_id": order["id"], "released_fen": released},
                event_type="refund_received",
                category="recoveries",
                dedup_key=f"refund_received:{order['id']}:{released}",
            )

    def _import_account(
        self, order: dict[str, Any], account: dict[str, Any], settings: dict[str, Any], quota_usd: float
    ) -> bool:
        validate_supplier_account(account)
        fingerprint = account_fingerprint(account)
        existing = self.store.delivery_by_fingerprint(fingerprint)
        if existing and existing.get("status") == "active":
            return True
        name = str(account.get("name") or account.get("email") or f"supply-{fingerprint[:12]}")[:200]
        delivery_id = existing.get("id") if existing else self.store.add_delivery(
            {
                "order_id": order["id"],
                "supplier_ref": str(account.get("supplier_ref") or account.get("id") or ""),
                "account_name": name,
                "fingerprint": fingerprint,
                "sub2_account_id": None,
                "quota_usd": quota_usd,
                "credential_version": _credential_version(account),
                "expires_at": _iso_expiry(account.get("expires_at")),
                "status": "validating",
                "attempts": 0,
                "last_error": "",
            }
        )
        credentials = dict(account["credentials"])
        models = list(settings.get("models", []))
        credentials["model_mapping"] = {model: model for model in models}
        extra = _sub2_openai_extra(settings)
        stage_groups = _unique_positive(
            [int(settings.get("staging_group_id", 0)), int(settings.get("monitor_group_id", 0))]
        )
        target_groups = _unique_positive(
            [int(settings.get("monitor_group_id", 0)), *list(settings.get("target_group_ids", []))]
        )
        if not stage_groups or not target_groups:
            raise ValueError("staging, monitoring and target groups must be configured")
        payload = {
            "name": name,
            "notes": f"Supply Bridge order {order['id']}",
            "platform": "openai",
            "type": "oauth",
            "credentials": credentials,
            "extra": extra,
            "concurrency": int(settings.get("account_concurrency", 30)),
            "priority": int(account.get("priority") or 0),
            "group_ids": stage_groups,
            "expires_at": _unix_expiry(account.get("expires_at")),
            "auto_pause_on_expired": True,
            "confirm_mixed_channel_risk": True,
        }
        try:
            created = self.sub2.create_account(payload, f"bridge-account-{order['id']}-{fingerprint}")
            account_id = int(created["id"])
            self.store.update_delivery(delivery_id, sub2_account_id=account_id, status="testing")
            test = self.sub2.test_account(account_id)
            if not _test_success(test):
                self.sub2.set_schedulable(account_id, False)
                self.store.update_delivery(
                    delivery_id,
                    status="quarantined",
                    attempts=1,
                    last_error=str(test.get("message") or test.get("error") or "account test failed"),
                )
                return False
            self.sub2.update_account(
                account_id,
                {
                    "group_ids": target_groups,
                    "concurrency": int(settings.get("account_concurrency", 30)),
                    "credentials": credentials,
                    "extra": extra,
                    "status": "active",
                    "confirm_mixed_channel_risk": True,
                },
            )
            self.sub2.set_schedulable(account_id, True)
            self.store.update_delivery(delivery_id, status="active", attempts=0, last_error="")
            return True
        except Exception as exc:
            self.store.update_delivery(
                delivery_id,
                status="quarantined",
                attempts=int(existing.get("attempts", 0) if existing else 0) + 1,
                last_error=_safe_error(exc),
            )
            raise

    def _poll_account_health(self, settings: dict[str, Any]) -> None:
        active = self.store.list_deliveries(1000, statuses=("active",))
        for delivery in active:
            account_id = delivery.get("sub2_account_id")
            if not account_id:
                continue
            try:
                account = self.sub2.account(int(account_id))
            except Exception:
                continue
            if not _looks_unauthorized(account):
                continue
            attempts = int(delivery.get("attempts", 0)) + 1
            try:
                self.sub2.set_schedulable(int(account_id), False)
            except Exception:
                pass
            self.store.update_delivery(
                int(delivery["id"]),
                status="recovery_pending",
                attempts=attempts,
                last_error="Sub2 reported an unauthorized account",
            )
            self.store.event(
                "warning",
                "account_401_detected",
                "401 account quarantined and queued for recovery",
                {"account_id": int(account_id), "attempts": attempts},
            )
            self._notify(
                "warning",
                "检测到 401 账号，已隔离并进入自动修复",
                {"account_id": int(account_id), "attempts": attempts},
                event_type="account_401_detected",
                category="recoveries",
                dedup_key=f"account_401_detected:{account_id}",
            )
            if attempts >= 3:
                self._notify(
                    "error",
                    "账号自动修复已连续失败三次",
                    {"account_id": int(account_id), "attempts": attempts},
                    event_type="account_recovery_exhausted",
                    category="recoveries",
                    dedup_key=f"account_recovery_exhausted:{account_id}",
                )

    def _poll_recoveries(self, settings: dict[str, Any]) -> None:
        if not self.supplier.configured:
            return
        try:
            items = self.supplier.recoveries()
        except Exception:
            return
        deliveries = self.store.list_deliveries(1000)
        for item in items:
            self._process_recovery_item(item, settings, deliveries)

    def _capture_replacement_files(
        self,
        order: dict[str, Any],
        response: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        files = replacement_files(response)
        if not files:
            return
        deliveries = [
            item for item in self.store.list_deliveries(1000) if item.get("order_id") == order.get("id")
        ]
        for item in files:
            current = dict(item)
            status_url = str(current.get("status_url") or "")
            if status_url and current.get("ready") is True:
                try:
                    latest = self.supplier.replacement_status(status_url)
                    if isinstance(latest, dict):
                        current.update(latest)
                except Exception as exc:
                    self.store.event(
                        "warning",
                        "replacement_status_failed",
                        "Replacement status refresh failed and will be retried",
                        {"error": _safe_error(exc)},
                    )
            self._process_recovery_item(current, settings, deliveries)

    def _process_recovery_item(
        self,
        item: dict[str, Any],
        settings: dict[str, Any],
        deliveries: list[dict[str, Any]],
    ) -> None:
        recovery_id = str(
            item.get("id")
            or item.get("recovery_id")
            or item.get("file_id")
            or item.get("ticket_id")
            or ""
        )
        if not recovery_id:
            return
        name = str(item.get("email") or item.get("account_name") or "")
        supplier_ref = str(item.get("account_id") or item.get("supplier_ref") or "")
        delivery = next(
            (
                row
                for row in deliveries
                if (name and str(row.get("account_name", "")).lower() == name.lower())
                or (supplier_ref and str(row.get("supplier_ref", "")) == supplier_ref)
            ),
            None,
        )
        if delivery is None and len(deliveries) == 1:
            delivery = deliveries[0]
        claim_url = str(item.get("claim_url") or "")
        status = str(item.get("delivery_status") or item.get("status") or "pending")
        if claim_url and item.get("ready") is True:
            status = "claimable"
        previous = next(
            (
                row
                for row in self.store.list_recoveries(1000)
                if str(row.get("supplier_recovery_id")) == recovery_id
            ),
            None,
        )
        self.store.upsert_recovery(
            {
                "supplier_recovery_id": recovery_id,
                "delivery_id": delivery.get("id") if delivery else None,
                "account_name": name or str(delivery.get("account_name", "") if delivery else ""),
                "status": status,
                "attempts": int(item.get("attempts") or 0),
                "last_error": str(item.get("error") or ""),
                "raw_json": json.dumps(item, ensure_ascii=True),
            }
        )
        if status in {"refunded", "refund_completed", "credited"} and (
            not previous or str(previous.get("status")) != status
        ):
            released_fen = _int(item, "released_fen", default=_int(item, "refund_fen"))
            self._notify(
                "info",
                "账号修复退款状态已更新",
                {"status": status, "released_fen": released_fen, "account_id": supplier_ref},
                event_type="recovery_refunded",
                category="recoveries",
                dedup_key=f"recovery_refunded:{recovery_id}:{status}",
            )
        if status not in {"claimable", "ready"} or not claim_url or not delivery:
            return
        try:
            claimed = self.supplier.claim_recovery(
                claim_url,
                str(item.get("claim_ticket") or ""),
                f"recovery-{recovery_id}",
            )
            accounts = normalize_accounts(claimed)
            if not accounts:
                raise RuntimeError("recovery claim returned no account credentials")
            new_version = _credential_version(accounts[0]) or _credential_version(claimed)
            old_version = int(delivery.get("credential_version") or 0)
            if old_version and new_version <= old_version:
                raise RuntimeError("replacement credential_version did not increase")
            self._replace_account(delivery, accounts[0], settings)
            self.store.upsert_recovery(
                {
                    "supplier_recovery_id": recovery_id,
                    "delivery_id": delivery["id"],
                    "account_name": name or str(delivery.get("account_name", "")),
                    "status": "claimed",
                    "attempts": int(item.get("attempts") or 0),
                    "last_error": "",
                    "raw_json": json.dumps(
                        {"status": "claimed", "credential_version": new_version},
                        ensure_ascii=True,
                    ),
                }
            )
        except Exception as exc:
            self.store.event(
                "warning",
                "recovery_claim_failed",
                "Recovery claim failed and will be retried",
                {"error": _safe_error(exc)},
            )
            self._notify(
                "warning",
                "修复凭据领取失败，将自动重试",
                {"error": _safe_error(exc), "attempts": int(item.get("attempts") or 0)},
                event_type="recovery_claim_failed",
                category="recoveries",
                dedup_key=f"recovery_claim_failed:{recovery_id}",
            )

    def _replace_account(self, delivery: dict[str, Any], account: dict[str, Any], settings: dict[str, Any]) -> None:
        account_id = int(delivery["sub2_account_id"])
        credentials = dict(account["credentials"])
        credentials["model_mapping"] = {m: m for m in settings.get("models", [])}
        extra = _sub2_openai_extra(settings)
        stage_groups = _unique_positive(
            [int(settings.get("staging_group_id", 0)), int(settings.get("monitor_group_id", 0))]
        )
        target_groups = _unique_positive(
            [int(settings.get("monitor_group_id", 0)), *list(settings.get("target_group_ids", []))]
        )
        self.sub2.set_schedulable(account_id, False)
        self.sub2.update_account(
            account_id,
            {"credentials": credentials, "extra": extra, "group_ids": stage_groups, "status": "active"},
        )
        test = self.sub2.test_account(account_id)
        if not _test_success(test):
            self.store.update_delivery(delivery["id"], status="quarantined", last_error="replacement test failed")
            raise RuntimeError("replacement account test failed")
        self.sub2.update_account(account_id, {"group_ids": target_groups, "status": "active"})
        self.sub2.set_schedulable(account_id, True)
        self.store.update_delivery(
            delivery["id"],
            status="active",
            last_error="",
            attempts=0,
            credential_version=max(
                int(delivery.get("credential_version") or 0),
                _credential_version(account),
            ),
        )
        self.store.event("info", "recovery_completed", "401 账号已自动补发并重新上线", {"account_id": account_id})
        self._notify(
            "info",
            "401 账号已自动补发并重新上线",
            {"account_id": account_id},
            event_type="recovery_completed",
            category="recoveries",
            dedup_key=f"recovery_completed:{account_id}:{_credential_version(account)}",
        )

    def _monitor_health(self, settings: dict[str, Any], metrics: Metrics) -> None:
        self._health_transition(
            settings,
            "sub2_connection",
            not metrics.sub2_connected,
            "error",
            "Sub2 连接中断",
            "Sub2 连接已恢复",
            {"error": metrics.last_error},
        )
        self._health_transition(
            settings,
            "pool_empty",
            metrics.sub2_connected and metrics.available_accounts <= 0,
            "error",
            "账号池已断供",
            "账号池供应已恢复",
            {
                "available_accounts": metrics.available_accounts,
                "concurrency_used": metrics.concurrency_used,
                "concurrency_max": metrics.concurrency_max,
            },
        )
        if metrics.external_supplier_last_seen_at:
            self._health_transition(
                settings,
                "external_supplier_connection",
                not metrics.external_supplier_connected,
                "warning",
                "供应商自动推送连接中断",
                "供应商自动推送连接已恢复",
                {"status": metrics.external_supplier_last_path or "未连接"},
            )
        if (
            metrics.external_supplier_last_status >= 400
            and metrics.external_supplier_last_source != "chaos_test"
        ):
            self._notify(
                "warning" if metrics.external_supplier_last_status < 500 else "error",
                "供应商自动推送接口出现异常",
                {
                    "status": metrics.external_supplier_last_status,
                    "error": metrics.external_supplier_last_path,
                    "request_count": metrics.external_supplier_request_count,
                },
                event_type="external_supplier_request_failed",
                category="orders",
                dedup_key=(
                    "external_supplier_request_failed:"
                    f"{metrics.external_supplier_last_path}:{metrics.external_supplier_last_status}"
                ),
            )
        if self.supplier.configured:
            self._health_transition(
                settings,
                "direct_supplier_connection",
                not metrics.supplier_connected,
                "warning",
                "本地备用采购器连接中断",
                "本地备用采购器连接已恢复",
                {"error": metrics.last_error},
            )
        threshold = int(settings.get("feishu_balance_threshold_fen", 0))
        self._health_transition(
            settings,
            "supplier_balance",
            metrics.supplier_connected and metrics.supplier_available_fen <= threshold,
            "warning",
            "供应商余额低于预警线",
            "供应商余额已恢复",
            {"balance_fen": metrics.supplier_available_fen, "estimated_fen": threshold},
            category="balance",
        )

    def _health_transition(
        self,
        settings: dict[str, Any],
        key: str,
        active: bool,
        level: str,
        incident_title: str,
        recovery_title: str,
        metadata: dict[str, Any],
        *,
        category: str = "pool",
    ) -> None:
        previous = self._health_states.get(key)
        self._health_states[key] = active
        if active:
            self._notify(
                level,
                incident_title,
                metadata,
                event_type=key,
                category=category,
                dedup_key=f"health:{key}:incident",
                force=previous is not True,
            )
        elif previous is True:
            self._notify(
                "info",
                recovery_title,
                metadata,
                event_type=f"{key}_recovered",
                category=category,
                dedup_key=f"health:{key}:recovery",
                force=True,
            )

    def _notification_credentials(self, settings: dict[str, Any]) -> tuple[str, str]:
        webhook = str(settings.get("feishu_webhook_url") or self.config.notification_webhook_url or "").strip()
        secret = str(
            settings.get("feishu_signing_secret") or self.config.notification_signing_secret or ""
        ).strip()
        return webhook, secret

    def _notify(
        self,
        level: str,
        message: str,
        metadata: dict[str, Any],
        *,
        event_type: str = "system",
        category: str = "pool",
        dedup_key: str = "",
        force: bool = False,
    ) -> bool:
        settings = self.store.settings()
        if not (settings.get("feishu_enabled") or settings.get("webhook_enabled")):
            return False
        if not settings.get(f"feishu_notify_{category}", True):
            return False
        webhook, secret = self._notification_credentials(settings)
        if not webhook:
            return False
        try:
            return self._notifier.send(
                webhook,
                secret,
                level,
                message,
                metadata,
                event_type=event_type,
                dedup_key=dedup_key,
                cooldown_seconds=int(settings.get("feishu_cooldown_seconds", 600)),
                force=force,
            )
        except Exception as exc:
            self.store.event("warning", "notification_failed", "飞书通知发送失败", {"error": _safe_error(exc)})
            return False


def _sub2_openai_extra(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "openai_passthrough": bool(settings.get("openai_passthrough", False)),
        "openai_oauth_responses_websockets_v2_mode": "ctx_pool",
        "openai_oauth_responses_websockets_v2_enabled": True,
        "codex_fingerprint_mode": "session",
    }


def normalize_accounts(payload: Any) -> list[dict[str, Any]]:
    value = payload
    for key in ("data", "payload", "result"):
        if isinstance(value, dict) and key in value and isinstance(value[key], (dict, list)):
            value = value[key]
    if isinstance(value, dict) and "accounts" in value:
        value = value["accounts"]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        if "data" in raw and isinstance(raw["data"], dict):
            nested = normalize_accounts(raw["data"])
            output.extend(nested)
            continue
        credentials = raw.get("credentials")
        if not isinstance(credentials, dict):
            credentials = {
                key: raw[key]
                for key in (
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
                )
                if raw.get(key) not in (None, "")
            }
        if not any(credentials.get(key) for key in ("access_token", "refresh_token", "id_token")):
            continue
        name = str(raw.get("name") or raw.get("email") or credentials.get("email") or "").strip()
        if not name:
            name = f"supply-{hashlib.sha256(json.dumps(credentials, sort_keys=True).encode()).hexdigest()[:12]}"
        output.append(
            {
                "name": name,
                "email": str(raw.get("email") or credentials.get("email") or ""),
                "supplier_ref": str(raw.get("id") or raw.get("account_id") or ""),
                "platform": "openai",
                "type": "oauth",
                "credentials": credentials,
                "extra": raw.get("extra") if isinstance(raw.get("extra"), dict) else {},
                "expires_at": raw.get("expires_at") or credentials.get("expires_at"),
                "priority": int(raw.get("priority") or 0),
                "credential_version": _credential_version(raw),
            }
        )
    return output


def replacement_files(payload: Any) -> list[dict[str, Any]]:
    pending = [payload]
    seen: set[int] = set()
    output: list[dict[str, Any]] = []
    while pending:
        value = pending.pop()
        if not isinstance(value, dict) or id(value) in seen:
            continue
        seen.add(id(value))
        files = value.get("replacement_files")
        if isinstance(files, list):
            output.extend(item for item in files if isinstance(item, dict))
        for key in ("data", "payload", "result", "order"):
            nested = value.get(key)
            if isinstance(nested, dict):
                pending.append(nested)
    return output


def _credential_version(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    for key in ("credential_version", "version"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    for key in ("data", "payload", "result", "account", "credentials"):
        version = _credential_version(payload.get(key))
        if version:
            return version
    return 0


def account_fingerprint(account: dict[str, Any]) -> str:
    credentials = account.get("credentials", {})
    identity = {
        "email": account.get("email") or credentials.get("email"),
        "chatgpt_account_id": credentials.get("chatgpt_account_id"),
        "refresh_token": credentials.get("refresh_token"),
        "id_token": credentials.get("id_token"),
    }
    if not any(identity.values()):
        identity["access_token"] = credentials.get("access_token")
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_supplier_account(account: dict[str, Any]) -> None:
    if not isinstance(account, dict) or not isinstance(account.get("credentials"), dict):
        raise ValueError("supplier account must contain credentials")
    credentials = account["credentials"]
    if not any(credentials.get(key) for key in ("access_token", "refresh_token", "id_token")):
        raise ValueError("supplier account has no OAuth token")
    if not any(
        account.get("email") or credentials.get(key)
        for key in ("email", "chatgpt_account_id", "chatgpt_user_id")
    ):
        raise ValueError("supplier account has no stable identity")
    plan = str(credentials.get("plan_type") or account.get("plan_type") or "").lower()
    if plan and "team" not in plan and "business" not in plan:
        raise ValueError("supplier account is not a Team account")
    expiry = _unix_expiry(account.get("expires_at") or credentials.get("expires_at"))
    if expiry and expiry <= int(time.time()) + 300:
        raise ValueError("supplier account is expired")


def _looks_unauthorized(account: Any) -> bool:
    if not isinstance(account, dict):
        return False
    values = [
        account.get("status"),
        account.get("error"),
        account.get("last_error"),
        account.get("health_status"),
        account.get("error_message"),
    ]
    text = " ".join(str(value).lower() for value in values if value not in (None, ""))
    return "401" in text or "unauthorized" in text or "invalid token" in text


def _redact_supplier_payload(value: Any) -> Any:
    sensitive = {
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "password",
        "credentials",
        "accounts",
    }
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key.lower() in sensitive else _redact_supplier_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_supplier_payload(item) for item in value]
    return value


def _select_metric(data: Any, section: str, key: str) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    values = data.get(section, {})
    if isinstance(values, dict):
        direct = values.get(key)
        if isinstance(direct, dict):
            return direct
        for value in values.values():
            if isinstance(value, dict) and str(value.get("group_id", value.get("platform", ""))) == key:
                return value
    if isinstance(values, list):
        for value in values:
            if isinstance(value, dict) and str(value.get("group_id", value.get("platform", ""))) == key:
                return value
    return None


def _int(data: Any, key: str, default: int = 0) -> int:
    try:
        return int((data or {}).get(key, default) or 0)
    except (TypeError, ValueError):
        return default


def _unique_positive(values: list[Any]) -> list[int]:
    output: list[int] = []
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0 and parsed not in output:
            output.append(parsed)
    return output


def _test_success(test: dict[str, Any]) -> bool:
    if test.get("success") is False or test.get("ok") is False:
        return False
    status = str(test.get("status") or "").lower()
    if status in {"error", "failed", "unauthorized"}:
        return False
    if test.get("error") and not test.get("success"):
        return False
    return True


def _unix_expiry(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        parsed = int(value)
        return parsed // 1000 if parsed > 10_000_000_000 else parsed
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int(parsed.timestamp())
    except ValueError:
        return None


def _iso_expiry(value: Any) -> str | None:
    unix = _unix_expiry(value)
    return datetime.fromtimestamp(unix, UTC).isoformat() if unix else None


def _safe_error(exc: Exception) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return message[:500] or exc.__class__.__name__
