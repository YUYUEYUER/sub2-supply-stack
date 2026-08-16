from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _secret(env_name: str, default_path: str = "") -> str:
    direct = os.getenv(env_name, "").strip()
    if direct:
        return direct
    path = os.getenv(f"{env_name}_FILE", default_path).strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _json_list(name: str, default: list[Any]) -> list[Any]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return list(default)
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


@dataclass(frozen=True)
class AppConfig:
    listen_host: str
    listen_port: int
    database_path: str
    supplier_base_url: str
    supplier_username: str
    supplier_password: str
    rbac_proxy_url: str
    rbac_token: str
    admin_token: str
    poll_interval_seconds: int
    request_timeout_seconds: int
    notification_webhook_url: str
    notification_signing_secret: str

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            listen_host=os.getenv("BRIDGE_LISTEN_HOST", "0.0.0.0"),
            listen_port=int(os.getenv("BRIDGE_LISTEN_PORT", "8080")),
            database_path=os.getenv("BRIDGE_DATABASE_PATH", "/data/bridge.db"),
            supplier_base_url=os.getenv("SUPPLIER_BASE_URL", "https://bugteam.team").rstrip("/"),
            supplier_username=os.getenv("SUPPLIER_USERNAME", "").strip(),
            supplier_password=_secret("SUPPLIER_PASSWORD", "/run/secrets/supplier_password"),
            rbac_proxy_url=os.getenv("SUB2_RBAC_PROXY_URL", "http://sub2-rbac-proxy:8081").rstrip("/"),
            rbac_token=_secret("SUB2_RBAC_TOKEN", "/run/secrets/rbac_token"),
            admin_token=_secret("BRIDGE_ADMIN_TOKEN", "/run/secrets/bridge_admin_token"),
            poll_interval_seconds=max(5, int(os.getenv("BRIDGE_POLL_INTERVAL_SECONDS", "10"))),
            request_timeout_seconds=max(5, int(os.getenv("BRIDGE_REQUEST_TIMEOUT_SECONDS", "30"))),
            notification_webhook_url=os.getenv("BRIDGE_NOTIFICATION_WEBHOOK", "").strip(),
            notification_signing_secret=_secret("BRIDGE_NOTIFICATION_SECRET"),
        )


@dataclass(frozen=True)
class ProxyConfig:
    listen_host: str
    listen_port: int
    sub2_base_url: str
    sub2_admin_key: str
    shared_token: str
    allowed_group_ids: tuple[int, ...]
    ownership_group_id: int
    default_import_group_ids: tuple[int, ...]
    allowed_models: tuple[str, ...]
    max_concurrency: int
    request_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "ProxyConfig":
        groups = tuple(int(v) for v in _json_list("RBAC_ALLOWED_GROUP_IDS", []))
        import_groups = tuple(int(v) for v in _json_list("RBAC_IMPORT_GROUP_IDS", []))
        models = tuple(str(v).strip() for v in _json_list("RBAC_ALLOWED_MODELS", []) if str(v).strip())
        return cls(
            listen_host=os.getenv("RBAC_LISTEN_HOST", "0.0.0.0"),
            listen_port=int(os.getenv("RBAC_LISTEN_PORT", "8081")),
            sub2_base_url=os.getenv("SUB2_BASE_URL", "http://sub2api:8080").rstrip("/"),
            sub2_admin_key=_secret("SUB2_ADMIN_API_KEY", "/run/secrets/sub2_admin_key"),
            shared_token=_secret("SUB2_RBAC_TOKEN", "/run/secrets/rbac_token"),
            allowed_group_ids=groups,
            ownership_group_id=int(os.getenv("RBAC_OWNERSHIP_GROUP_ID", "0")),
            default_import_group_ids=import_groups,
            allowed_models=models,
            max_concurrency=max(1, int(os.getenv("RBAC_MAX_ACCOUNT_CONCURRENCY", "100"))),
            request_timeout_seconds=max(5, int(os.getenv("RBAC_REQUEST_TIMEOUT_SECONDS", "30"))),
        )
