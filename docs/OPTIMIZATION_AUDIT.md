# V1.2 Code audit and optimization ledger

Scope: repository reviewed across API, importer, indexer, frontend, Gorse, Compose, ML baselines and CI on 2026-10-23. The tests cover local SQLite/HTTP mocks, **not** live three-host integration.

## Implemented

| Level | Finding | Change | Validation |
|---|---|---|---|
| P0 | Recommendation loaded first 5,000 products and did Python full sort/linear lookups | Bounded SQL retrieval: recent interests, Gorse IDs, popular candidates, rating fallback | >5,000 product test |
| P0 | Concurrent sync workers could send same outbox entry | Atomic SQL `UPDATE ... RETURNING` claim with lease token, bounded parallel HTTP and conditional ACK | Lease test, mocked Gorse test |
| P0 | Transient Gorse failures were immediately retried forever | Exponential backoff, capped at 30 minutes, and stored truncated error | Retry test |
| P0 | Reimported item metadata was never requeued after first import | Deterministic per-item outbox now reopens when payload changes; cancels stale lease | Code review (add an integration test for importer) |
| P1 | Existing SQLite lacked outbox claim columns | Additive schema upgrade at startup; preserves existing V1 outbox rows | Legacy schema test |
| P1 | Bulk indexing loaded every product into RAM | SQLAlchemy `yield_per` pages; bulk requests up to configurable batch size | Static code inspection; needs live OpenSearch test |
| P1 | Rebuilding search index risked serving half-built results | Build into versioned index, then `_aliases` promotion | Static code inspection; needs live OpenSearch test |
| P1 | Client-provided idempotency key was global; duplicate key with changed payload incorrectly succeeded | Server-side visitor-scoped hash, payload conflict HTTP 409, race handling | Idempotency tests |
| P1 | Search fallback treated `%` and `_` as SQL wildcard | Escape wildcard characters | Search test |
| P1 | Any cross-origin browser could attempt state-changing routes | Deny non-allowlisted Origin on write methods; keep secure cookie option | Origin test |
| P1 | Frontend could display stale results on rapid filter/search changes | Abort stale fetch requests and skip outdated state updates | Code review; frontend build not run here |
| P2 | Sync required a manual forever-running script | Compose sync sidecar on Mac; API remains sole SQLite writer | Compose YAML validation |
| P2 | CI did not include frontend build | GitHub Actions Node 22 build job | GitHub Actions only |

## Still required before public or high-volume deployment

1. **Migrations**: the V1-to-V1.2 schema upgrade is intentionally additive. Introduce Alembic, migration locking and reversible revisions before multi-instance deployment. Back up SQLite (`sqlite3 .backup`) before upgrading.
2. **Async DB usage**: async search/recommendation endpoints still use synchronous SQLAlchemy. Switch to `AsyncSession` or move whole handlers into a bounded worker pool after profiling event-loop stalls.
3. **Search updates**: alias promotion is full index rebuild, not near-real-time CDC. Add change-stream/outbox indexing and safe retirement of old generations. Query `category` as keyword after reindexing to the new mapping.
4. **Performance**: fallback `popularity` still performs a grouped aggregation over historical events. Materialize popularity and trending features periodically or cache in Redis; verify P95 under a defined dataset/concurrency.
5. **Idempotency/outbox**: event idempotency is one key per visitor and payload; Gorse `PUT /api/feedback` overwrites the same `(type,user,item)` combination, so repeated clicks are retained in raw event log but not counted repeatedly by Gorse. Failed rows have retry/backoff but no dead-letter cutoff yet.
6. **Gorse security**: use private LAN firewall allowlists and set optional `GORSE_API_KEY` (applies to `GORSE_SERVER_API_KEY` in Gorse Master) and enforce LAN firewall rules; confirm it propagates to Server on live version. Never expose unauthenticated OpenSearch, Gorse, Redis or PostgreSQL to the public Internet.
7. **Authentication**: signed anonymous sessions are not full accounts. Add registration, ownership checks, consent/retention, opt-out and CSRF tokens for a production internet deployment. Origin checks complement, but do not replace, CSRF tokens.
8. **Reproducibility**: backend uses semver ranges; frontend currently has no package-lock because npm registry access was unavailable in the audit environment. Generate and commit `package-lock.json` with `npm install --package-lock-only`, use `npm ci` in CI, and pin images by digest after verification.
9. **Observability**: structured request logging with redaction, Prometheus metrics and distributed traces still needed. Don't log secrets/session cookies.
10. **Compatibility**: execute Compose config, three-host LAN tests, macOS Docker Desktop test, Gorse 0.5.10 compatibility, and OpenSearch alias switch with real containers. The audit runner has no Docker executable and npm could not contact its registry.

## Local verification

```bash
PYTHONPATH=services/api python -m pytest -q tests
python -m compileall -q services/api scripts ml
python - <<'PY'
from pathlib import Path
import yaml
for f in Path('infra').glob('*/compose.yaml'):
    data = yaml.safe_load(f.read_text())
    assert data['services']
    print(f, 'OK')
PY
```

## Rollout

- Back up existing `commerce.db` using SQLite's backup command while writes are quiesced or through SQLite backup API.
- Update `.env` (`ALLOWED_ORIGINS`, strong `SESSION_SECRET`), then deploy Ubuntu A, Ubuntu B, Mac.
- Check `/health/ready`, then run `scripts/index_search.py` to promote the new keyword-category index.
- Mac's sync sidecar drains outbox every 30 seconds. Verify `/api/admin/metrics` backlog decreases.
- Roll back code and database from backup if migrations or configuration cause a regression; do **not** point old V1 at new schemas without verifying compatibility.
