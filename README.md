# Distributed Commerce RecSys

A three-machine LAN e-commerce discovery and recommendation research project. **SQLite is the default business database**; PostgreSQL can be selected by configuration. Gorse maintains a *separate* PostgreSQL data store, because Gorse does not support SQLite as its data store.

> **V1.6 evaluation, database copy and LAN benchmark:** [docs/ITERATION_V1_6.md](docs/ITERATION_V1_6.md). **V1.5 migrations and live catalog updates:** [docs/ITERATION_V1_5.md](docs/ITERATION_V1_5.md). Prior **V1.4 reliability:** [docs/ITERATION_V1_4.md](docs/ITERATION_V1_4.md) — HTTP reuse, circuit breakers, write throttling, safe outbox cleanup, popularity backfill and container hardening. Prior **V1.3 optimization:** [docs/ITERATION_V1_3.md](docs/ITERATION_V1_3.md) — verified fixes, upgrade notes and remaining risks. Earlier audit: [docs/OPTIMIZATION_AUDIT.md](docs/OPTIMIZATION_AUDIT.md).

> **V1.8 catalog withdrawal and offline model adapters:** [docs/ITERATION_V1_8.md](docs/ITERATION_V1_8.md).

## Features

- Next.js storefront with product browsing, search, category filters, details and demo cart.
- FastAPI commerce API, SQLAlchemy models, session-based anonymous visitors, SQLite WAL and PostgreSQL mode.
- OpenSearch-backed product search with SQLite FTS5 / SQL LIKE degradation; Gorse distributed recommendations with local-interest/popularity fallback.
- Alembic schema versioning on production startup, catalog soft deletion / recovery, and a separate durable OpenSearch incremental-update sidecar.
- Atomic raw-event + outbox persistence, batched Gorse integration, Amazon Reviews 2023 product metadata and optional positive review-history import.
- Separate Compose configurations for Ubuntu A (storage/search/master), Ubuntu B (worker/server), and macOS (web/API).

## Topology

```
Browser -> macOS:3000 Next.js -> macOS API:8000 -> macOS-local SQLite
                                          |-> Ubuntu A:9200 OpenSearch
                                          |-> Ubuntu B:8087 Gorse server
Ubuntu B Gorse worker/server -> Ubuntu A:8086 Gorse master
Ubuntu A: Gorse-only PostgreSQL + Redis + OpenSearch
```

**SQLite db files must never be NFS/SMB-shared between computers**. All business writes happen through the Mac API. Switching business DB to PostgreSQL does not migrate existing data automatically; for an offline, verified one-time copy use `scripts/migrate_sqlite_to_postgres.py` (see V1.6 guide). Docker Compose files are local to each machine, not a cross-machine overlay network. This is a development LAN configuration, **not production HA**.

## Quickstart: local API without external infrastructure

Requires Python >=3.11. Clone this repository onto macOS:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r services/api/requirements.txt pytest
export SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export ADMIN_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export SQLITE_PATH=./data/commerce.db
PYTHONPATH=services/api python scripts/import_products.py --demo
PYTHONPATH=services/api uvicorn app:app --reload --port 8000
```

In another terminal (Node 22):

```bash
cd apps/storefront
npm install
npm run dev
# http://localhost:3000
```

Without Gorse / OpenSearch, the API intentionally uses SQL search and interest + popularity fallback recommendations.

## Three-machine LAN deployment

1. Assign reserved IP addresses to each machine and ensure bidirectional LAN reachability. Install Docker Engine / Docker Compose on both Ubuntu hosts and Docker Desktop on macOS.
2. Clone the repository onto *each* machine. Run `cp .env.example .env`; replace all IP addresses and placeholder secrets. Keep `.env` out of Git. Generate secrets using `openssl rand -hex 32`.
3. On **Ubuntu A**: `docker compose --env-file .env -f infra/ubuntu-a/compose.yaml up -d`
4. On **Ubuntu B**: `docker compose --env-file .env -f infra/ubuntu-b/compose.yaml up -d`
5. Set `ALLOWED_ORIGINS` to the storefront origin (e.g. `http://192.168.1.103:3000`) and update all secrets. On **macOS**: `docker compose --env-file .env -f infra/macos/compose.yaml up -d --build`
6. On Mac import demo products into the **Docker volume**, not the host SQLite file:

