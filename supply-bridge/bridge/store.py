from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


DEFAULT_SETTINGS: dict[str, Any] = {
    "auto_enabled": True,
    "dry_run": True,
    "emergency_stop": False,
    "products": ["team_1h"],
    "low_watermark": 2,
    "target_available": 5,
    "min_order_units": 1,
    "max_order_units": 5,
    "daily_spend_cap_fen": 2000,
    "cooldown_seconds": 30,
    "forecast_lead_minutes": 10,
    "replenish_on_eta": True,
    "replenish_on_concurrency": True,
    "replenish_on_empty": True,
    "replenish_on_low_stock": True,
    "replenish_on_schedule": False,
    "schedule_interval_minutes": 60,
    "schedule_quantity": 1,
    "concurrency_threshold_percent": 80,
    "account_concurrency": 30,
    "monitor_group_id": 0,
    "staging_group_id": 0,
    "target_group_ids": [],
    "models": [
        "gpt-image-2",
        "gpt-5.3-codex",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.5",
        "gpt-5.6",
        "gpt-5.6-sol",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "codex-auto-review",
    ],
    "openai_passthrough": False,
    "poll_interval_seconds": 10,
    "webhook_enabled": False,
    "feishu_enabled": False,
    "feishu_webhook_url": "",
    "feishu_signing_secret": "",
    "feishu_balance_threshold_fen": 500,
    "feishu_cooldown_seconds": 600,
    "feishu_notify_pool": True,
    "feishu_notify_balance": True,
    "feishu_notify_orders": True,
    "feishu_notify_recoveries": True,
}


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          captured_at TEXT NOT NULL,
          metrics_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots(captured_at DESC);
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY,
          supplier_order_id TEXT UNIQUE,
          product TEXT NOT NULL,
          quantity INTEGER NOT NULL,
          status TEXT NOT NULL,
          trigger_type TEXT NOT NULL,
          estimated_fen INTEGER NOT NULL DEFAULT 0,
          charged_fen INTEGER NOT NULL DEFAULT 0,
          released_fen INTEGER NOT NULL DEFAULT 0,
          idempotency_key TEXT NOT NULL UNIQUE,
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '',
          raw_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, updated_at DESC);
        CREATE TABLE IF NOT EXISTS deliveries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id TEXT NOT NULL REFERENCES orders(id),
          supplier_ref TEXT NOT NULL DEFAULT '',
          account_name TEXT NOT NULL,
          fingerprint TEXT NOT NULL UNIQUE,
          sub2_account_id INTEGER,
          quota_usd REAL NOT NULL DEFAULT 0,
          credential_version INTEGER NOT NULL DEFAULT 0,
          expires_at TEXT,
          status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status, updated_at DESC);
        CREATE TABLE IF NOT EXISTS recoveries (
          supplier_recovery_id TEXT PRIMARY KEY,
          delivery_id INTEGER REFERENCES deliveries(id),
          account_name TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '',
          raw_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL,
          level TEXT NOT NULL,
          event_type TEXT NOT NULL,
          message TEXT NOT NULL,
          metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_events_time ON events(created_at DESC);
        """
        with self._lock, self.connect() as conn:
            conn.executescript(schema)
            delivery_columns = {row[1] for row in conn.execute("PRAGMA table_info(deliveries)")}
            if "credential_version" not in delivery_columns:
                conn.execute(
                    "ALTER TABLE deliveries ADD COLUMN credential_version INTEGER NOT NULL DEFAULT 0"
                )
            now = utcnow()
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
                    (key, json.dumps(value, ensure_ascii=True), now),
                )
        self._secure_database_files()

    def _secure_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(f"{self.path}{suffix}", 0o600)
            except (FileNotFoundError, OSError):
                pass

    def settings(self) -> dict[str, Any]:
        result = dict(DEFAULT_SETTINGS)
        with self.connect() as conn:
            for row in conn.execute("SELECT key,value_json FROM settings"):
                result[row["key"]] = json.loads(row["value_json"])
        return result

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        unknown = set(values) - set(DEFAULT_SETTINGS)
        if unknown:
            raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
        now = utcnow()
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for key, value in values.items():
                    conn.execute(
                        "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                        (key, json.dumps(value, ensure_ascii=True), now),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.settings()

    def event(self, level: str, event_type: str, message: str, metadata: dict[str, Any] | None = None) -> None:
        safe = _redact(metadata or {})
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(created_at,level,event_type,message,metadata_json) VALUES(?,?,?,?,?)",
                (utcnow(), level, event_type, message, json.dumps(safe, ensure_ascii=True)),
            )

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [_row_json(row, "metadata_json", "metadata") for row in rows]

    def add_snapshot(self, metrics: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO snapshots(captured_at,metrics_json) VALUES(?,?)",
                (utcnow(), json.dumps(metrics, ensure_ascii=True)),
            )
            cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
            conn.execute("DELETE FROM snapshots WHERE captured_at < ?", (cutoff,))

    def latest_snapshots(self, limit: int = 360) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT captured_at,metrics_json FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"captured_at": r["captured_at"], **json.loads(r["metrics_json"])} for r in rows]

    def upsert_order(self, order: dict[str, Any]) -> None:
        now = utcnow()
        payload = dict(order)
        payload.setdefault("created_at", now)
        payload.setdefault("updated_at", now)
        payload.setdefault("raw_json", "{}")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO orders(id,supplier_order_id,product,quantity,status,trigger_type,
                   estimated_fen,charged_fen,released_fen,idempotency_key,attempts,last_error,raw_json,created_at,updated_at)
                   VALUES(:id,:supplier_order_id,:product,:quantity,:status,:trigger_type,:estimated_fen,
                   :charged_fen,:released_fen,:idempotency_key,:attempts,:last_error,:raw_json,:created_at,:updated_at)
                   ON CONFLICT(id) DO UPDATE SET supplier_order_id=excluded.supplier_order_id,
                   status=excluded.status,estimated_fen=excluded.estimated_fen,charged_fen=excluded.charged_fen,
                   released_fen=excluded.released_fen,attempts=excluded.attempts,last_error=excluded.last_error,
                   raw_json=excluded.raw_json,updated_at=excluded.updated_at""",
                payload,
            )

    def update_order(self, order_id: str, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utcnow()
        assignments = ",".join(f"{key}=?" for key in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE orders SET {assignments} WHERE id=?", (*values.values(), order_id))

    def order(self, order_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return _row_json(row, "raw_json", "raw") if row else None

    def active_orders(self) -> list[dict[str, Any]]:
        terminal = ("completed", "partial", "cancelled", "failed", "dry_run")
        marks = ",".join("?" for _ in terminal)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM orders WHERE status NOT IN ({marks}) ORDER BY created_at", terminal
            ).fetchall()
        return [_row_json(r, "raw_json", "raw") for r in rows]

    def list_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row_json(r, "raw_json", "raw") for r in rows]

    def daily_spend_fen(self) -> int:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(charged_fen),0) AS total FROM orders WHERE created_at>=?", (start,)
            ).fetchone()
        return int(row["total"])

    def delivery_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM deliveries WHERE fingerprint=?", (fingerprint,)).fetchone()
        return dict(row) if row else None

    def delivery(self, delivery_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        return dict(row) if row else None

    def add_delivery(self, values: dict[str, Any]) -> int:
        now = utcnow()
        payload = {**values, "created_at": now, "updated_at": now}
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO deliveries(order_id,supplier_ref,account_name,fingerprint,sub2_account_id,
                   quota_usd,credential_version,expires_at,status,attempts,last_error,created_at,updated_at)
                   VALUES(:order_id,:supplier_ref,:account_name,:fingerprint,:sub2_account_id,:quota_usd,
                   :credential_version,:expires_at,:status,:attempts,:last_error,:created_at,:updated_at)""",
                payload,
            )
            return int(cur.lastrowid)

    def update_delivery(self, delivery_id: int, **values: Any) -> None:
        values["updated_at"] = utcnow()
        assignments = ",".join(f"{key}=?" for key in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE deliveries SET {assignments} WHERE id=?", (*values.values(), delivery_id))

    def list_deliveries(self, limit: int = 300, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if statuses:
                marks = ",".join("?" for _ in statuses)
                rows = conn.execute(
                    f"SELECT * FROM deliveries WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?",
                    (*statuses, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM deliveries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def upsert_recovery(self, values: dict[str, Any]) -> None:
        now = utcnow()
        payload = {**values, "created_at": now, "updated_at": now}
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO recoveries(supplier_recovery_id,delivery_id,account_name,status,attempts,
                   last_error,raw_json,created_at,updated_at)
                   VALUES(:supplier_recovery_id,:delivery_id,:account_name,:status,:attempts,:last_error,
                   :raw_json,:created_at,:updated_at)
                   ON CONFLICT(supplier_recovery_id) DO UPDATE SET delivery_id=excluded.delivery_id,
                   account_name=excluded.account_name,status=excluded.status,attempts=excluded.attempts,
                   last_error=excluded.last_error,raw_json=excluded.raw_json,updated_at=excluded.updated_at""",
                payload,
            )

    def list_recoveries(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM recoveries ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row_json(r, "raw_json", "raw") for r in rows]


def _row_json(row: sqlite3.Row, source: str, target: str) -> dict[str, Any]:
    value = dict(row)
    raw = value.pop(source, "{}")
    try:
        value[target] = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        value[target] = {}
    return value


def _redact(value: Any) -> Any:
    sensitive = {
        "password",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "admin_key",
        "credentials",
        "webhook",
        "webhook_url",
        "feishu_webhook_url",
        "feishu_signing_secret",
        "signing_secret",
    }
    if isinstance(value, dict):
        return {k: "[redacted]" if k.lower() in sensitive else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value
