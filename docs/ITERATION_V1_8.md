# V1.8 — Catalog withdrawal, model score adapters, and LAN recovery observations

## Shipped

- `products.is_active` is a non-null Boolean with a default of `true`. Alembic `0003_v18_catalog_visibility` adds the field without deleting existing business rows, plus an index. Both SQLite and PostgreSQL remain supported; the **default business database is SQLite**. Gorse continues using its own PostgreSQL.
- An authenticated Mac-only admin API route, `PATCH /api/admin/products/{product_id}/availability`, accepts `{"active": false}` to withdraw and `{"active": true}` to restore. Repeating the same operation is a no-op. The Next.js storefront does **not** proxy admin routes.
- Withdrawal immediately excludes a product from the catalog, categories, details, FTS5/SQL and OpenSearch-backed search results, recommendations, similar items, and new events/carts. Existing carts for that product are cleared. Historical feedback remains for research and audits.
- In the *same business database transaction*, changes are queued to both Gorse (an `Item` with `IsHidden=true`) and OpenSearch (a bulk `delete` action). OpenSearch delete of an already-missing document is treated as success; other failed items remain queued with retries. Restoring queues a Gorse visible item and an OpenSearch `index` document.
- The full index rebuild streams **active-only** products. A re-import refreshes metadata without reversing an administrator takedown. Only an explicit authenticated restore reactivates the item.
- `ml/adapt_predictions.py` converts external model score tables (CSV/TSV) into JSONL top-K predictions for V1.6's common snapshot evaluator. It validates unknown users, seen/out-of-catalog candidates, duplicates, and nonfinite scores; **no live Gorse API is used**.
- `scripts/probe_recovery.py` makes bounded, read-only health observations of the three LAN nodes during a *manual* fault drill. It records transitions and UTC timestamps in JSONL; it does not induce failures.

## Upgrade safely

1. On the Mac, stop API, import, Gorse sync, search-sync and storefront writers. Take a consistent SQLite backup (including WAL state); for PostgreSQL, take a transactional database backup. Preserve `.env` and access keys. Do not copy an open SQLite `.db` without accounting for its WAL file.
2. Update code on all three hosts (`git pull origin main`), then deploy the Ubuntu services and Mac Compose as documented in the root README. The Mac `migrate` one-shot job will run `alembic upgrade head` **before** the API starts.
3. Verify the Mac migration completed and `alembic_version.version_num` reads `0003_v18_catalog_visibility`. An API configured with `SCHEMA_MODE=external` will refuse an outdated schema.
4. Complete an initial OpenSearch full index build if needed. Run `search-sync` and `sync` until pending catalog events are acknowledged. Observe failures/backlog through the authenticated `/api/admin/metrics` endpoint.

For a local Mac admin test (only the API container's localhost-bound host port, not Next.js):

```bash
curl -X PATCH 'http://127.0.0.1:8000/api/admin/products/demo:0/availability' \
  -H "X-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"active":false}'
# Verify immediate public 404; then restore with {"active":true}.
```

To collect health transitions during a **manual** reboot/disconnection test in your own lab:

```bash
python scripts/probe_recovery.py \
  --mac 192.168.1.20 --ubuntu-a 192.168.1.21 --ubuntu-b 192.168.1.22 \
  --samples 60 --interval 5 --output recovery-test.jsonl
```

Only private IPv4/loopback addresses are accepted. These probes are **not** a full application-level distributed recovery or load test; no remote services are changed by this script. The exact recovery time is bounded by the probe interval and network timeouts.

## Offline comparison with RecBole or snapshot-trained Gorse

```bash
# Use the same V1.6 train.inter and catalog snapshot as every baseline.
python ml/adapt_predictions.py --snapshot ml/data/eval-v16 \
  --input recbole-scores.tsv --output recbole-predictions.jsonl
python ml/evaluation_cli.py score --snapshot ml/data/eval-v16 \
  --name recbole --predictions recbole-predictions.jsonl
```

The score input must have `user_id`, `item_id`, `score` columns. Use `--delimiter comma` if it is a CSV. This adapter does not export model logits or run training itself: generate test-candidate scores *from a model trained only on the snapshot's training data*. Calling a live Gorse instance backed by newer historical data invalidates the offline comparison.

## Limits and future work

- Local tests use SQLite and mocked search/Gorse, with optional real PostgreSQL tests in CI. No three-host fault injection has been executed on the user's hardware, and no live Gorse/OpenSearch deletion response or realistic million-product throughput has been measured.
- Remote outbox delivery is at-least-once and the same product's remote writes may briefly overlap with a newer catalog update. Local queries always honor `is_active`, but there is still a **remote last-writer race** if the previous request completes after a newer update. A next iteration should add a per-item serialized delivery/version fence for strict convergence under races, and a scheduled reconciliation process.
- Catalog soft deletion preserves past rows but is **not** a data-erasure policy. Handling personal-data deletion requires its own privacy lifecycle.
- The LAN diagnostics are read-only; production-grade monitoring, dashboards, trace correlation and fault drills remain open.
- The external model adapter standardizes scores but does not automatically run Gorse or RecBole training/inference.
