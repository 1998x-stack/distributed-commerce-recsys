# V1.3 iteration: verified changes, migration and outstanding risks

V1.3 addresses the independent V1.2 audit at commit `82bbbcabc3c29d333350a2202c26b34a51d96163`.
The default business DB remains SQLite; PostgreSQL remains selectable by `DB_DRIVER`.
This is a LAN-only research platform, not a production e-commerce service.

## Completed in this change

| Audit ID | Implementation | Verification |
| --- | --- | --- |
| V1 / G4 | Admin sync batch accepts 1–1000; sidecar drains successful backlogs quickly, sends arrays of up to 100 items/feedback per HTTP request | Mocked Gorse test; real Gorse 0.5.10 not run |
| V2 / R1 | Build interest candidates from multiple categories and use a 50% per-category diversity cap when alternatives exist | Multi-category test |
| R2 / R7 / R8 | Gorse results and session candidates now share a ranking pool; only cart/purchase exclude items; per-placement `source` and `request_id` | API contract tests; not an online recommendation lift test |
| R3 / F3 | `/api/events/batch` handles attributed, idempotent impressions; frontend IntersectionObserver logs when cards reach 50% visibility; Gorse `read_feedback_types=["impression"]` | API test; browser/Gorse not live-tested |
| V3 / A3 | Return slim product cards; full description in `/api/products/{id}` | Test covers >1KB description |
| V4 / R5 | Similar items reranked by title/brand/relative price and quality | API code; large real catalog needs benchmark |
| V5 | Idempotent duplicate `/api/events` is 200 rather than 201 | Test |
| V6 / R6 | Transactionally maintain unique-visitor popularity in `product_stats`; no full events scan during recommendation | Distinct visitor test |
| V7 / S1 | SQLite FTS5 with product INSERT/UPDATE/DELETE triggers; retains escaped LIKE fallback when FTS5 unavailable | Search/update/wildcard tests |
| V8 / A6 | SQLite WAL `synchronous=NORMAL`, `temp_store=MEMORY` | API code; see durability tradeoff below |
| V9 / M2 | Offline baseline uses **global** timestamp cut, novel-item targets, cosine-normalized ItemCF and capped per-user history | Global-time + repeat-item tests |
| A1 | Atomic SQLite/PG cart UPSERT and visitor-keyed idempotency with conflict detection | SQLite API tests; PG integration job added |
| A2 | Public `purchase_demo` event forbidden; only internal/historical events can use that label | Validation test; anonymous rate limiting remains open |
| I1 / I2 | Next rewrites are now an allowlist excluding `/api/admin/*`; API internal URL explicitly forwarded as Docker build arg | Code/config review; Next build not run locally |
| I3 / I4 | API healthcheck, healthy dependency startup, Docker context `.dockerignore` | YAML parse; Docker unavailable locally |
| A9 / G1 | Amazon metadata fallback `main_category`, `rating_number`; item metadata has stable snapshot timestamp | Import path, no real source archive in local tests |
| M1 | Optional Amazon Reviews positive interaction importer, deterministic deduplication and Gorse backfill outbox | Schema/normalizer test; full dataset not imported |
| I8 | Optional PostgreSQL CI job and more SQLite tests | 22 local tests pass, PG job needs CI runner |

## Upgrade existing SQLite safely

1. Stop API and sync containers. Take a **SQLite online backup** of the database; keep the `-wal` and `-shm` files until the backup completes. Do not just copy a running `.db` file. Back up the `commerce_data` Docker volume if using Compose.
2. Pull the new code and update all three machines when deploying Gorse config changes.
3. Startup still uses a lightweight additive schema upgrade: creates `cart_operations`, `product_visitors`, `product_stats`, and `products_fts` and adds `rating_count` to `products`. This is not a versioned migration. Run only one API startup/migration process at a time.
4. The first startup performs an FTS5 backfill of existing products. On large catalogs this may take time and extra disk space; run off-peak and ensure free space.
5. **Historical popularity is not backfilled** into `product_stats`; counters start from subsequent interactions. Use a dedicated migration/backfill before comparing new and old popularity scores.
6. Restart Gorse master with revised `read_feedback_types` and re-check model refresh. New impressions sent after deployment feed the read signal; historical impressions are not automatically replayed.

SQLite `synchronous=NORMAL` reduces per-transaction synchronization overhead in WAL mode but allows loss of the last committed transactions after an OS crash/power failure. For strongest durability set it to FULL and benchmark the latency.

## Import genuine interaction histories

```bash
PYTHONPATH=services/api python scripts/import_products.py --file meta_Electronics.jsonl.gz --limit 10000
PYTHONPATH=services/api python scripts/import_reviews.py --file Electronics.jsonl.gz --limit 100000
PYTHONPATH=services/api python ml/offline_baselines.py
```

The review importer keeps only stars >=4, pseudonymizes source IDs with deterministic UUIDs, requires matching products, and writes Gorse feedback through the transactional outbox. These IDs remain linkable within the study; pseudonymization is not an anonymization guarantee. Historical Amazon feedback is not the same as full browsing/impression logs. Do not pool training and test events from different datasets as if they share identities.

## Remaining high-priority items

- **Live three-node validation**: Gorse 0.5.10 item batch semantics, read-feedback model ingestion, item/feedback ordering, OpenSearch aliases, PostgreSQL startup and Docker networking.
- **Schema migration tooling**: proper Alembic versions, rollback procedures, and one-shot migration container; the legacy additive migration is for compatibility only.
- **Security**: per-client write rate limits, account authentication, CSRF hardening, abuse detection, admin isolation and secrets handling; LAN-only scope remains mandatory.
- **Read/write latency**: a shared HTTP client, circuit breaker and non-blocking DB calls on the async paths; current 2s timeouts are still observable fallback delays.
- **Search completeness**: Chinese tokenization, facets, synonyms, language analyzers, index delta sync, data cleanup and PostgreSQL full-text/trigram fallback.
- **Real evaluation**: one common data split/candidate pool for Gorse, RecBole and baselines, offline-to-serving model registry, and statistically meaningful controlled online experiments.
- **Scale**: `product_stats` counts all-time distinct visitors, not 7-day-decayed uniques; no cleanup or old-event lifecycle yet. SQLite remains a single-writer DB; moving to PG requires migration.
- **Frontend**: Next.js build and browser tests not run locally because npm packages were not cached; test browser impression instrumentation, modal keyboard accessibility, and screenshots in CI.

## Local checks performed

`PYTHONPATH=services/api pytest -q tests` => **22 passed, 1 skipped** (PostgreSQL integration requires `PG_TEST_URL`). Python compile and YAML parse passed. No Docker, Gorse, OpenSearch, PostgreSQL or Next production build were launched locally.
