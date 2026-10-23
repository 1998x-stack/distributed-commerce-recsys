"""Commerce API: SQLite-first, PostgreSQL-ready, with search/recs degradation."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import time
import math
import re
from collections import defaultdict
import os
import secrets
from contextlib import asynccontextmanager
from operational import CircuitBreaker, TokenBuckets, MetricsCounters
from search_coordination import sync_gate_open
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Index, create_engine, event as sqla_event, func, inspect, or_, select, text, update, case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("commerce.api")


def settings() -> dict:
    config_file = Path(os.getenv('APP_CONFIG', 'config/app.yaml'))
    config = yaml.safe_load(config_file.read_text()) if config_file.exists() else {}
    cfg = config or {}
    cfg.setdefault('database', {})
    cfg.setdefault('services', {})
    cfg['database']['driver'] = os.getenv('DB_DRIVER', cfg['database'].get('driver', 'sqlite'))
    cfg['database']['sqlite_path'] = os.getenv('SQLITE_PATH', cfg['database'].get('sqlite_path', './data/commerce.db'))
    cfg['database']['postgres_url'] = os.getenv('DATABASE_URL_POSTGRES', cfg['database'].get('postgres_url', ''))
    cfg['services']['search'] = os.getenv('OPENSEARCH_URL', cfg['services'].get('search', ''))
    cfg['services']['gorse'] = os.getenv('GORSE_URL', cfg['services'].get('gorse', ''))
    return cfg


CONFIG = settings()
DRIVER = CONFIG['database']['driver']
if DRIVER not in ('sqlite', 'postgresql'):
    raise RuntimeError('DB_DRIVER must be sqlite or postgresql')
if DRIVER == 'sqlite':
    db_file = Path(CONFIG['database']['sqlite_path']).resolve()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    DSN = f'sqlite:///{db_file}'
else:
    DSN = CONFIG['database']['postgres_url']
    if not DSN.startswith(('postgresql://', 'postgresql+psycopg://')):
        raise RuntimeError('PostgreSQL mode requires DATABASE_URL_POSTGRES')
    if DSN.startswith('postgresql://'):
        DSN = DSN.replace('postgresql://', 'postgresql+psycopg://', 1)

engine = create_engine(DSN, pool_pre_ping=True, connect_args={'timeout': 10} if DRIVER == 'sqlite' else {})
if DRIVER == 'sqlite':
    @sqla_event.listens_for(engine, 'connect')
    def sqlite_pragmas(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute('PRAGMA journal_mode=WAL')
        cur.execute('PRAGMA busy_timeout=5000')
        cur.execute('PRAGMA foreign_keys=ON')
        # NORMAL trades durability against host power loss for lower write latency.
        cur.execute('PRAGMA synchronous=NORMAL')
        cur.execute('PRAGMA temp_store=MEMORY')
        cur.close()
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = 'products'
    __table_args__ = (Index('ix_products_category_rating', 'category', 'rating', 'id'),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), default='demo', index=True)
    source_id: Mapped[str] = mapped_column(String(128), default='')
    title: Mapped[str] = mapped_column(String(500), index=True)
    description: Mapped[str] = mapped_column(Text, default='')
    category: Mapped[str] = mapped_column(String(100), index=True)
    brand: Mapped[str] = mapped_column(String(100), default='')
    price: Mapped[float] = mapped_column(Float, default=0)
    currency: Mapped[str] = mapped_column(String(8), default='USD')
    image_url: Mapped[str] = mapped_column(Text, default='')
    rating: Mapped[float] = mapped_column(Float, default=0)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default='1', index=True)
    catalog_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default='1')


class Event(Base):
    __tablename__ = 'events'
    __table_args__ = (Index('ix_events_visitor_at', 'visitor', 'at'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    visitor: Mapped[str] = mapped_column(String(36), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey('products.id'), index=True)
    event_type: Mapped[str] = mapped_column(String(20))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    request_id: Mapped[str] = mapped_column(String(64), default='')
    position: Mapped[int] = mapped_column(Integer, default=-1)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)


class Cart(Base):
    __tablename__ = 'cart'
    __table_args__ = (UniqueConstraint('visitor', 'product_id'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    visitor: Mapped[str] = mapped_column(String(36), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey('products.id'))
    quantity: Mapped[int] = mapped_column(Integer, default=1)


class CartOperation(Base):
    """Once-only cart increments; scoped idempotency key is already HMACed."""
    __tablename__ = 'cart_operations'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    visitor: Mapped[str] = mapped_column(String(36))
    product_id: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer)


class ProductVisitor(Base):
    """At most one positive interaction per visitor/product for popularity."""
    __tablename__ = 'product_visitors'
    __table_args__ = (UniqueConstraint('visitor', 'product_id'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visitor: Mapped[str] = mapped_column(String(36))
    product_id: Mapped[str] = mapped_column(String(128), index=True)


class ProductStat(Base):
    __tablename__ = 'product_stats'
    product_id: Mapped[str] = mapped_column(ForeignKey('products.id'), primary_key=True)
    unique_visitors: Mapped[int] = mapped_column(Integer, default=0)



class Outbox(Base):
    __tablename__ = 'outbox'
    __table_args__ = (Index('ix_outbox_claim', 'delivered', 'next_attempt_at', 'lease_until'),
                      Index('ix_outbox_delivered_at', 'delivered', 'delivered_at'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[str] = mapped_column(Text)
    catalog_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default='1')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivered: Mapped[bool] = mapped_column(default=False, index=True)
    # Nullable additions allow non-destructive upgrades of existing V1 databases.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SearchMaintenance(Base):
    """Single persisted rebuild lease; coordinates API sync and offline reindexer."""
    __tablename__ = 'search_maintenance'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EventInput(BaseModel):
    product_id: str = Field(min_length=1, max_length=128)
    event_type: Literal['impression', 'click', 'favorite', 'cart']
    request_id: str = Field(default='', max_length=64)
    position: int = Field(default=-1, ge=-1, le=100000)
    idempotency_key: str | None = Field(default=None, max_length=100)


class BatchInput(BaseModel):
    events: list[EventInput] = Field(min_length=1, max_length=50)


class CatalogAvailability(BaseModel):
    active: bool


class CartInput(BaseModel):
    product_id: str = Field(min_length=1, max_length=128)
    quantity: int = Field(default=1, ge=1, le=20)


def upgrade_existing_schema(conn):
    """Idempotently reconcile legacy V1..V1.4 schema during the Alembic baseline."""
    required = {
        'next_attempt_at': 'TIMESTAMP',
        'lease_owner': 'VARCHAR(36)',
        'lease_until': 'TIMESTAMP',
        'last_error': 'VARCHAR(512)',
        'created_at': 'TIMESTAMP',
        'delivered_at': 'TIMESTAMP',
    }
    # Existing V1 databases created before the composite index need it too.
    existing_indexes = {idx['name'] for idx in inspect(conn).get_indexes('events')}
    if 'ix_events_visitor_at' not in existing_indexes:
        conn.execute(text('CREATE INDEX ix_events_visitor_at ON events (visitor, at)'))
    columns = {col['name'] for col in inspect(conn).get_columns('products')}
    if 'catalog_revision' not in columns:
        conn.execute(text('ALTER TABLE products ADD COLUMN catalog_revision INTEGER NOT NULL DEFAULT 1'))
    if 'rating_count' not in columns:
        conn.execute(text('ALTER TABLE products ADD COLUMN rating_count INTEGER NOT NULL DEFAULT 0'))
    if 'is_active' not in columns:
        conn.execute(text('ALTER TABLE products ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1'))
    columns = {col['name'] for col in inspect(conn).get_columns('outbox')}
    if 'catalog_revision' not in columns:
        conn.execute(text('ALTER TABLE outbox ADD COLUMN catalog_revision INTEGER NOT NULL DEFAULT 1'))
    for name, datatype in required.items():
        if name not in columns:
            conn.execute(text(f'ALTER TABLE outbox ADD COLUMN {name} {datatype}'))
    # An index on the existing table must be added explicitly; create_all
    # only creates indexes automatically when the table is new.
    conn.execute(text('CREATE INDEX IF NOT EXISTS ix_outbox_created_at ON outbox (created_at)'))
    conn.execute(text('CREATE INDEX IF NOT EXISTS ix_outbox_claim ON outbox (delivered, next_attempt_at, lease_until)'))
    conn.execute(text('CREATE INDEX IF NOT EXISTS ix_outbox_delivered_at ON outbox (delivered, delivered_at)'))
    # create_all does not add indexes to pre-existing tables.
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(bind=conn, checkfirst=True)
    conn.execute(insert_on_conflict(SearchMaintenance).values(id=1).on_conflict_do_nothing(index_elements=['id']))
    if conn.dialect.name == 'sqlite':
        install_sqlite_fts(conn)


def initialize_schema():
    """Development/test convenience. Production must run Alembic externally."""
    with engine.begin() as conn:
        Base.metadata.create_all(bind=conn)
        upgrade_existing_schema(conn)


def check_schema_revision():
    """Reject stale/uncertified schemas instead of migrating from API workers."""
    with engine.connect() as conn:
        try:
            version = conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
        except Exception as exc:
            raise RuntimeError('Database not migrated; run `alembic upgrade head` first') from exc
        if version != '0004_v19_catalog_fencing':
            raise RuntimeError(f'Expected schema 0004_v19_catalog_fencing, found {version!r}; run Alembic')


def install_sqlite_fts(conn):
    """One-time FTS5 backfill + triggers, including on existing V1 product tables.

    FTS5 is optional in custom SQLite builds; the SQL LIKE fallback still works.
    """
    exists = conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='products_fts'")).first()
    try:
        conn.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(title, description, brand, content='products', content_rowid='rowid')"))
        conn.execute(text("""CREATE TRIGGER IF NOT EXISTS products_fts_ai AFTER INSERT ON products BEGIN
            INSERT INTO products_fts(rowid, title, description, brand) VALUES (new.rowid, new.title, new.description, new.brand);
        END"""))
        conn.execute(text("""CREATE TRIGGER IF NOT EXISTS products_fts_ad AFTER DELETE ON products BEGIN
            INSERT INTO products_fts(products_fts, rowid, title, description, brand) VALUES ('delete', old.rowid, old.title, old.description, old.brand);
        END"""))
        conn.execute(text("""CREATE TRIGGER IF NOT EXISTS products_fts_au AFTER UPDATE ON products BEGIN
            INSERT INTO products_fts(products_fts, rowid, title, description, brand) VALUES ('delete', old.rowid, old.title, old.description, old.brand);
            INSERT INTO products_fts(rowid, title, description, brand) VALUES (new.rowid, new.title, new.description, new.brand);
        END"""))
        if not exists:
            conn.execute(text("INSERT INTO products_fts(products_fts) VALUES ('rebuild')"))
    except Exception as exc:
        logger.warning('FTS5 unavailable, SQL LIKE fallback retained: %s', exc)


def insert_on_conflict(table):
    """Use the native SQLite/PG SQLAlchemy upsert API."""
    if DRIVER == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    return insert(table)


def update_popularity(db: Session, who: str, pid: str):
    first = db.execute(insert_on_conflict(ProductVisitor).values(visitor=who, product_id=pid)
                       .on_conflict_do_nothing(index_elements=['visitor', 'product_id']))
    if first.rowcount:
        db.execute(insert_on_conflict(ProductStat).values(product_id=pid, unique_visitors=1)
                   .on_conflict_do_update(index_elements=['product_id'],
                                          set_={'unique_visitors': ProductStat.unique_visitors + 1}))


def product_quality(p: Product) -> float:
    """Bayesian shrinkage; unreviewed items use a 4.0 prior."""
    count = max(0, p.rating_count or 0)
    return (count * (p.rating or 0) + 20 * 4.0) / (count + 20)



@asynccontextmanager
async def lifespan(application: FastAPI):
    if os.getenv('SCHEMA_MODE', 'auto') == 'external':
        check_schema_revision()
    else:
        initialize_schema()
    # One connection pool shared by all synchronous FastAPI workers. Endpoints
    # use normal `def` handlers so SQLAlchemy and HTTP network I/O run in
    # FastAPI's threadpool rather than blocking the event loop.
    application.state.circuits = CircuitBreaker()
    application.state.runtime_metrics = MetricsCounters()
    application.state.rate_limiter = TokenBuckets(
        capacity=float(os.getenv('RATE_LIMIT_BURST', '120')),
        refill_per_second=float(os.getenv('RATE_LIMIT_PER_MINUTE', '120')) / 60,
    )
    with httpx.Client(timeout=httpx.Timeout(2.0, connect=0.35),
                      limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
                      trust_env=False) as client:
        application.state.provider_http = client
        yield


SECRET = os.getenv('SESSION_SECRET')
if not SECRET or SECRET.startswith(('replace-', 'development-only')):
    if os.getenv('APP_ENV', 'development') == 'production':
        raise RuntimeError('Configure a strong SESSION_SECRET in production')
    SECRET = secrets.token_urlsafe(48)
    logger.warning('SESSION_SECRET absent: random ephemeral development secret; sessions reset on restart')
if os.getenv('APP_ENV', 'development') == 'production' and (len(os.getenv('ADMIN_KEY', '')) < 32 or os.getenv('ADMIN_KEY', '').startswith('replace-')):
    raise RuntimeError('Production ADMIN_KEY must be a non-placeholder secret of at least 32 characters')

app = FastAPI(title='Distributed Commerce RecSys API', version='0.3.0', lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET, same_site='lax', max_age=365 * 24 * 3600, https_only=os.getenv('COOKIE_SECURE', 'false').lower() == 'true')


@app.middleware('http')
async def origin_guard(request: Request, call_next):
    """Reject unexpected browser-originated writes; non-browser API clients have admin auth."""
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        origin = request.headers.get('origin')
        if origin:
            allowed = {url.strip().rstrip('/') for url in os.getenv('ALLOWED_ORIGINS', 'http://localhost:3000,http://127.0.0.1:3000').split(',') if url.strip()}
            if origin.rstrip('/') not in allowed:
                return JSONResponse({'detail': 'Origin not allowed'}, status_code=403)
    return await call_next(request)


@app.middleware('http')
async def request_diagnostics(request: Request, call_next):
    request_id = uuid.uuid4().hex
    start = time.perf_counter()
    response = await call_next(request)
    ms = round((time.perf_counter() - start) * 1000, 1)
    response.headers['X-Request-ID'] = request_id
    response.headers['Server-Timing'] = f'api;dur={ms}'
    # Do not log search terms, cookies or authorization headers.
    logger.info('request id=%s method=%s status=%s latency_ms=%s',
                request_id, request.method, response.status_code, ms)
    return response


def db_session():
    with SessionLocal() as db:
        yield db


def visitor(request: Request) -> str:
    if 'visitor' not in request.session:
        request.session['visitor'] = str(uuid.uuid4())
    return request.session['visitor']


def limit_write(request: Request, who: str, cost: int = 1):
    """Anonymous abuse throttle; process-local and not a substitute for auth."""
    if os.getenv('RATE_LIMIT_ENABLED', 'true').lower() == 'false':
        return
    limiter = request.app.state.rate_limiter
    # The visitor limit works even when all clients share the Next.js proxy IP.
    if not limiter.allow('visitor:' + who, cost):
        raise HTTPException(429, 'Too many write requests', headers={'Retry-After': '30'})
    # This higher IP budget bounds rapid identity cycling; X-Forwarded-For is
    # deliberately not trusted from an unauthenticated reverse proxy.
    ip = request.client.host if request.client else 'unknown'
    if not limiter.allow('ip:' + ip, cost / 8):
        raise HTTPException(429, 'Too many requests from this address', headers={'Retry-After': '30'})


def as_product(p: Product, detail: bool = False) -> dict:
    fields = ('id', 'source', 'title', 'category', 'brand', 'price', 'currency', 'image_url', 'rating')
    result = {field: getattr(p, field) for field in fields}
    if detail:
        result['description'] = p.description
        result['rating_count'] = p.rating_count
    return result


def search_document(product: Product) -> dict:
    """Canonical OpenSearch product shape; keep batch and full-build mappings aligned."""
    return {'id': product.id, **{name: getattr(product, name) for name in
            ('title', 'description', 'brand', 'category', 'price', 'rating', 'source')}}


def ensure_product(db: Session, pid: str) -> Product:
    result = db.get(Product, pid)
    if result is None or not result.is_active:
        raise HTTPException(404, 'Product not found')
    return result


def admin_key(key: str | None = Header(default=None, alias='X-Admin-Key')):
    expected = os.getenv('ADMIN_KEY', '')
    if not expected or not key or not secrets.compare_digest(key, expected):
        raise HTTPException(403, 'Admin authorization required')


def gorse_item_document(product: Product) -> dict:
    return {'ItemId': product.id, 'IsHidden': not product.is_active,
            'Categories': [product.category],
            'Labels': {'brand': [product.brand] if product.brand else [],
                       'category': [product.category]},
            'Timestamp': '2023-12-31T00:00:00Z' if product.source == 'amazon-2023' else '2026-01-01T00:00:00Z'}


def queue_catalog_updates(db: Session, product: Product, *, force: bool = False) -> None:
    """Transactionally persist the latest item + search state.

    One deterministic outbox row per product/kind is safe to retry and keeps
    large imports bounded. Remote calls are deliberately outside the transaction.
    """
    item = gorse_item_document(product)
    search = search_document(product) if product.is_active else {'id': product.id, '_op': 'delete'}
    changes = []
    for kind, namespace, payload in (('item', 'gorse-item-', item),
                                      ('search', 'search-item-', search)):
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, namespace + product.id))
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        row = db.get(Outbox, key)
        changes.append((key, kind, encoded, row))
    # A transaction-level revision covers both providers. A forced requeue is
    # used by reconciliation after remote state drift, not by normal imports.
    changed = force or any(row is None or row.payload != encoded for _, _, encoded, row in changes)
    if not changed:
        return
    if any(row is not None for _, _, _, row in changes):
        product.catalog_revision = (product.catalog_revision or 1) + 1
    revision = product.catalog_revision or 1
    for key, kind, encoded, row in changes:
        if row is None:
            db.add(Outbox(id=key, kind=kind, payload=encoded, catalog_revision=revision))
        else:
            row.payload = encoded
            row.catalog_revision = revision
            row.delivered = False
            row.delivered_at = None
            # IMPORTANT: never cancel an in-flight lease. That old worker must
            # complete (or expire) before the new version may be dispatched.
            row.next_attempt_at = None
            row.attempts = 0
            row.last_error = None


def acknowledge_catalog_delivery(db: Session, row: Outbox, owner: str, ok: bool,
                                 error: str, now: datetime) -> tuple[bool, bool]:
    """ACK only the exact revision sent; release superseded leases for replay."""
    values = {'lease_owner': None, 'lease_until': None}
    if ok:
        values.update(delivered=True, delivered_at=datetime.now(timezone.utc), last_error=None)
    else:
        count = (row.attempts or 0) + 1
        values.update(attempts=count, last_error=error,
                      next_attempt_at=now + timedelta(seconds=min(1800, 2 ** min(count, 10))))
    result = db.execute(update(Outbox).where(
        Outbox.id == row.id, Outbox.lease_owner == owner,
        Outbox.catalog_revision == row.catalog_revision).values(**values))
    if result.rowcount:
        return ok, not ok
    # The business transaction advanced the revision during the network call.
    # Do not mark the newer payload delivered or apply backoff to it.
    db.execute(update(Outbox).where(
        Outbox.id == row.id, Outbox.lease_owner == owner,
        Outbox.catalog_revision != row.catalog_revision).values(
            lease_owner=None, lease_until=None, next_attempt_at=None))
    return False, False


@app.patch('/api/admin/products/{pid}/availability', dependencies=[Depends(admin_key)])
def set_product_availability(pid: str, payload: CatalogAvailability, db: Session = Depends(db_session)):
    """Soft-delete/restore a catalog item, without destroying historical events."""
    product = db.get(Product, pid)
    if product is None:
        raise HTTPException(404, 'Product not found')
    changed = bool(product.is_active) != payload.active
    if changed:
        product.is_active = payload.active
        # Remove items from carts on withdrawal. Prior behavioral events stay.
        if not payload.active:
            db.query(Cart).filter(Cart.product_id == pid).delete(synchronize_session=False)
        queue_catalog_updates(db, product)
        db.commit()
    return {'id': pid, 'active': bool(product.is_active), 'changed': changed}


@app.get('/health/live')
def live():
    return {'status': 'alive'}


@app.get('/health/ready')
def ready(db: Session = Depends(db_session)):
    try:
        db.execute(select(func.count()).select_from(Product)).scalar_one()
    except Exception:
        logger.exception('Readiness failed')
        raise HTTPException(503, 'Database unavailable')
    return {'status': 'ready', 'database': DRIVER}


@app.get('/api/health')
def health(db: Session = Depends(db_session)):
    count = db.scalar(select(func.count()).select_from(Product))
    return {'database': DRIVER, 'products': count, 'search_configured': bool(CONFIG['services']['search']), 'gorse_configured': bool(CONFIG['services']['gorse'])}


@app.get('/api/products')
def products(category: str = '', limit: int = Query(24, ge=1, le=100), offset: int = Query(0, ge=0), db: Session = Depends(db_session)):
    stmt = select(Product).where(Product.is_active.is_(True))
    if category:
        stmt = stmt.where(Product.category == category)
    count = db.scalar(select(func.count()).select_from(Product).where(Product.is_active.is_(True), Product.category == category) if category else select(func.count()).select_from(Product).where(Product.is_active.is_(True)))
    rows = db.scalars(stmt.order_by(Product.id).offset(offset).limit(limit)).all()
    return {'items': [as_product(p) for p in rows], 'total': count, 'limit': limit, 'offset': offset}


@app.get('/api/categories')
def categories(db: Session = Depends(db_session)):
    return [name for name in db.scalars(select(Product.category).where(Product.is_active.is_(True)).distinct().order_by(Product.category)).all() if name]


@app.get('/api/products/{pid}')
def get_product(pid: str, db: Session = Depends(db_session)):
    return as_product(ensure_product(db, pid), detail=True)


@app.get('/api/search')
def search(request: Request, q: str = Query(..., min_length=1, max_length=160), category: str = '',
                 limit: int = Query(24, ge=1, le=100), offset: int = Query(0, ge=0, le=10000),
                 db: Session = Depends(db_session)):
    ids = []
    provider = 'sql'
    base_url = CONFIG['services']['search'].rstrip('/')
    if base_url and request.app.state.circuits.allow('search'):
        try:
            query = {'size': limit, 'from': offset, '_source': False,
                     'query': {'bool': {'must': [{'multi_match': {
                         'query': q, 'fields': ['title^4', 'brand^2', 'description']}}],
                         'filter': ([{'term': {'category': category}}] if category else [])}}}
            resp = request.app.state.provider_http.post(base_url + '/commerce-products/_search', json=query)
            resp.raise_for_status()
            ids = [hit['_id'] for hit in resp.json().get('hits', {}).get('hits', [])]
            provider = 'opensearch'
            request.app.state.circuits.success('search')
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            request.app.state.circuits.failure('search')
            logger.warning('Search provider unavailable, using SQL: %s', type(exc).__name__)
    if provider == 'opensearch':
        mapped = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids), Product.is_active.is_(True))).all()}
        items = [as_product(mapped[i]) for i in ids if i in mapped]
    else:
        # FTS5 indexes words on SQLite; punctuation-only requests still use literal LIKE.
        terms = re.findall(r'[^\W_]+', q, flags=re.UNICODE)
        fts_used = False
        if DRIVER == 'sqlite' and terms and len(terms) <= 8:
            try:
                match = ' AND '.join('"' + term.replace('"', '""') + '"' for term in terms)
                stmt = select(Product).where(Product.is_active.is_(True), Product.id.in_(
                    select(Product.id).where(text('products.rowid IN (SELECT rowid FROM products_fts WHERE products_fts MATCH :match)')).params(match=match)))
                if category:
                    stmt = stmt.where(Product.category == category)
                items = [as_product(p) for p in db.scalars(stmt.order_by(Product.rating.desc(), Product.id).offset(offset).limit(limit)).all()]
                fts_used = True
                provider = 'sqlite-fts5'
            except Exception as exc:
                logger.warning('FTS5 query unavailable, using LIKE: %s', type(exc).__name__)
        if not fts_used:
            escaped = q.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_')
            like = f'%{escaped}%'
            stmt = select(Product).where(Product.is_active.is_(True), or_(Product.title.ilike(like, escape='\\'), Product.description.ilike(like, escape='\\'), Product.brand.ilike(like, escape='\\')))
            if category:
                stmt = stmt.where(Product.category == category)
            items = [as_product(p) for p in db.scalars(stmt.order_by(Product.rating.desc(), Product.id).offset(offset).limit(limit)).all()]
    request.app.state.runtime_metrics.increment('search_' + provider.replace('-', '_'))
    return {'items': items, 'provider': provider, 'query': q, 'offset': offset, 'limit': limit}


WEIGHTS = {'impression': 0, 'click': 1, 'favorite': 3, 'cart': 5, 'purchase_demo': 8}


def scoped_idempotency(visitor_id: str, key: str) -> str:
    """Prevents two visitors using the same client key from conflicting."""
    return hmac.new(SECRET.encode(), f'{visitor_id}:{key}'.encode(), hashlib.sha256).hexdigest()


def check_duplicate(db: Session, who: str, key: str, payload: EventInput):
    found = db.scalar(select(Event).where(Event.idempotency_key == key))
    if not found:
        return False
    if found.visitor != who or found.product_id != payload.product_id or found.event_type != payload.event_type or found.request_id != payload.request_id or found.position != payload.position:
        raise HTTPException(409, 'Idempotency key reused with different event payload')
    return True


@app.post('/api/events', status_code=201)
def record_event(payload: EventInput, request: Request, response: Response, db: Session = Depends(db_session)):
    ensure_product(db, payload.product_id)
    who = visitor(request)
    limit_write(request, who)
    key = scoped_idempotency(who, payload.idempotency_key) if payload.idempotency_key else None
    if key and check_duplicate(db, who, key, payload):
        response.status_code = 200
        return {'duplicate': True}
    ev = Event(id=str(uuid.uuid4()), visitor=who, product_id=payload.product_id, event_type=payload.event_type, request_id=payload.request_id, position=payload.position, idempotency_key=key)
    db.add(ev)
    if WEIGHTS[payload.event_type]:
        update_popularity(db, who, payload.product_id)
    if payload.event_type != 'impression' or (payload.request_id and payload.position >= 0):
        feedback = {'FeedbackType': payload.event_type, 'UserId': who, 'ItemId': payload.product_id, 'Value': float(WEIGHTS[payload.event_type]), 'Timestamp': datetime.now(timezone.utc).isoformat()}
        db.add(Outbox(id=str(uuid.uuid4()), kind='feedback', payload=json.dumps(feedback)))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if key and check_duplicate(db, who, key, payload):
            response.status_code = 200
            return {'duplicate': True}
        raise
    return {'id': ev.id, 'recorded': True}


@app.post('/api/events/batch', status_code=201)
def record_impressions(batch: BatchInput, request: Request, response: Response, db: Session = Depends(db_session)):
    """Bounded transactional recommendation exposure logger; click events use /events."""
    if any(ev.event_type != 'impression' or not ev.request_id or ev.position < 0 for ev in batch.events):
        raise HTTPException(422, 'Batch requires attributed impressions')
    who = visitor(request)
    limit_write(request, who, len(batch.events))
    keys_seen = set()
    inserted = 0
    for ev in batch.events:
        ensure_product(db, ev.product_id)
        key_raw = ev.idempotency_key or f'impression:{ev.request_id}:{ev.position}:{ev.product_id}'
        key = scoped_idempotency(who, key_raw)
        if key in keys_seen:
            raise HTTPException(422, 'Duplicate keys inside batch')
        keys_seen.add(key)
        if check_duplicate(db, who, key, ev):
            continue
        db.add(Event(id=str(uuid.uuid4()), visitor=who, product_id=ev.product_id, event_type='impression',
                     request_id=ev.request_id, position=ev.position, idempotency_key=key))
        feedback = {'FeedbackType': 'impression', 'UserId': who, 'ItemId': ev.product_id,
                    'Value': 1, 'Timestamp': datetime.now(timezone.utc).isoformat()}
        db.add(Outbox(id=str(uuid.uuid4()), kind='feedback', payload=json.dumps(feedback)))
        inserted += 1
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # A concurrent identical batch may have won the race; verify all keys.
        if all(check_duplicate(db, who, scoped_idempotency(who, ev.idempotency_key or
                 f'impression:{ev.request_id}:{ev.position}:{ev.product_id}'), ev) for ev in batch.events):
            response.status_code = 200
            return {'recorded': 0, 'duplicates': len(batch.events)}
        raise
    if not inserted:
        response.status_code = 200
    return {'recorded': inserted, 'duplicates': len(batch.events) - inserted}


@app.get('/api/cart')
def get_cart(request: Request, db: Session = Depends(db_session)):
    who = visitor(request)
    cart = db.execute(select(Cart, Product).join(Product, Cart.product_id == Product.id).where(Cart.visitor == who, Product.is_active.is_(True))).all()
    return {'items': [{'product': as_product(p), 'quantity': c.quantity} for c, p in cart]}


@app.post('/api/cart', status_code=201)
def add_cart(payload: CartInput, request: Request, response: Response,
             idempotency_key: str | None = Header(default=None, alias='Idempotency-Key', max_length=100),
             db: Session = Depends(db_session)):
    ensure_product(db, payload.product_id)
    who = visitor(request)
    limit_write(request, who)
    key = scoped_idempotency(who, 'cart:' + idempotency_key) if idempotency_key else None

    def repeated():
        op = db.get(CartOperation, key) if key else None
        if op:
            if op.visitor != who or op.product_id != payload.product_id or op.quantity != payload.quantity:
                raise HTTPException(409, 'Idempotency key reused with different cart payload')
            response.status_code = 200
            return {'ok': True, 'duplicate': True}
        return None

    duplicate = repeated()
    if duplicate:
        return duplicate
    try:
        if key:
            db.add(CartOperation(key=key, visitor=who, product_id=payload.product_id, quantity=payload.quantity))
            db.flush()  # Unique key must be reserved in the same transaction as the cart update.
        stmt = insert_on_conflict(Cart).values(visitor=who, product_id=payload.product_id, quantity=payload.quantity)
        db.execute(stmt.on_conflict_do_update(index_elements=['visitor', 'product_id'], set_={
            'quantity': case((Cart.quantity + payload.quantity > 99, 99),
                             else_=Cart.quantity + payload.quantity)}))
        update_popularity(db, who, payload.product_id)
        db.add(Event(id=str(uuid.uuid4()), visitor=who, product_id=payload.product_id, event_type='cart'))
        feedback = {'FeedbackType': 'cart', 'UserId': who, 'ItemId': payload.product_id, 'Value': 5,
                    'Timestamp': datetime.now(timezone.utc).isoformat()}
        db.add(Outbox(id=str(uuid.uuid4()), kind='feedback', payload=json.dumps(feedback)))
        db.commit()
    except IntegrityError:
        db.rollback()
        duplicate = repeated()
        if duplicate:
            return duplicate
        raise
    return {'ok': True, 'duplicate': False}


@app.delete('/api/cart/{pid}')
def remove_cart(pid: str, request: Request, db: Session = Depends(db_session)):
    who = visitor(request)
    limit_write(request, who)
    row = db.scalar(select(Cart).where(Cart.visitor == who, Cart.product_id == pid))
    if row:
        db.delete(row)
        db.commit()
    return {'ok': True}


@app.get('/api/recommendations/home')
def recommend(request: Request, limit: int = Query(12, ge=1, le=50), db: Session = Depends(db_session)):
    who = visitor(request)
    ids: list[str] = []
    base = CONFIG['services']['gorse'].rstrip('/')
    if base and request.app.state.circuits.allow('gorse'):
        try:
            headers = {'X-API-Key': os.environ['GORSE_API_KEY']} if os.getenv('GORSE_API_KEY') else {}
            resp = request.app.state.provider_http.get(f'{base}/api/recommend/{who}', params={'n': limit * 4}, headers=headers)
            resp.raise_for_status()
            ids = [r if isinstance(r, str) else r['Id'] for r in resp.json()]
            request.app.state.circuits.success('gorse')
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            request.app.state.circuits.failure('gorse')
            logger.warning('Gorse unavailable, using fallback: %s', type(exc).__name__)

    # Keep interest history separate from impression/dedup history.
    history = db.execute(select(Event.product_id, Event.event_type)
                         .where(Event.visitor == who, Event.event_type.in_(['click', 'favorite', 'cart', 'purchase_demo']))
                         .order_by(Event.at.desc(), Event.id.desc()).limit(120)).all()
    used_ids = {pid for pid, _ in history}
    category_map = dict(db.execute(select(Product.id, Product.category).where(Product.id.in_(used_ids), Product.is_active.is_(True))).all()) if used_ids else {}
    category_scores: dict[str, float] = defaultdict(float)
    for index, (pid, kind) in enumerate(history):
        if pid in category_map:
            category_scores[category_map[pid]] += WEIGHTS[kind] * (0.97 ** index)
    max_interest = max(category_scores.values(), default=0.0)
    # A click is a signal, not a permanent exclusion. Purchased/carted items are excluded.
    strongly_used = {pid for pid, kind in history if kind in ('cart', 'purchase_demo')}
    candidates: dict[str, tuple[Product, float, str]] = {}

    def register(rows, source, base_score):
        for pos, product in enumerate(rows):
            if product.id in strongly_used:
                continue
            score = base_score - pos * (8.0 / max(limit * 4, 1))
            old = candidates.get(product.id)
            if old is None or score > old[1]:
                candidates[product.id] = product, score, source

    if ids:
        products_by_id = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids), Product.is_active.is_(True))).all()}
        register((products_by_id[pid] for pid in ids if pid in products_by_id), 'gorse', 30)

    # Each category supplies an independent bounded candidate pool, even if Gorse fills the slate.
    for cat, score in sorted(category_scores.items(), key=lambda value: -value[1])[:4]:
        stmt = select(Product).where(Product.category == cat, Product.is_active.is_(True))
        if strongly_used:
            stmt = stmt.where(Product.id.not_in(strongly_used))
        register(db.scalars(stmt.order_by(Product.rating.desc(), Product.id).limit(max(12, limit * 2))),
                 'interest', 18 + 12 * score / max_interest)

    stats = select(Product).join(ProductStat, Product.id == ProductStat.product_id).where(Product.is_active.is_(True)).order_by(
        ProductStat.unique_visitors.desc(), Product.rating.desc(), Product.id).limit(limit * 4)
    register(db.scalars(stats), 'popular', 12)
    register(db.scalars(select(Product).where(Product.is_active.is_(True)).order_by(Product.rating.desc(), Product.id).limit(limit * 5)), 'rating', 7)

    def score(item):
        p, base_score, _ = item
        boost = (35.0 * category_scores.get(p.category, 0) / max_interest) if max_interest else 0.0
        return base_score + boost + 1.5 * product_quality(p) - (5 if p.id in used_ids else 0)

    ranked = sorted(candidates.values(), key=lambda x: (-score(x), x[0].id))
    categories = {p.category for p, _, _ in ranked}
    cap = max(1, math.ceil(limit * 0.5)) if len(categories) > 1 else limit
    ordered: list[tuple[Product, str]] = []
    counts: dict[str, int] = defaultdict(int)
    used: set[str] = set()
    for pass_number in (0, 1):
        for p, _, source in ranked:
            if len(ordered) >= limit:
                break
            if p.id in used or (pass_number == 0 and counts[p.category] >= cap):
                continue
            ordered.append((p, source))
            used.add(p.id)
            counts[p.category] += 1
    sources = {source for _, source in ordered}
    strategy = 'gorse' if sources == {'gorse'} else ('gorse+rerank' if 'gorse' in sources else 'local-interest-popularity')
    request_id = str(uuid.uuid4())
    request.app.state.runtime_metrics.increment('recommend_gorse' if 'gorse' in sources else 'recommend_local')
    return {'items': [as_product(p) for p, _ in ordered], 'strategy': strategy, 'request_id': request_id,
            'placements': [{'product_id': p.id, 'position': i, 'source': source}
                           for i, (p, source) in enumerate(ordered)]}


@app.get('/api/recommendations/similar/{pid}')
def similar(pid: str, limit: int = Query(8, ge=1, le=30), db: Session = Depends(db_session)):
    p = ensure_product(db, pid)
    title_terms = set(re.findall(r'\w+', p.title.lower()))
    matches = db.scalars(select(Product).where(Product.category == p.category, Product.id != p.id, Product.is_active.is_(True))
                         .order_by(Product.rating.desc(), Product.id).limit(max(150, limit * 12))).all()

    def similarity(other: Product):
        overlap = len(title_terms & set(re.findall(r'\w+', other.title.lower())))
        brand = 2 if p.brand and other.brand and p.brand.casefold() == other.brand.casefold() else 0
        price = 1 / (1 + abs((p.price or 0) - (other.price or 0)) / max(p.price or 1, 1))
        return (overlap * 3 + brand + price + .1 * product_quality(other), other.id)

    matches.sort(key=lambda other: (-similarity(other)[0], other.id))
    return {'items': [as_product(x) for x in matches[:limit]], 'strategy': 'content-similarity'}


@app.get('/api/admin/metrics', dependencies=[Depends(admin_key)])
def metrics(db: Session = Depends(db_session)):
    oldest = db.scalar(select(func.min(Outbox.created_at)).where(Outbox.delivered.is_(False)))
    # SQLite's driver returns naive UTC timestamps even for timezone-aware fields.
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    age_seconds = max(0, int((datetime.now(timezone.utc) - oldest).total_seconds())) if oldest else None
    gate = db.get(SearchMaintenance, 1)
    gate_until = gate.lease_until if gate else None
    if gate_until is not None and gate_until.tzinfo is None:
        gate_until = gate_until.replace(tzinfo=timezone.utc)
    gate_remaining = max(0, int((gate_until - datetime.now(timezone.utc)).total_seconds())) if gate_until else 0
    return {'products': db.scalar(select(func.count()).select_from(Product)),
            'active_products': db.scalar(select(func.count()).select_from(Product).where(Product.is_active.is_(True))),
            'withdrawn_products': db.scalar(select(func.count()).select_from(Product).where(Product.is_active.is_(False))),
            'catalog_latest_revision': db.scalar(select(func.max(Product.catalog_revision))) or 0,
            'catalog_leased_tasks': db.scalar(select(func.count()).select_from(Outbox).where(
                Outbox.kind.in_(['item', 'search']), Outbox.delivered.is_(False),
                Outbox.lease_owner.is_not(None), Outbox.lease_until > datetime.now(timezone.utc))),
            'search_rebuild_active': bool(gate and gate.owner and gate_remaining > 0),
            'search_rebuild_lease_remaining_seconds': gate_remaining,
            'search_pending': db.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind == 'search', Outbox.delivered.is_(False))),
            'gorse_pending': db.scalar(select(func.count()).select_from(Outbox).where(Outbox.kind.in_(['item', 'feedback']), Outbox.delivered.is_(False))),
            'events': db.scalar(select(func.count()).select_from(Event)),
            'pending_outbox': db.scalar(select(func.count()).select_from(Outbox).where(Outbox.delivered.is_(False))),
            'oldest_pending_seconds': age_seconds,
            'database': DRIVER, 'circuits': app.state.circuits.status(),
            'requests': app.state.runtime_metrics.snapshot()}


@app.post('/api/admin/catalog/reconcile', dependencies=[Depends(admin_key)])
def reconcile_catalog(request: Request, after: str = Query('', max_length=128),
                      limit: int = Query(10, ge=1, le=25), repair: bool = False,
                      db: Session = Depends(db_session)):
    """Bounded remote read-back: detect and optionally requeue inconsistent items.

    Gorse does not expose conditional versions, so this is anti-entropy repair,
    not a substitute for real remote CAS. Remote outages never trigger repairs.
    """
    gorse_url = CONFIG['services']['gorse'].rstrip('/')
    search_url = CONFIG['services']['search'].rstrip('/')
    if not gorse_url and not search_url:
        raise HTTPException(503, 'Configure at least one catalog provider')
    products = db.scalars(select(Product).where(Product.id > after)
                          .order_by(Product.id).limit(limit)).all()
    headers = {'X-API-Key': os.environ['GORSE_API_KEY']} if os.getenv('GORSE_API_KEY') else {}
    drift = []
    errors = []
    fixed = 0
    for p in products:
        inconsistent = []
        encoded_id = quote(p.id, safe='')
        if gorse_url:
            try:
                result = request.app.state.provider_http.get(
                    f'{gorse_url}/api/item/{encoded_id}', headers=headers, timeout=3)
                if result.status_code == 404:
                    if p.is_active:
                        inconsistent.append('gorse')
                else:
                    result.raise_for_status()
                    remote = result.json()
                    expected = gorse_item_document(p)
                    if not isinstance(remote, dict) or any(remote.get(k) != expected[k] for k in
                            ('ItemId', 'IsHidden', 'Categories', 'Labels')):
                        inconsistent.append('gorse')
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                errors.append({'product_id': p.id, 'provider': 'gorse', 'type': type(exc).__name__})
        if search_url:
            try:
                result = request.app.state.provider_http.get(
                    f'{search_url}/commerce-products/_doc/{encoded_id}', timeout=3)
                if result.status_code == 404:
                    if p.is_active:
                        inconsistent.append('search')
                else:
                    result.raise_for_status()
                    remote = result.json()
                    expected = search_document(p)
                    document = remote.get('_source', {}) if isinstance(remote, dict) else {}
                    # OpenSearch _source excludes the ID; it is encoded as _id.
                    if ((not p.is_active) or (not remote.get('found', True)) or
                        remote.get('_id') != p.id or not isinstance(document, dict) or
                        any(document.get(k) != v for k, v in expected.items() if k != 'id')):
                        inconsistent.append('search')
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                errors.append({'product_id': p.id, 'provider': 'search', 'type': type(exc).__name__})
        if inconsistent:
            for provider in inconsistent:
                request.app.state.runtime_metrics.increment('catalog_drift_' + provider)
            drift.append({'product_id': p.id, 'providers': inconsistent})
            if repair:
                queue_catalog_updates(db, p, force=True)
                fixed += 1
                request.app.state.runtime_metrics.increment('catalog_repaired')
    if fixed:
        db.commit()
    return {'checked': len(products), 'drift': drift, 'errors': errors,
            'requeued': fixed, 'next_after': products[-1].id if products else None}


@app.post('/api/admin/sync', dependencies=[Depends(admin_key)])
def sync(request: Request, batch: int = Query(200, ge=1, le=1000), db: Session = Depends(db_session)):
    url = CONFIG['services']['gorse'].rstrip('/')
    if not url:
        raise HTTPException(503, 'GORSE_URL not configured')
    now = datetime.now(timezone.utc)
    owner = str(uuid.uuid4())
    available = ((Outbox.lease_until.is_(None)) | (Outbox.lease_until < now))
    due = ((Outbox.next_attempt_at.is_(None)) | (Outbox.next_attempt_at <= now))
    pending = select(Outbox.id).where(Outbox.kind.in_(['item', 'feedback']), Outbox.delivered.is_(False), available, due).order_by(Outbox.created_at, Outbox.id).limit(batch)
    # Atomic compare-and-swap lease works with SQLite >=3.35 and PostgreSQL.
    claimed = db.execute(update(Outbox)
        .where(Outbox.id.in_(pending), Outbox.delivered.is_(False), available, due)
        .values(lease_owner=owner, lease_until=now + timedelta(minutes=5))
        .returning(Outbox.id)).scalars().all()
    db.commit()
    rows = db.scalars(select(Outbox).where(Outbox.id.in_(claimed))).all() if claimed else []
    # Freeze exactly the version and payload sent, independently of later updates.
    rows = [Outbox(id=r.id, kind=r.kind, payload=r.payload, catalog_revision=r.catalog_revision, attempts=r.attempts) for r in rows]
    sent = 0
    failures = 0
    headers = {'X-API-Key': os.environ['GORSE_API_KEY']} if os.getenv('GORSE_API_KEY') else {}
    results = []
    # Items first so fresh catalog entries exist before associated feedback arrives.
    groups: dict[str, list[Outbox]] = defaultdict(list)
    for row in rows:
        groups[row.kind].append(row)
    client = request.app.state.provider_http
    for kind in ('item', 'feedback'):
        kind_rows = groups.pop(kind, [])
        # Bound request size independently of the claim limit.
        for start in range(0, len(kind_rows), 100):
            chunk = kind_rows[start:start + 100]
            try:
                payloads = [json.loads(row.payload) for row in chunk]
                if kind == 'feedback':
                    result = client.put(url + '/api/feedback', json=payloads, headers=headers, timeout=10)
                else:
                    result = client.post(url + '/api/items', json=payloads, headers=headers, timeout=10)
                result.raise_for_status()
                results.extend((row.id, True, '') for row in chunk)
            except (httpx.HTTPError, ValueError) as exc:
                results.extend((row.id, False, str(exc)[:512]) for row in chunk)
    for other_rows in groups.values():
        results.extend((row.id, False, 'Unsupported outbox kind') for row in other_rows)
    by_id = {row.id: row for row in rows}
    for row_id, ok, error in results:
        if by_id[row_id].kind == 'item':
            delivered, failed = acknowledge_catalog_delivery(db, by_id[row_id], owner, ok, error, now)
            sent += int(delivered)
            failures += int(failed)
            continue
        row = by_id[row_id]
        values = {'lease_owner': None, 'lease_until': None}
        if ok:
            values.update(delivered=True, delivered_at=datetime.now(timezone.utc), last_error=None)
        else:
            count = (row.attempts or 0) + 1
            values.update(attempts=count, last_error=error,
                          next_attempt_at=now + timedelta(seconds=min(1800, 2 ** min(count, 10))))
        ack = db.execute(update(Outbox).where(Outbox.id == row_id,
                             Outbox.lease_owner == owner).values(**values))
        if ack.rowcount:
            sent += int(ok)
            failures += int(not ok)
    db.commit()
    remaining = db.scalar(select(func.count()).select_from(Outbox).where(Outbox.delivered.is_(False), Outbox.kind.in_(['item', 'feedback'])))
    return {'claimed': len(claimed), 'delivered': sent, 'failed': failures, 'remaining': remaining}


@app.post('/api/admin/outbox/prune', dependencies=[Depends(admin_key)])
def prune_outbox(days: int = Query(30, ge=1, le=3650),
                 limit: int = Query(1000, ge=1, le=10000), db: Session = Depends(db_session)):
    """Delete only acknowledged rows older than the retention threshold."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    old_ids = select(Outbox.id).where(Outbox.delivered.is_(True), Outbox.delivered_at < cutoff).limit(limit)
    deleted = db.query(Outbox).filter(Outbox.id.in_(old_ids)).delete(synchronize_session=False)
    db.commit()
    return {'deleted': deleted, 'retention_days': days}


