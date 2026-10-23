# Architecture and hard boundaries

## Data plane

- **Business SoT**: SQLite on Mac writable only by FastAPI (default). Optional PostgreSQL using SQLAlchemy engine and same ORM classes.
- **Index**: OpenSearch on Ubuntu A, imported from authoritative product rows; any failed search request falls back to SQL query.
- **Recommendation**: Gorse Master on A, Gorse Worker/Server on B; Gorse has independent PostgreSQL storage and Redis cache.
- **Behavior**: Browser -> FastAPI -> SQL raw Event and durable Outbox in one transaction. The sync worker POSTs items/feedback to Gorse. Raw events contain repeated interactions; Gorse may coalesce feedback by key.
- **Session**: Signed httponly same-site cookie; new anonymous visitor ID created server-side. Not a production auth system.

## Performance evolution

SQLite is ideal for single-writer prototypes and smaller data. For write-heavy real multi-service workloads, use business PostgreSQL, transactional migrations and replication. A + B are independently hosted services but Gorse/PostgreSQL/OpenSearch are not HA. Connect hosts by reserved LAN IP / LAN DNS, **not** container service names across host boundaries. Do not mount the SQLite file over network shares.

## Operational workflow

1. Check host hardware/network, install Docker and set `.env`.
2. Start A then B then Mac.
3. `import_products.py` seeds data and outbox; `index_search.py` rebuilds the search index; `sync_gorse.py` drains outbox.
4. Check `/health/ready`, `/api/health`, Gorse `/api/health/ready`, OpenSearch root.
5. Query product, send feedback event, retrieve recommendation, compare strategies.
6. Save backups before switching database types. Migration between SQLite and PG requires a separate transfer step; `DB_DRIVER` switch by itself does not copy data.

## Next milestones

- Alembic with dialect-specific migration tests and SQLite-to-PG export/import command.
- Dedicated write-through indexing queue and idempotent workers; event dead-letter handling.
- Hybrid BM25/vector search, RRF, item and user features, learned ranker, A/B testing.
- Split item ranking into a dedicated service; expose trace IDs and OpenTelemetry dashboards.
- User registration/auth, rate limits, CSRF controls, privacy/retention policies.