```bash
docker compose --env-file .env -f infra/macos/compose.yaml exec api python /app/scripts/import_products.py --demo
```

The command above requires importer scripts in the API image (included). Then index search / sync Gorse:

```bash
# inside Mac API container (indexing; imports need to finish first)
docker compose --env-file .env -f infra/macos/compose.yaml exec api python /app/scripts/index_search.py
# sync a batch of durable outbox messages
curl -X POST -H "X-Admin-Key: $ADMIN_KEY" 'http://127.0.0.1:8000/api/admin/sync?batch=1000'
```

The Mac Compose file runs `migrate` before the API, then separate `sync` (Gorse) and `search-sync` (OpenSearch) sidecars. See the [V1.5 upgrade notes](docs/ITERATION_V1_5.md) before a full alias rebuild.

The Mac Compose file runs a `sync` sidecar that drains successful backlog batches rapidly, then checks again after an idle pause. Use `docker compose -f infra/macos/compose.yaml logs -f sync` to inspect failures. Gorse offline models refresh periodically, while the API also reranks learned candidates using current-session category interest. Recommendations include an attributed slate ID; impression events are recorded only when cards enter the viewport.

### Database switching

```
# SQLite (default)
DB_DRIVER=sqlite
SQLITE_PATH=/app/data/commerce.db

# PostgreSQL (business data, additional commerce DB and user required)
DB_DRIVER=postgresql
DATABASE_URL_POSTGRES=postgresql://commerce:strong-secret@UBUNTU_A_IP:5432/commerce
```

PostgreSQL in Ubuntu A's Compose is initially **Gorse-only**, not a pre-created commerce database. For PostgreSQL business mode provision a **separate role/database** and publish access on the private LAN with authentication and firewall rules; do not reuse Gorse's credentials/database.

### Security

This is a **LAN-only prototype**. The OpenSearch security plugin is disabled for development; never forward 9200 to the Internet. Use firewall allowlists, private Wi-Fi, and strong random session/admin secrets. Gorse upstream REST API must not be exposed to untrusted clients; only the FastAPI gateway calls it. No production auth, payments, stock, seller or checkout fulfillment is implemented. Anonymous clients cannot submit simulated purchases. V1.4 adds **best-effort process-local write rate limiting** (configure `RATE_LIMIT_*` in `.env`), but durable abuse detection and authenticated user accounts remain pending. Anonymous sessions are not account authentication; do not use this as a real commerce platform.

### Verification and tests

```bash
PYTHONPATH=services/api pytest -q tests
source .env
bash scripts/check-lan.sh
curl http://127.0.0.1:8000/api/products
curl 'http://127.0.0.1:8000/api/search?q=headphones'
```

## Data sources

- Amazon Reviews 2023 metadata: https://amazon-reviews-2023.github.io/main.html (download category `meta_*.jsonl.gz`; read rights before redistribution).
- First local run uses **synthetic demo products**, no dataset download required.
- `PYTHONPATH=services/api python scripts/import_products.py --file PATH_TO_META.jsonl.gz --limit 10000`
- Do not distribute original protected images, private user data, or third-party dataset files in Git.

## V1.6 reproducible evaluation and database migration

- `ml/evaluation_cli.py prepare` produces a fixed, train-only global-time snapshot; `score` checks external Gorse/RecBole prediction JSONL against the same users, candidate pool and metrics. See [V1.6 protocol](docs/ITERATION_V1_6.md).
- `scripts/migrate_sqlite_to_postgres.py --sqlite PATH` is a **dry run**; `--execute` copies into an EMPTY separately provisioned business PostgreSQL schema with transactional verification. Stop all writers and back up SQLite before switching.
- `scripts/bench_api.py` offers bounded read-only private LAN latency/throughput measurements; it does not replace real three-node fault-injection tests.

## Limitations

V1 is a runnable research baseline, not full Taobao/JD functionality: single-node search storage, no distributed SQL, no payment system, no account login, no high-availability guarantees. Production uses versioned Alembic migrations (V1.5+); local development retains `create_all` for convenience. Back up before upgrades and never run concurrent schema updaters. Gorse eventual consistency and cold-start fallbacks should be evaluated separately. See `docs/ARCHITECTURE.md`.

