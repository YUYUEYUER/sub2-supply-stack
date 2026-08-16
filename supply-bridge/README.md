# Sub2 Supply Bridge

Sub2 Supply Bridge connects a supplier pickup API to Sub2 through a separate least-privilege proxy. The bridge never receives the Sub2 administrator API key.

The supplier integration follows the customer API lifecycle: 30-day customer tokens,
inventory quotes, idempotent order creation, 202 pickup polling with Retry-After,
initial account pickup, paginated recoveries, replacement-file status refresh and
one-time recovery claims with credential-version checks.

Feishu custom-bot notifications are configured in the operator UI. Webhook and
optional signing-secret values are never returned by the API; incident cards cover
pool outages and recovery, supplier connectivity, low balance, order/import failures,
401 replacement, and refunds with per-event cooldown deduplication.

## Runtime boundaries

- `sub2-rbac-proxy` is reachable only on the private Compose network.
- `sub2-supply-bridge` stores operational state in SQLite and exposes its UI on `127.0.0.1:19090`.
- Supplier, Sub2, and UI secrets are mounted as read-only files under `/run/secrets`.
- Account credentials are used for import but are not written to events or order audit payloads.

## Verification

```sh
python -m unittest discover -s tests -v
python -m compileall -q bridge
docker build -t ghcr.io/yuyueyuer/sub2-supply-bridge:1.0.22 .
```

## Required secret files

The Compose project expects these files under `sub2api-deploy/secrets/`:

- `sub2_admin_key`
- `rbac_token`
- `bridge_admin_token`
- `supplier_password`

Keep all secret files out of source control and restrict them to the deployment owner.
