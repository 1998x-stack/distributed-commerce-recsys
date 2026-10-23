# Distributed Commerce RecSys

A three-machine LAN e-commerce discovery and recommendation research project. **SQLite is the default business database**; PostgreSQL can be selected by configuration. Gorse maintains a *separate* PostgreSQL data store, because Gorse does not support SQLite as its data store.

> **V1.2 optimization audit:** [docs/OPTIMIZATION_AUDIT.md](docs/OPTIMIZATION_AUDIT.md) — completed fixes, open production risks and verification commands.

## Features

- Next.js storefront with product browsing, search, category filters, details and demo cart.
- FastAPI commerce API, SQLAlchemy models, session-based anonymous visitors, SQLite WAL and PostgreSQL mode.
- OpenSearch-backed product search with SQL degradation; Gorse distributed recommendations with local-interest/popularity fallback.
- Atomic raw-event + outbox persistence, retryable Gorse integration, dataset import from Amazon Reviews 2023 metadata.
- Separate Compose configurations for Ubuntu A (storage/search/master), Ubuntu B (worker/server), and macOS (web/API).

## Topology

```
Browser -> macOS:3000 Next.js -> macOS API:8000 -> macOS-local SQLite
                                          |-> Ubuntu A:9200 OpenSearch
                                          |-> Ubuntu B:8087 Gorse server
Ubuntu B Gorse worker/server -> Ubuntu A:8086 Gorse master
Ubuntu A: Gorse-only PostgreSQL + Redis + OpenSearch
```

**SQLite db files must never be NFS/SMB-shared between computers**. All business writes happen through the Mac API. Switching business DB to PostgreSQL does not migrate existing data automatically; use database migrations/export-import. Docker Compose files are local to each machine, not a cross-machine overlay network. This is a development LAN configuration, **not production HA**.

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

The Mac Compose file now runs a `sync` sidecar that invokes `scripts/sync_gorse.py` every 30 seconds. Use `docker compose -f infra/macos/compose.yaml logs -f sync` to inspect failures. Gorse offline models refresh periodically, so local fallback and newest-session reranking are separate from learned Gorse results.

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

This is a **LAN-only prototype**. The OpenSearch security plugin is disabled for development; never forward 9200 to the Internet. Use firewall allowlists, private Wi-Fi, and strong random session/admin secrets. Gorse upstream REST API must not be exposed to untrusted clients; only the FastAPI gateway calls it. No production auth, payments, stock, seller or checkout fulfillment is implemented. Anonymous sessions are not account authentication; do not use this as a real commerce platform.

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

## Limitations

V1 is a runnable research baseline, not full Taobao/JD functionality: single-node search storage, no distributed SQL, no payment system, no account login, no high-availability guarantees. `create_all` covers first-run schema creation; use Alembic migrations before persistent production use. Gorse eventual consistency and cold-start fallbacks should be evaluated separately. See `docs/ARCHITECTURE.md`.

### Search index rebuild in V1.2

`scripts/index_search.py` streams products in batches into a new versioned OpenSearch index and atomically promotes the `commerce-products` alias after the index is complete. The first upgrade replaces the old V1 index named `commerce-products`; subsequent rebuilds retain prior generation indexes (prune only after confirming the new one works). **Category filters require this rebuild** because `category` is now indexed as a `keyword` field.

### Testing limitations

Local tests verify SQLite/API workflows and mocked Gorse failure handling, but no Docker integration was run in the code-review environment. The project is still a LAN research prototype; consult the audit ledger before attempting production use.
