"""Commerce API: SQLite-first, PostgreSQL-ready, with search/recs degradation."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import httpx
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Index, create_engine, event as sqla_event, func, inspect, or_, select, text, update
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
        cur.close()
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = 'products'
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


class Outbox(Base):
    __tablename__ = 'outbox'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivered: Mapped[bool] = mapped_column(default=False, index=True)
    # Nullable additions allow non-destructive upgrades of existing V1 databases.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(512))


class EventInput(BaseModel):
    product_id: str = Field(min_length=1, max_length=128)
    event_type: Literal['impression', 'click', 'favorite', 'cart', 'purchase_demo']
    request_id: str = Field(default='', max_length=64)
    position: int = Field(default=-1, ge=-1, le=100000)
    idempotency_key: str | None = Field(default=None, max_length=100)


class CartInput(BaseModel):
    product_id: str = Field(min_length=1, max_length=128)
    quantity: int = Field(default=1, ge=1, le=20)


def initialize_schema():
    """Backward-compatible V1 additive upgrade. Use Alembic for future changes."""
    Base.metadata.create_all(engine)
    required = {
        'next_attempt_at': 'TIMESTAMP',
        'lease_owner': 'VARCHAR(36)',
        'lease_until': 'TIMESTAMP',
        'last_error': 'VARCHAR(512)',
    }
    with engine.begin() as conn:
        # Existing V1 databases created before the composite index need it too.
        existing_indexes = {idx['name'] for idx in inspect(conn).get_indexes('events')}
        if 'ix_events_visitor_at' not in existing_indexes:
            conn.execute(text('CREATE INDEX ix_events_visitor_at ON events (visitor, at)'))
        columns = {col['name'] for col in inspect(conn).get_columns('outbox')}
        for name, datatype in required.items():
            if name not in columns:
                conn.execute(text(f'ALTER TABLE outbox ADD COLUMN {name} {datatype}'))


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_schema()
    yield


SECRET = os.getenv('SESSION_SECRET')
if not SECRET or SECRET.startswith(('replace-', 'development-only')):
    if os.getenv('APP_ENV', 'development') == 'production':
        raise RuntimeError('Configure a strong SESSION_SECRET in production')
    SECRET = secrets.token_urlsafe(48)
    logger.warning('SESSION_SECRET absent: random ephemeral development secret; sessions reset on restart')

app = FastAPI(title='Distributed Commerce RecSys API', version='0.2.0', lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET, same_site='lax', https_only=os.getenv('COOKIE_SECURE', 'false').lower() == 'true')


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


def db_session():
    with SessionLocal() as db:
        yield db


def visitor(request: Request) -> str:
    if 'visitor' not in request.session:
        request.session['visitor'] = str(uuid.uuid4())
    return request.session['visitor']


def as_product(p: Product) -> dict:
    return {field: getattr(p, field) for field in ('id', 'source', 'title', 'description', 'category', 'brand', 'price', 'currency', 'image_url', 'rating')}


def ensure_product(db: Session, pid: str) -> Product:
    result = db.get(Product, pid)
    if result is None:
        raise HTTPException(404, 'Product not found')
    return result


def admin_key(key: str | None = Header(default=None, alias='X-Admin-Key')):
    expected = os.getenv('ADMIN_KEY', '')
    if not expected or not key or not secrets.compare_digest(key, expected):
        raise HTTPException(403, 'Admin authorization required')


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
    stmt = select(Product)
    if category:
        stmt = stmt.where(Product.category == category)
    count = db.scalar(select(func.count()).select_from(Product).where(Product.category == category) if category else select(func.count()).select_from(Product))
    rows = db.scalars(stmt.order_by(Product.id).offset(offset).limit(limit)).all()
    return {'items': [as_product(p) for p in rows], 'total': count, 'limit': limit, 'offset': offset}


@app.get('/api/categories')
def categories(db: Session = Depends(db_session)):
    return [name for name in db.scalars(select(Product.category).distinct().order_by(Product.category)).all() if name]


@app.get('/api/products/{pid}')
def get_product(pid: str, db: Session = Depends(db_session)):
    return as_product(ensure_product(db, pid))


@app.get('/api/search')
async def search(q: str = Query(..., min_length=1, max_length=160), category: str = '', limit: int = Query(24, ge=1, le=100), db: Session = Depends(db_session)):
    ids = []
    provider = 'sql'
    base_url = CONFIG['services']['search'].rstrip('/')
    if base_url:
        try:
            query = {'size': limit, 'query': {'bool': {'must': [{'multi_match': {'query': q, 'fields': ['title^4', 'brand^2', 'description', 'category^2']}}], 'filter': ([{'term': {'category': category}}] if category else [])}}}
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.post(base_url + '/commerce-products/_search', json=query)
                resp.raise_for_status()
                ids = [hit['_id'] for hit in resp.json().get('hits', {}).get('hits', [])]
                provider = 'opensearch'
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning('Search provider unavailable, using SQL: %s', type(exc).__name__)
    if provider == 'opensearch':
        mapped = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids))).all()}
        items = [as_product(mapped[i]) for i in ids if i in mapped]
    else:
        escaped = q.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_')
        like = f'%{escaped}%'
        stmt = select(Product).where(or_(Product.title.ilike(like, escape='\\'), Product.description.ilike(like, escape='\\'), Product.brand.ilike(like, escape='\\')))
        if category:
            stmt = stmt.where(Product.category == category)
        items = [as_product(p) for p in db.scalars(stmt.order_by(Product.rating.desc(), Product.id).limit(limit)).all()]
    return {'items': items, 'provider': provider, 'query': q}


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
def record_event(payload: EventInput, request: Request, db: Session = Depends(db_session)):
    ensure_product(db, payload.product_id)
    who = visitor(request)
    key = scoped_idempotency(who, payload.idempotency_key) if payload.idempotency_key else None
    if key and check_duplicate(db, who, key, payload):
        return {'duplicate': True}
    ev = Event(id=str(uuid.uuid4()), visitor=who, product_id=payload.product_id, event_type=payload.event_type, request_id=payload.request_id, position=payload.position, idempotency_key=key)
    db.add(ev)
    if WEIGHTS[payload.event_type]:
        feedback = {'FeedbackType': payload.event_type, 'UserId': who, 'ItemId': payload.product_id, 'Value': float(WEIGHTS[payload.event_type]), 'Timestamp': datetime.now(timezone.utc).isoformat()}
        db.add(Outbox(id=str(uuid.uuid4()), kind='feedback', payload=json.dumps(feedback)))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if key and check_duplicate(db, who, key, payload):
            return {'duplicate': True}
        raise
    return {'id': ev.id, 'recorded': True}


@app.get('/api/cart')
def get_cart(request: Request, db: Session = Depends(db_session)):
    who = visitor(request)
    cart = db.execute(select(Cart, Product).join(Product, Cart.product_id == Product.id).where(Cart.visitor == who)).all()
    return {'items': [{'product': as_product(p), 'quantity': c.quantity} for c, p in cart]}


@app.post('/api/cart', status_code=201)
def add_cart(payload: CartInput, request: Request, db: Session = Depends(db_session)):
    ensure_product(db, payload.product_id)
    who = visitor(request)
    row = db.scalar(select(Cart).where(Cart.visitor == who, Cart.product_id == payload.product_id))
    if row:
        row.quantity = min(99, row.quantity + payload.quantity)
    else:
        db.add(Cart(visitor=who, product_id=payload.product_id, quantity=payload.quantity))
    feedback = {'FeedbackType': 'cart', 'UserId': who, 'ItemId': payload.product_id, 'Value': 5, 'Timestamp': datetime.now(timezone.utc).isoformat()}
    db.add(Event(id=str(uuid.uuid4()), visitor=who, product_id=payload.product_id, event_type='cart'))
    db.add(Outbox(id=str(uuid.uuid4()), kind='feedback', payload=json.dumps(feedback)))
    db.commit()
    return {'ok': True}


@app.delete('/api/cart/{pid}')
def remove_cart(pid: str, request: Request, db: Session = Depends(db_session)):
    who = visitor(request)
    row = db.scalar(select(Cart).where(Cart.visitor == who, Cart.product_id == pid))
    if row:
        db.delete(row)
        db.commit()
    return {'ok': True}


@app.get('/api/recommendations/home')
async def recommend(request: Request, limit: int = Query(12, ge=1, le=50), db: Session = Depends(db_session)):
    who = visitor(request)
    ids: list[str] = []
    strategy = 'local-interest-popularity'
    base = CONFIG['services']['gorse'].rstrip('/')
    if base:
        try:
            headers = {'X-API-Key': os.environ['GORSE_API_KEY']} if os.getenv('GORSE_API_KEY') else {}
            async with httpx.AsyncClient(timeout=2.0, headers=headers) as client:
                resp = await client.get(f'{base}/api/recommend/{who}', params={'n': limit * 2})
                resp.raise_for_status()
                ids = [r if isinstance(r, str) else r['Id'] for r in resp.json()]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning('Gorse unavailable, using fallback: %s', type(exc).__name__)

    history = db.execute(select(Event.product_id, Event.event_type).where(Event.visitor == who).order_by(Event.at.desc(), Event.id.desc()).limit(120)).all()
    seen = {pid for pid, _ in history}
    category_scores: dict[str, float] = {}
    if seen:
        category_map = dict(db.execute(select(Product.id, Product.category).where(Product.id.in_(seen))).all())
        for pid, etype in history:
            if pid in category_map:
                cat = category_map[pid]
                category_scores[cat] = category_scores.get(cat, 0) + WEIGHTS.get(etype, 0)

    ordered: list[Product] = []
    added: set[str] = set(seen)

    def add_candidates(candidates):
        for product in candidates:
            if product.id not in added and len(ordered) < limit:
                ordered.append(product)
                added.add(product.id)

    if ids:
        gorse_rows = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids))).all()}
        add_candidates(gorse_rows[pid] for pid in ids if pid in gorse_rows)
        if ordered:
            strategy = 'gorse+fallback'

    # Bounded DB queries: never deserialize the whole catalog to rank in Python.
    for cat, _score in sorted(category_scores.items(), key=lambda item: -item[1])[:3]:
        if len(ordered) >= limit:
            break
        stmt = select(Product).where(Product.category == cat)
        if added:
            stmt = stmt.where(Product.id.not_in(added))
        add_candidates(db.scalars(stmt.order_by(Product.rating.desc(), Product.id).limit(limit)).all())

    if len(ordered) < limit:
        popularity = (select(Event.product_id.label('pid'), func.count(Event.id).label('score'))
                      .where(Event.event_type.in_(['click', 'favorite', 'cart', 'purchase_demo']))
                      .group_by(Event.product_id).order_by(func.count(Event.id).desc()).limit(limit * 3)).subquery()
        stmt = select(Product).join(popularity, Product.id == popularity.c.pid).order_by(popularity.c.score.desc(), Product.id)
        if added:
            stmt = stmt.where(Product.id.not_in(added))
        add_candidates(db.scalars(stmt.limit(limit)).all())
    if len(ordered) < limit:
        stmt = select(Product).order_by(Product.rating.desc(), Product.id)
        if added:
            stmt = stmt.where(Product.id.not_in(added))
        add_candidates(db.scalars(stmt.limit(limit)).all())
    if strategy == 'gorse+fallback' and len(ordered) == min(limit, len(ids)):
        strategy = 'gorse'
    return {'items': [as_product(p) for p in ordered], 'strategy': strategy}


@app.get('/api/recommendations/similar/{pid}')
def similar(pid: str, limit: int = Query(8, ge=1, le=30), db: Session = Depends(db_session)):
    p = ensure_product(db, pid)
    matches = db.scalars(select(Product).where(Product.category == p.category, Product.id != p.id).order_by(Product.rating.desc(), Product.id).limit(limit)).all()
    return {'items': [as_product(x) for x in matches], 'strategy': 'category'}


@app.get('/api/admin/metrics', dependencies=[Depends(admin_key)])
def metrics(db: Session = Depends(db_session)):
    return {'products': db.scalar(select(func.count()).select_from(Product)), 'events': db.scalar(select(func.count()).select_from(Event)), 'pending_outbox': db.scalar(select(func.count()).select_from(Outbox).where(Outbox.delivered == False)), 'database': DRIVER}


@app.post('/api/admin/sync', dependencies=[Depends(admin_key)])
async def sync(batch: int = Query(100, ge=1, le=100), db: Session = Depends(db_session)):
    url = CONFIG['services']['gorse'].rstrip('/')
    if not url:
        raise HTTPException(503, 'GORSE_URL not configured')
    now = datetime.now(timezone.utc)
    owner = str(uuid.uuid4())
    available = ((Outbox.lease_until.is_(None)) | (Outbox.lease_until < now))
    due = ((Outbox.next_attempt_at.is_(None)) | (Outbox.next_attempt_at <= now))
    pending = select(Outbox.id).where(Outbox.delivered.is_(False), available, due).order_by(Outbox.id).limit(batch)
    # Atomic compare-and-swap lease works with SQLite >=3.35 and PostgreSQL.
    claimed = db.execute(update(Outbox)
        .where(Outbox.id.in_(pending), Outbox.delivered.is_(False), available, due)
        .values(lease_owner=owner, lease_until=now + timedelta(minutes=5))
        .returning(Outbox.id)).scalars().all()
    db.commit()
    rows = db.scalars(select(Outbox).where(Outbox.id.in_(claimed))).all() if claimed else []
    sent = 0
    failures = 0
    headers = {'X-API-Key': os.environ['GORSE_API_KEY']} if os.getenv('GORSE_API_KEY') else {}
    semaphore = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=5.0, headers=headers) as client:
        async def send(row):
            async with semaphore:
                try:
                    payload = json.loads(row.payload)
                    if row.kind == 'feedback':
                        response = await client.put(url + '/api/feedback', json=[payload])
                    elif row.kind == 'item':
                        response = await client.post(url + '/api/items', json=[payload])
                    else:
                        raise ValueError('Unsupported outbox kind')
                    response.raise_for_status()
                    return (row.id, True, '')
                except (httpx.HTTPError, ValueError) as exc:
                    return (row.id, False, str(exc)[:512])
        results = await asyncio.gather(*(send(row) for row in rows))
    for row_id, ok, error in results:
        values = {'lease_owner': None, 'lease_until': None}
        if ok:
            values.update(delivered=True, last_error=None)
            sent += 1
        else:
            old = next(row for row in rows if row.id == row_id)
            count = (old.attempts or 0) + 1
            values.update(attempts=count, last_error=error,
                          next_attempt_at=now + timedelta(seconds=min(1800, 2 ** min(count, 10))))
            failures += 1
        # Never ACK a row whose payload was changed/requeued during the request.
        db.execute(update(Outbox).where(Outbox.id == row_id, Outbox.lease_owner == owner).values(**values))
    db.commit()
    remaining = db.scalar(select(func.count()).select_from(Outbox).where(Outbox.delivered.is_(False)))
    return {'claimed': len(claimed), 'delivered': sent, 'failed': failures, 'remaining': remaining}