@app.post('/api/admin/search/sync', dependencies=[Depends(admin_key)])
def sync_search(request: Request, batch: int = Query(200, ge=1, le=1000),
                db: Session = Depends(db_session)):
    """Deliver durable catalog mutations to the current OpenSearch write alias.

    Concurrent import and full alias promotion need coordinated maintenance; see
    docs/ITERATION_V1_5.md. Unsuccessful and partial bulk responses remain queued.
    """
    base = CONFIG['services']['search'].rstrip('/')
    if not base:
        raise HTTPException(503, 'OPENSEARCH_URL not configured')
    now = datetime.now(timezone.utc)
    owner = str(uuid.uuid4())
    # A SQL write-lock gates claim + commit atomically against reindex start.
    # Do not claim while the indexer is building a new alias generation.
    if not sync_gate_open(db, SearchMaintenance):
        db.rollback()
        raise HTTPException(423, 'Search rebuild in progress; sync will retry')
    available = or_(Outbox.lease_until.is_(None), Outbox.lease_until < now)
    due = or_(Outbox.next_attempt_at.is_(None), Outbox.next_attempt_at <= now)
    pending = (select(Outbox.id).where(Outbox.kind == 'search', Outbox.delivered.is_(False),
                                     available, due).order_by(Outbox.created_at, Outbox.id).limit(batch))
    claimed = db.execute(update(Outbox).where(Outbox.id.in_(pending), Outbox.kind == 'search',
                        Outbox.delivered.is_(False), available, due)
                        .values(lease_owner=owner, lease_until=now + timedelta(minutes=5))
                        .returning(Outbox.id)).scalars().all()
    db.commit()
    rows = db.scalars(select(Outbox).where(Outbox.id.in_(claimed)).order_by(Outbox.created_at, Outbox.id)).all() if claimed else []
    rows = [Outbox(id=r.id, kind=r.kind, payload=r.payload, catalog_revision=r.catalog_revision, attempts=r.attempts) for r in rows]
    results: list[tuple[str, bool, str]] = []
    for start in range(0, len(rows), 100):
        chunk = rows[start:start + 100]
        try:
            docs = [json.loads(row.payload) for row in chunk]
            ndjson = []
            for row, doc in zip(chunk, docs):
                identifier = doc.get('id')
                if not identifier:
                    raise ValueError('Missing search document id')
                operation = doc.get('_op', 'index')
                if operation not in ('index', 'delete'):
                    raise ValueError('Unsupported search outbox operation')
                ndjson.append(json.dumps({operation: {'_index': 'commerce-products', '_id': identifier,
                                      'version': row.catalog_revision, 'version_type': 'external_gte'}}))
                if operation == 'index':
                    ndjson.append(json.dumps({k: v for k, v in doc.items() if k not in ('id', '_op')}, ensure_ascii=False))
            resp = request.app.state.provider_http.post(
                base + '/_bulk', content='\n'.join(ndjson) + '\n',
                headers={'Content-Type': 'application/x-ndjson'}, timeout=20)
            resp.raise_for_status()
            result = resp.json()
            if not isinstance(result, dict):
                raise ValueError('Malformed OpenSearch bulk response')
            items = result.get('items')
            if not isinstance(items, list) or len(items) != len(chunk):
                raise ValueError('Malformed OpenSearch bulk response')
            for row, doc, item in zip(chunk, docs, items):
                operation = doc.get('_op', 'index')
                status = item.get(operation, {})
                code = status.get('status', 0) if isinstance(status, dict) else 0
                success = isinstance(code, int) and (
                    200 <= code < 300 or (operation == 'delete' and code == 404 and status.get('result') == 'not_found')
                ) and not status.get('error')
                results.append((row.id, success, '' if success else str(status.get('error', status))[:512]))
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            results.extend((row.id, False, str(exc)[:512]) for row in chunk)
    by_id = {row.id: row for row in rows}
    sent = failed = 0
    for row_id, ok, error in results:
        delivered, did_fail = acknowledge_catalog_delivery(db, by_id[row_id], owner, ok, error, now)
        sent += int(delivered)
        failed += int(did_fail)
    db.commit()
    remaining = db.scalar(select(func.count()).select_from(Outbox).where(
        Outbox.kind == 'search', Outbox.delivered.is_(False)))
    return {'claimed': len(claimed), 'delivered': sent, 'failed': failed, 'remaining': remaining}
