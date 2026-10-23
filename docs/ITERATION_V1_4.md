# V1.4 operational reliability iteration

Base: `ea15585e0a97f9c16ae9ea2068e8c0e878345ea9` (V1.3). This is still a three-host LAN prototype, not a production-ready multi-replica service.

## Changes delivered

| Area | Change | Limit |
| --- | --- | --- |
| HTTP integration | A shared `httpx.Client` connection pool across FastAPI endpoints, 350 ms connect timeout and provider-specific circuit breakers | Individual remote requests still have a 2-second total timeout; circuit state is process-local |
| API scheduling | Search, recommendation and outbox sync handlers now run in FastAPI's threadpool, so synchronous SQLAlchemy/HTTP calls no longer block the event loop | Long sync requests still occupy a worker thread |
| Circuit breaker | Three consecutive failures open a 15-second circuit; only one half-open probe is allowed; success resets the circuit | No cross-process distributed coordination |
| Anonymous writes | Weighted token buckets for visitor/session and high-budget proxy IP; HTTP 429 with Retry-After; impression batches consume tokens by event count | Process-local only, no durable anti-abuse or user authentication. IP behind Next proxy can be shared |
| Gorse outbox | `created_at`, `delivered_at`, claim/retention indexes; oldest-first due claims; explicit authenticated pruning of old **delivered** entries only | Old delivered rows with unknown timestamps are intentionally retained; no dead-letter queue yet |
| Monitoring | Response `X-Request-ID`, `Server-Timing`, admin-only outbox backlog age, circuit state, and provider fallback counters | Metrics are process-local and reset on restart; request logger omits search terms/cookies/keys |
| Historical popularity | SQL-only, idempotent backfill of distinct positive visitor-product interactions; both SQLite and PostgreSQL | Run with writes stopped; all-time uniques are still not time-decayed |
| Containers | API and sync execute as UID 10001, read-only root filesystem and tmpfs, volume ownership bootstrap, startup/readiness checks, bounded logs | Full multi-host Docker and Gorse/OpenSearch have not been live-tested |
| CI | Added Compose syntax validation for all three host manifests; Python tests exercise limits, circuits, outbox retention and metrics | Docker runtime and end-to-end browser tests are still separate exercises |

Gorse 0.5 REST API supports arrays for `POST /api/items` and `PUT /api/feedback`. This iteration preserves the existing batch upload and at-least-once outbox semantics. Successful remote upload followed by local crash may cause redelivery; `PUT /api/feedback` overwrites the same feedback key rather than incrementing its value. Do not replace it with accumulating `POST` without revisiting idempotency.

## Upgrade safely

1. Stop Mac `api` and `sync` containers before upgrading. Back up the SQLite database using the SQLite online backup facility (or copy the stopped Docker volume), including any required WAL state. Keep backups of the Docker Compose and `.env` configuration.
2. Update the repository on all hosts; review `.env.example`. `ADMIN_KEY` must have **at least 32 characters**, must not start with `replace-`, and should be generated randomly in production mode. Generate it via `openssl rand -hex 32`. Replace `SESSION_SECRET` with a distinct strong secret. Changing it invalidates anonymous sessions.
3. Run `docker compose --env-file .env -f infra/macos/compose.yaml up -d --build`. The one-shot `volume-init` job adjusts ownership on existing `commerce_data` volumes before launching the non-root API. Confirm the volume is backed up before running this job.
4. The existing additive upgrade adds `outbox.created_at`, `outbox.delivered_at` and indexes on startup. Historical rows have unknown times; they remain eligible for delivery, but not automatic date-based pruning. **This is not Alembic**. Do not start multiple schema-migrating API instances simultaneously.
5. Optional: rebuild unique-visitor popularity from existing event history while API/sync are stopped. For local Python, run `PYTHONPATH=services/api python scripts/backfill_popularity.py`. With Compose, while both API and sync are stopped, run:

   ```bash
   docker compose --env-file .env -f infra/macos/compose.yaml run --rm --no-deps api python /app/scripts/backfill_popularity.py
   docker compose --env-file .env -f infra/macos/compose.yaml up -d
   ```

   The backfill is idempotent and ignores impressions. It counts at most one positive action per visitor/product across historical click, favorite, cart, review and simulated purchase events.
6. Optional: purge confirmed delivered outbox messages older than 30 days by calling the admin API **from the Mac host**:

   ```bash
   curl -fsS -X POST -H "X-Admin-Key: ${ADMIN_KEY}" \
     'http://127.0.0.1:8000/api/admin/outbox/prune?days=30&limit=1000'
   ```

   This operation never deletes undelivered/leased events, and does not delete raw `events`. Back up the database first. Repeat batches if you need to delete many already-acknowledged rows.
7. Test `/health/ready`, the storefront and the admin metrics; verify `oldest_pending_seconds` eventually decreases when Gorse is healthy.

## Configurations

- `RATE_LIMIT_ENABLED=true`: set `false` only for a trusted, isolated load-test run.
- `RATE_LIMIT_PER_MINUTE=120`: token refill per minute per visitor.
- `RATE_LIMIT_BURST=120`: per-visitor initial burst capacity.

There is a higher allowance for requests from a shared proxy address. This is intentionally a **best-effort throttle** rather than account-level security. A serious public service needs shared rate limiting, proxy trust rules, CSRF/session design, an authenticated user system and more monitoring.

## Verification

- `PYTHONPATH=services/api pytest -q tests` locally: **33 passed, 1 skipped** (optional real PostgreSQL service is not available in this local environment).
- Python compile check and Compose YAML parsing succeeded. CI validates all Compose models and runs a PostgreSQL service-container test.
- Not verified here: real Docker build, three-host network reachability, Gorse 0.5.10 runtime behavior, OpenSearch indexing, long-running soak tests, frontend end-to-end interaction.

## Remaining high-priority work

1. One-shot Alembic migrations and SQLite-to-PostgreSQL data migration with integrity checks. Current lightweight `create_all`/`ALTER` path is not suitable for multi-worker startup.
2. SQLite concurrent-write benchmarking and a queue/batching plan, plus network failure injection on all three physical hosts.
3. Proper distributed rate limiting (Redis), admin isolation with network ACLs, full CSRF and identity protection.
4. PostgreSQL full-text/trigram fallback and OpenSearch incremental index outbox (current search updates need full rebuilds).
5. Unified offline evaluation of Gorse, RecBole and ItemCF with identical temporal split/candidate sets.
6. Live recommendation quality experiments and statistically meaningful A/B testing.
