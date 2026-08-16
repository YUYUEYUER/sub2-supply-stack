from __future__ import annotations

import signal
import threading
from typing import Any

from .clients import Sub2Client, SupplierClient
from .config import AppConfig
from .engine import BridgeEngine
from .store import Store
from .web import WebServer


def main() -> None:
    config = AppConfig.from_env()
    if not config.admin_token or not config.rbac_token:
        raise SystemExit("BRIDGE_ADMIN_TOKEN and SUB2_RBAC_TOKEN are required")
    store = Store(config.database_path)
    store.initialize()
    sub2 = Sub2Client(config.rbac_proxy_url, config.rbac_token, config.request_timeout_seconds)
    supplier = SupplierClient(
        config.supplier_base_url,
        config.supplier_username,
        config.supplier_password,
        config.request_timeout_seconds,
    )
    engine = BridgeEngine(config, store, sub2, supplier)
    server = WebServer(
        (config.listen_host, config.listen_port),
        admin_token=config.admin_token,
        store=store,
        engine=engine,
    )

    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    engine.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        engine.stop()


if __name__ == "__main__":
    main()