### Search index rebuild in V1.2

`scripts/index_search.py` streams products in batches into a new versioned OpenSearch index and atomically promotes the `commerce-products` alias after the index is complete. The first upgrade replaces the old V1 index named `commerce-products`; subsequent rebuilds retain prior generation indexes (prune only after confirming the new one works). **Category filters require this rebuild** because `category` is now indexed as a `keyword` field.

### Testing limitations

Local tests verify SQLite/API workflows and mocked Gorse failure handling, but no Docker integration was run in the code-review environment. The project is still a LAN research prototype; consult the audit ledger before attempting production use.

### V1.3 local data model and upgrade

- Lists and recommendations intentionally omit long descriptions; fetch `/api/products/{id}` for details.
- SQLite FTS5 is optional and searches indexed words; complex Chinese segmentation, exact substring semantics, and PostgreSQL full-text performance require further work.
- Unique-visitor popularity is transactional but counts only events observed after V1.3 deployment, without historical backfill or time decay.
- The optional review importer uses high-rated Amazon reviews as historical positives, not as an unbiased clickstream.
- Back up the SQLite volume before the additive schema upgrade; follow [V1.3 migration notes](docs/ITERATION_V1_3.md).

### V1.4: operational reliability

- Gorse and OpenSearch share a pooled HTTP transport with 15-second circuit-breaker cooldown after three consecutive failures. API network + SQL calls run in threadpool handlers.
- Visitor-scoped event/cart writes are rate-limited (429 on excess). This protects the **single-process LAN demo**, not a multi-replica public service.
- Admin metrics include provider fallback counts, circuit state and oldest pending outbox age; responses carry `X-Request-ID` and `Server-Timing`.
- Outbox rows have delivery timestamps and due-claim indexes. Safely delete acknowledged records using `POST /api/admin/outbox/prune?days=30` with `X-Admin-Key`; pending rows are never purged by this endpoint.
- `scripts/backfill_popularity.py` rebuilds unique-visitor counts from historical positive feedback entirely inside SQLite/PostgreSQL. Stop writes while running.
- Mac API and sync containers run as non-root users with read-only root filesystems; a one-shot `volume-init` handles existing data volume permissions.
- **Before upgrading:** follow the backup, schema and migration steps in [the V1.4 guide](docs/ITERATION_V1_4.md).


## V1.7: coordinated search reindex & diagnostics

V1.7 adds the SQL-backed `search_maintenance` gate and Alembic revision
`0002_v17_search_gate`. During a full OpenSearch alias rebuild, the incremental
search Outbox sync endpoint responds HTTP 423, preserves pending events and
resumes after promotion. Current product imports remain permitted and their
updates replay after the alias swap. One in-flight sync must finish before
indexing begins; duplicate rebuilds are rejected. See
[docs/ITERATION_V1_7.md](docs/ITERATION_V1_7.md) for the migration, operating
procedure, eventual-consistency boundaries, and restoration instructions.

Read-only LAN status from another machine:

```bash
python scripts/diagnose_lan.py --mac 192.168.1.20 --ubuntu-a 192.168.1.21 --ubuntu-b 192.168.1.22 --json
```

These sample IPs must be replaced with your actual private LAN addresses.
No physical three-node outage or live Gorse/OpenSearch end-to-end test has yet
been performed in this development environment.

## V1.8: withdraw and restore a product (Mac admin API)

V1.8 database head is `0003_v18_catalog_visibility`. A withdrawn product stays
in the business database and historic events for evaluation, but becomes
invisible to catalog, search, recommendations and carts immediately. Both Gorse
(`IsHidden`) and OpenSearch (bulk `delete`) get durable outbox updates. An Amazon
metadata re-import will **not** automatically restore a manually withdrawn item.

```bash
# Run on the Mac host; the admin API binds to 127.0.0.1:8000, not the storefront.
curl -X PATCH http://127.0.0.1:8000/api/admin/products/demo:0/availability \
  -H "X-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"active":false}'
# Restore the same way using {"active":true}.
```

Snapshot-trained model scores can now be converted with `ml/adapt_predictions.py`;
see `ml/README.md`. A read-only manual LAN recovery observer is available via
`scripts/probe_recovery.py`. See the V1.8 upgrade guide for requirements and
limitations.
