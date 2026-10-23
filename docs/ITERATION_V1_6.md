# V1.6 — Reproducible offline evaluation, database copy and bounded LAN benchmarking

**Scope:** Small research prototype on macOS + Ubuntu A + Ubuntu B; default business database remains SQLite. This release does not change API response formats, add new SQL tables, or alter the separate Gorse PostgreSQL store. **No real three-machine LAN tests were performed in the development environment.**

## 1. One leakage-aware evaluation protocol

`ml/evaluation_protocol.py` and `ml/evaluation_cli.py` implement `global-temporal-novel-item-v1`:

- Sort all positive `click`, `favorite`, `cart`, `purchase_demo` or imported `review` events by timestamp. Split the **entire** population on one global UTC cutoff (supplied, or a chronological 80% boundary). All training interactions are strictly at or before that cutoff. The held-out target is each eligible user's **first novel** post-cutoff product that was already present in the training product universe. Users require at least two distinct training items; unanswerable targets are excluded, and the output reports the eligible user count.
- Each algorithm predicts from the **same** training catalog minus **all** seen pre-cutoff products for that user. ItemCF uses bounded unique histories, cosine-normalized co-occurrence, and a popularity fill. Popularity counts distinct training users, not raw repeated clicks. Both baselines are deterministic and score the same `Recall@K`/`NDCG@K` on the same users. This protocol deliberately measures novel-item retrieval, not repeat purchase or logged CTR.
- `prepare` writes `manifest.json` (including a source data fingerprint, file checksums and `k`), `catalog.json`, `targets.jsonl` (held-out truth and seen training IDs), `train.inter` (training-only RecBole atomic interactions), baseline predictions and metrics. The output directory must **not** already exist. Private training files live under ignored `ml/data/` and must not be published.
- To compare another model, emit **one JSON object per user** in a JSONL file: `{"user_id":"visitor-1","items":["product-a","product-b"]}`. `score` rejects duplicate items, unknown users or candidates outside that user's eligible pool. Missing users count as misses; the submitted-user coverage is reported. All results are tied to the same snapshot fingerprint.

```bash
# From repo root on the Mac, using the same SQLite/PostgreSQL business config as the API
PYTHONPATH=services/api python ml/evaluation_cli.py prepare \
  --cutoff 2026-10-01T00:00:00Z --k 20 --output ml/data/eval-2026-10-01

# Re-score the saved popularity baseline (or substitute Gorse/RecBole prediction JSONL)
python ml/evaluation_cli.py score --snapshot ml/data/eval-2026-10-01 \
  --name popularity --predictions ml/data/eval-2026-10-01/popularity.jsonl

# Export TRAIN ONLY interactions for an optional RecBole setup
python ml/prepare_recbole.py --snapshot ml/data/eval-2026-10-01
```

**Important qualification:** `train_recbole.py` still invokes RecBole's native training/validation machinery, whose metrics and split are **not** the above protocol. To make a defensible cross-model comparison, train with the saved pre-cutoff interactions only, produce top-K predictions for snapshot users against the saved candidate pool, and score those predictions with `evaluation_cli.py`. This release provides the scorer and input contract, not automated live Gorse inference or a complete RecBole checkpoint-to-ranking adapter. Gorse can also differ due to training refresh and data ingestion; do not compare a live Gorse slate without controlling training data and cutoff.

The history is biased by the original display/exposure policy. No negative exposure model, causal debiasing, confidence intervals, or randomized online A/B test is implemented. Mixed Amazon historical review timestamps and fresh demo click data often give unsuitable splits; inspect `users` and `train_products` before interpreting scores.

## 2. Safely copying SQLite business data into PostgreSQL

`python scripts/migrate_sqlite_to_postgres.py` is an **offline copy** for users wishing to switch the business backend; it does **not** migrate the Gorse database. It does not create or reset databases. It requires both source SQLite and target *separate business PostgreSQL* to be at Alembic revision `0001_v15_baseline` and the target's business tables to be **empty**. A migration to a non-empty PostgreSQL destination is rejected rather than merged.

**Maintenance runbook:**

