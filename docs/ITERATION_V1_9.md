# V1.9 — Catalog write fencing and remote anti-entropy

## What changed

- `products.catalog_revision` and `outbox.catalog_revision` are monotonic per-product integers. Alembic adds the two columns in revision `0004_v19_catalog_fencing` without deleting previous data. **SQLite remains the default business database**, with PostgreSQL optional. Gorse storage remains separate.
- A catalog update no longer cancels an in-progress lease. A Gorse/OpenSearch worker can ACK only the exact revision it read; when a newer revision appears during the HTTP call, the old ACK clears the old lease without marking the replacement delivered. This applies only to catalog `item` and `search` rows. Feedback delivery is unchanged.
- OpenSearch incremental index/delete requests and full-index builds use `version_type=external_gte` with the same product revision. Version conflicts are treated as delivery failures for investigation, not silently acknowledged.
- `POST /api/admin/catalog/reconcile` reads remote Gorse `GET /api/item/{id}` and OpenSearch `GET /commerce-products/_doc/{id}` (10 products per call by default, at most 25). It reports mismatches/errors, and with `repair=true` durably requeues the current state for **both** providers. Backend outages do not result in repairs.
- `scripts/reconcile_catalog.py` pages through the admin read-back API. Without `--repair` it is read-only; with `--repair` it runs anti-entropy repairs. A bounded optional Compose `reconciler` profile can perform sweeps hourly. Admin routes are not exposed by Next.js.

## Important limitations

Gorse 0.5.x has no per-item compare-and-swap version on `POST /api/items`, so a network request that times out but completes long after its lease expires **can still overwrite newer state**. Reconciliation repairs observed drift; it does not offer linearizable or instant convergence. Re-run a sweep after failures/maintenance. OpenSearch external versions help with out-of-order writes but delete tombstone version protection expires according to `index.gc_deletes` (default 60 seconds), so late stale writes still require anti-entropy. The API filters withdrawn items from local results regardless of downstream status.

`external_gte` permits identical versions, which is needed for retries. Do not manually reuse a revision for different catalog documents. Never roll back the business DB to an older snapshot while keeping an index with newer versions without a clean search index rebuild.

## Mandatory upgrade order for existing V1.8 deployments

1. Stop Mac API, storefront writers, product imports, Gorse `sync` and `search-sync`. Back up SQLite using SQLite's backup API / a consistent snapshot (not a simple copy of a WAL-open `.db`), or take a PostgreSQL backup. Check available disk and save `.env` separately.
2. `git pull origin main` on the three machines. On Mac run `alembic upgrade head` using the same DB env as the API. Confirm `alembic_version` is `0004_v19_catalog_fencing`.
3. With all writers and workers **still stopped**, run:

   ```bash
   PYTHONPATH=services/api SCHEMA_MODE=external python scripts/requeue_catalog.py
   PYTHONPATH=services/api SCHEMA_MODE=external python scripts/requeue_catalog.py --execute
   ```

   This re-snapshots existing business catalog state and intentionally updates each product revision. It does not modify Gorse or OpenSearch directly. For Docker Mac deployments, run the script in the migrated API image with the normal mounted data volume (`docker compose ... run --rm --no-deps api python /app/scripts/requeue_catalog.py --execute`). Do not use a stale API image.
4. Build a **new full OpenSearch index generation** with V1.9 `scripts/index_search.py` while product imports and search-sync are paused. This switches the index from pre-V1.9 internal version numbers to the new external revision numbers. Wait for alias promotion to succeed. Search sync should not be started until this is complete.
5. Start Gorse and search sync sidecars, then the API/storefront. Drain Outbox queues; run a **read-only** reconciliation sweep followed by `--repair` if mismatches remain. Verify `/api/admin/metrics`: `catalog_latest_revision`, `catalog_leased_tasks`, backlog, and error metrics.

The deployment's usual `migrate` one-shot job only applies Alembic; it does **not** do steps 3 and 4 automatically, because those operations require operator-controlled downtime and a consistent index generation. Do not blindly run `docker compose up -d` on an existing V1.8 installation until the upgrade sequence above is complete.

## Private LAN read-back

```bash
export API_URL=http://127.0.0.1:8000
export ADMIN_KEY='your-real-admin-secret'
python scripts/reconcile_catalog.py --limit 25 --max-pages 20
python scripts/reconcile_catalog.py --limit 25 --max-pages 20 --repair
```

The CLI exits nonzero on remote errors and never silently repairs unknown provider failures. The optional `docker compose --profile reconciliation -f infra/macos/compose.yaml up -d reconciler` starts an opt-in hourly bounded sweep; it is not enabled by the default Compose deployment. The example profile scans at most 2,500 products from the start of the catalog each hour; for larger catalogs plan a full sweep using the CLI or implement a persistent cursor before enabling unattended coverage. `max-pages` bounds request volume: 20 pages checks at most 500 products; larger catalogs need multiple sweeps. Only run on your own LAN, from an authenticated host. Gorse and OpenSearch ports must remain private.

## Verification

`PYTHONPATH=services/api pytest -q tests` covers catalog revision preservation, concurrent stale ACK behavior, external version metadata, remote-read errors, read-only reconciliation and V1.8→V1.9 migration. Live multi-machine network delays, OpenSearch delete tombstone expiry and Gorse timeout ordering have not been exercised on the three target machines.
