# V1.5: Versioned database bootstrap and incremental OpenSearch updates

This release **keeps SQLite as the default commerce store** and preserves `DB_DRIVER=postgresql`. It does not move or convert data between them. Gorse still needs its own PostgreSQL database on Ubuntu A.

## New capabilities

1. Alembic revision `0001_v15_baseline` creates a fresh database or adopts an existing V1.4 schema **without deleting product/cart/event records**. It reconciles legacy additive columns/indexes and installs SQLite FTS5 if available. Revision downgrade intentionally refuses to destroy data.
2. Production Compose runs migrations in the Mac-only `migrate` one-shot service, after volume permissions are prepared and before the API starts. The API runs with `SCHEMA_MODE=external` and refuses an out-of-date/unversioned schema. Never start multiple migrators against the same database.
3. Importing a product now records two **independent** transactional Outbox kinds: `item` for Gorse and `search` for OpenSearch. Search delivery uses deterministic per-product IDs, CAS leases and partial-bulk error inspection. Gorse sync claims only `item` / `feedback`, while `search-sync` claims only `search`.
4. `POST /api/admin/search/sync?batch=500` can retry errors, sends up to 100 documents per OpenSearch `_bulk` request, and writes via the active `commerce-products` alias. Metrics report `search_pending` and `gorse_pending` separately.

## V1.4 → V1.5 upgrade checklist

1. **Back up** the Mac business database before stopping services. For SQLite, stop API/sync/importers first and use SQLite backup or a quiesced Docker volume snapshot; do not copy only the `.db` file while WAL writers remain active. Save `.env` and confirm sufficient disk space.
2. Stop Mac `api`, `sync` and `search-sync` if present. During the full OpenSearch index rebuild, **also stop catalog imports and search-sync**. It is unsafe to promote a new alias generation while older concurrent incremental writes are being acknowledged against the previous alias.
3. Pull the new code on all three machines. Ubuntu nodes have no required data migration this release.
4. On the Mac, use `docker compose --env-file .env -f infra/macos/compose.yaml up -d --build`. The `volume-init → migrate → api` dependency chain runs `alembic upgrade head` **once**, then starts the API and both background sidecars. Check that `migrate` finishes successfully before using the storefront.
5. For a local non-Docker install, run `alembic upgrade head` from the repository root with the same `DB_DRIVER`, `SQLITE_PATH` or `DATABASE_URL_POSTGRES` as the API. In production, set `SCHEMA_MODE=external`; for unit tests/local prototyping only, legacy auto-initialization remains available.
6. For a first OpenSearch deployment or after a mapping change, **with all importers and `search-sync` stopped**, build the alias using `python scripts/index_search.py` in the API container, then restart search-sync. This is a maintenance operation, not yet zero-downtime concurrent reindexing.
7. Import a changed demo product, inspect `/api/admin/metrics` as an authorized local administrator, and confirm `search_pending` drains and the new title becomes searchable after OpenSearch refresh. Index refresh is near-real-time, not immediate on successful bulk.

## Failures and limitations

- If OpenSearch is down, messages remain durable in the Mac business database with backoff; the browser can use SQLite FTS5 / SQL fallback.
- The search outbox represents *updates and inserts made by the current importer*. There is no product-delete API, delete tombstone protocol, distributed reindex lock or CDC adapter yet. Manual direct SQL product edits will not enqueue OpenSearch updates.
- Search indexing is **at least once**, not linearizable against concurrent full alias swaps. Keep imports and search-sync paused for full builds, or perform a later reconciliation before opening traffic. In a crash window an HTTP-successful batch may be retried; OpenSearch indexing by the same ID is idempotent for equal payloads.
- A Gorse server response never acknowledges search messages, and an OpenSearch error never deletes raw product data.
- API migrations are not performed during server startup in production. Avoid resetting the Alembic version table or `stamp head` without verifying the actual schema.
- SQLite to PostgreSQL remains an explicit future data export/migration procedure. **Changing `DB_DRIVER` alone starts on the other database; it does not copy previous data.**

## Verification

- `PYTHONPATH=services/api pytest -q tests`
- `python -m compileall -q services/api scripts ml migrations`
- `alembic upgrade head` with a disposable SQLite database, repeated twice
- CI: PostgreSQL service container exercises real database mode; frontend build and Compose config checks remain present
- **Not exercised here:** physical Ubuntu/macOS LAN, OpenSearch bulk API against a running server, Gorse runtime, multi-host failure injection
