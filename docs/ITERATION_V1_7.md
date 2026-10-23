# V1.7 — Coordinated OpenSearch alias rebuilds and LAN health diagnostics

## Scope and guarantees

This release preserves the default **SQLite business database**, optional PostgreSQL business backend, separate Gorse PostgreSQL and three-machine Compose topology. It adds a **database-backed search rebuild lease** and read-only diagnostic probes. No physical three-host failure drill or live Gorse/OpenSearch load test has been performed by the development environment.

### Why reindexing needed coordination

Before V1.7, `scripts/index_search.py` could build a new OpenSearch index while `search-sync` delivered updates to the old `commerce-products` alias. The alias could be switched after a change was acknowledged on the previous generation, losing that change in the new index.

V1.7 establishes the following protocol:

1. The indexer atomically claims singleton row `search_maintenance(id=1)` for up to **300 seconds**. A second indexer is rejected.
2. The indexer **waits for preexisting search Outbox delivery leases** to clear (maximum 35 seconds by default) before building. If they do not drain, the rebuild aborts and releases its lease; the old alias remains available.
3. The incremental `/api/admin/search/sync` endpoint acquires a **SQL row write lock** on that singleton within the same transaction as the Outbox claim. While a rebuild lease is active it returns **HTTP 423** without claiming events. This is valid for both SQLite WAL and PostgreSQL and does not depend on a per-process mutex.
4. The indexer streams product rows into a new versioned index and **renews its lease** at batch boundaries and before the alias swap. It refuses promotion if its lease has expired or ownership has changed.
5. The indexer's alias promotion remains atomic. The lease is released in `finally` on success or failure.
6. **Product imports are NOT frozen during rebuilding.** They continue to write durable Outbox entries transactionally. Updates that occur while the indexer is reading the catalog remain pending and are delivered to the *new alias* by the sidecar after release. As a result, there may be a short period of eventual inconsistency after a successful promotion until the delta queue drains.

The previous Outbox retry/fencing protections still apply. No prior delivered product rows need to be replayed because the fresh generation is populated from the business database and all concurrent mutations requeue a search Outbox row. This assumes product mutations go through the importer, which transactionally emits Outbox events. **Direct SQL modifications to `products` without Outbox are not supported** and can create stale search results.

**Lease caveats:** if the indexer crashes, the gate remains closed for at most the remaining lease period (up to 5 minutes); after that the search sidecar can resume. If a bulk request stalls beyond its lease, the indexer fails its next renewal and refuses promotion. Use NTP/time synchronization across hosts, because lock timestamps come from the Mac's business database client clock. There is no automatic takeover of a half-built index; orphaned generations should be inspected and cleaned manually only after confirming the live alias.

## Upgrade / rollback

**Back up the live Mac SQLite volume** with all writers stopped. Pull the repository on all three hosts and stop `api`, `sync`, `search-sync` and imports before upgrading. The Mac `migrate` service runs `alembic upgrade head` as part of Compose startup. The new Alembic revision is **`0002_v17_search_gate`** and creates the one-row `search_maintenance` table. The migration is additive and preserves existing product, cart, event and Outbox data. API external-schema mode refuses older schema revisions.

```bash
# Run from the repo root after configuring .env; stop write services and back up data first

docker compose --env-file .env -f infra/macos/compose.yaml up -d --build
# Verify the migration service succeeded:
docker compose --env-file .env -f infra/macos/compose.yaml logs migrate
# Diagnostic read-only check from any LAN node:
python scripts/diagnose_lan.py --mac 192.168.1.20 --ubuntu-a 192.168.1.21 --ubuntu-b 192.168.1.22 --json
```

For a full catalog reindex, run **only one instance** on the Mac, where the business database and OpenSearch URL are configured. During the build it is expected that the `search-sync` sidecar receives HTTP 423 and retries later.

```bash
# Example from the repo root on macOS using host-accessible LAN OpenSearch
# Configure DB_DRIVER, SQLITE_PATH (host volume path), and OPENSEARCH_URL correctly.
PYTHONPATH=services/api OPENSEARCH_URL=http://192.168.1.21:9200 python scripts/index_search.py --batch-size 250
```

Note: the Mac Docker SQLite volume is **not** automatically at `./data/commerce.db` on the host. For a safer deployment, execute the indexer from the API image attached to the same volume and pass `OPENSEARCH_URL`, rather than pointing a host-side script at the wrong SQLite file. Do not invoke two indexers concurrently.

`/api/admin/metrics` now reports `search_rebuild_active` and `search_rebuild_lease_remaining_seconds`, alongside the existing search Outbox backlog. Admin metrics require the `X-Admin-Key` header; they are not exposed via the public storefront.

For SQLite → PostgreSQL offline copying, both source and target must now be at revision `0002_v17_search_gate`. The migration tool **excludes the operational singleton row** from row copies because Alembic initializes it on both databases. Existing business-table row-count and hash verification is unchanged. Use the V1.6 offline cutover safeguards.

## Read-only diagnostics and failure drills

`python scripts/diagnose_lan.py` accepts only literal **RFC1918 or loopback IPv4** addresses. It checks the storefront API proxy, OpenSearch cluster health endpoint, Gorse master RPC TCP port and Gorse server TCP port. It uses short timeouts, never authenticates or writes, and suppresses configured HTTP proxies. The two TCP checks indicate *reachability only*, not that Gorse has trained or that the API returns recommendations.

For a controlled LAN drill, take backups first and, during a maintenance window, manually stop either OpenSearch or the Gorse server, verify that the storefront falls back to local search/recommendations, then restart the service and ensure Outbox backlogs drain. Confirm each result using `/api/admin/metrics` and `scripts/bench_api.py`; do not interpret synthetic/mock tests as a successful physical fault drill.

## Validation

```bash
PYTHONPATH=services/api:ml pytest -q tests
python -m compileall -q services/api scripts ml migrations
# Also in GitHub CI:
# - PostgreSQL service-container migration/copy tests
# - Next.js build, Compose config validation
# - non-root API image: Alembic one-shot migration + schema-enforced startup
```

The new regression tests cover exclusive rebuild claims, inflight delivery draining, stale-owner lease fencing, 423 behavior, successful alias swap using a mocked OpenSearch, read-only LAN probe inputs, and additive V1.6 → V1.7 database migration with business rows retained.

### Still deferred

Physical three-host recovery drills; a tested production OpenSearch cluster; outbox stale-delete tombstones; automated Gorse/RecBole checkpoint-to-prediction inference adapters; Prometheus/Grafana dashboards; full distributed availability/failover. This version solves **coordination**, not high availability.