1. Stop `api`, `sync`, `search-sync`, and all product/review importers on the Mac. Leave the SQLite Docker volume intact. Take a consistent SQLite backup with the database closed or the SQLite backup API — do not just copy an active WAL `.db` without its WAL. Do not start another API or importer until the cutover decision is complete.
2. Provision a separate business PostgreSQL database and role; **never point at Gorse's own PostgreSQL database**. Restrict traffic to the LAN, back up the target, and run `alembic upgrade head` on that empty PostgreSQL database. Do not use an already-populated production database.
3. Locate the persistent SQLite file (Mac Compose currently mounts it at `/app/data/commerce.db` inside the API/migration container). For an offline host-side migration, copy it safely into a local file and provide its real host path; the Docker volume is not automatically present at `./data/commerce.db`. Install the API Python requirements.
4. Verify the SQLite source in read-only mode. The default command is **dry-run** and prints row counts, with SQLite integrity and foreign-key checks:

```bash
python scripts/migrate_sqlite_to_postgres.py --sqlite /secure/backup/commerce.db

# Set the separate business PG URL securely via the environment (never commit it).
export DATABASE_URL_POSTGRES='postgresql://commerce:REPLACE_ME@192.168.1.10:5432/commerce'
python scripts/migrate_sqlite_to_postgres.py --sqlite /secure/backup/commerce.db --execute
```

5. The actual importer takes exclusive locks on target business tables, verifies each table's row count and a canonical row SHA256 within **one PostgreSQL transaction**, then commits all rows or rolls back. It restores autoincrement sequence state for `cart` and `product_visitors` as needed. Existing target rows cause an error. It includes `products`, events, outbox, cart, stats and other ORM-managed business tables; SQLite FTS virtual tables are not copied because PostgreSQL search is a different implementation.
6. Only after a successful copy, set `DB_DRIVER=postgresql` and `DATABASE_URL_POSTGRES` for the Mac API and migration containers, then start the Mac stack. Keep the SQLite backup for rollback and verify products, recent user sessions, cart operations and pending Outbox tasks. **Do not run two active business databases**: subsequent writes to one will not replicate to the other. Repointing the API back to the old SQLite backup after accepting PostgreSQL writes would discard those newer writes.

The source file is verified as a single SQLite read snapshot; writers should still be stopped to avoid the backup/cutover gap. SHA256 digests detect accidental data differences; they are **not** cryptographic audit proofs against a malicious source. A very large dataset may need a tuned batch size and a longer maintenance window. No general Alembic downgrade or rollback of writes is provided.

## 3. Read-only LAN performance baseline

The script `scripts/bench_api.py` reports status distribution, throughput, P50/P95/P99. Only GET requests to `/api/products`, `/api/search`, `/api/recommendations` or `/health` are accepted, and the base must use localhost or a literal private/loopback IP. It rejects public destinations and limits the benchmark to 2,000 requests with concurrency <=32.

```bash
# On Ubuntu, benchmark through the LAN-exposed Next.js /api/* proxy (port 3000).
python scripts/bench_api.py --url http://192.168.1.103:3000 \
  --path '/api/search?q=headphones' --requests 200 --concurrency 8
```

For API-direct timing run the same script **on macOS** using `--url http://127.0.0.1:8000` (Compose intentionally binds API port 8000 to loopback). From Ubuntu use the storefront proxy at the Mac LAN IP on port 3000. Results depend on catalog size, cache warmth, OpenSearch/Gorse health, network and client placement. Run separate cold/warm benchmarks; a localhost mock test is not three-machine throughput verification.

## 4. Verification and remaining work

```bash
PYTHONPATH=services/api:ml pytest -q tests
python -m compileall -q services/api scripts ml migrations
```

GitHub CI includes a **disposable PostgreSQL** integration test for the cross-database copy; in environments without a local PostgreSQL service that test is skipped. Actual OpenSearch/Gorse/Docker/LAN behavior remains unverified until physical test runs.

**Deferred:** multi-host failover drills; strict full-alias-reindex and incremental-outbox coordination; search deletion tombstones; shared Redis limiter; unified automated Gorse/RecBole inference adapters; dedicated monitoring dashboard.
