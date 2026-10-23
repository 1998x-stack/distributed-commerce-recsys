"""Commerce API: SQLite-first, PostgreSQL-ready, with search/recs degradation."""
from __future__ import annotations
import json
import os
import secrets
from contextlib import asynccontextmanager
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event as sqla_event, func, or_, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from starlette.middleware.sessions import SessionMiddleware


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


class EventInput(BaseModel):
    product_id: str
    event_type: Literal['impression', 'click', 'favorite', 'cart', 'purchase_demo']
    request_id: str = Field(default='', max_length=64)
    position: int = Field(default=-1, ge=-1, le=100000)
    idempotency_key: str | None = Field(default=None, max_length=100)


class CartInput(BaseModel):
    product_id: str
    quantity: int = Field(default=1, ge=1, le=20)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title='Distributed Commerce RecSys API', version='0.1.1', lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=os.getenv('SESSION_SECRET', 'development-only-change-me'), same_site='lax', https_only=os.getenv('COOKIE_SECURE', 'false').lower() == 'true')


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
    db.execute(select(func.count()).select_from(Product)).scalar_one()
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
            query = {'size': limit, 'query': {'bool': {'must': [{'multi_match': {'query': q, 'fields': ['title^4', 'brand^2', 'description', 'category^2']}}], 'filter': ([{'term': {'category.keyword': category}}] if category else [])}}}
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.post(base_url + '/commerce-products/_search', json=query)
                resp.raise_for_status()
                ids = [hit['_id'] for hit in resp.json().get('hits', {}).get('hits', [])]
                provider = 'opensearch'
        except (httpx.HTTPError, ValueError, KeyError):
            pass
    if provider == 'opensearch':
        mapped = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids))).all()}
        items = [as_product(mapped[i]) for i in ids if i in mapped]
    else:
        like = f'%{q}%'
        stmt = select(Product).where(or_(Product.title.ilike(like), Product.description.ilike(like), Product.brand.ilike(like)))
        if category:
            stmt = stmt.where(Product.category == category)
        items = [as_product(p) for p in db.scalars(stmt.limit(limit)).all()]
    return {'items': items, 'provider': provider, 'query': q}


WEIGHTS = {'impression': 0, 'click': 1, 'favorite': 3, 'cart': 5, 'purchase_demo': 8}


@app.post('/api/events', status_code=201)
def record_event(payload: EventInput, request: Request, db: Session = Depends(db_session)):
    ensure_product(db, payload.product_id)
    who = visitor(request)
    if payload.idempotency_key and db.scalar(select(Event.id).where(Event.idempotency_key == payload.idempotency_key)):
        return {'duplicate': True}
    ev = Event(id=str(uuid.uuid4()), visitor=who, product_id=payload.product_id, event_type=payload.event_type, request_id=payload.request_id, position=payload.position, idempotency_key=payload.idempotency_key)
    db.add(ev)
    if WEIGHTS[payload.event_type]:
        # Outbox is atomic with event record; worker retries if Gorse is unavailable.
        feedback = {'FeedbackType': payload.event_type, 'UserId': who, 'ItemId': payload.product_id, 'Value': float(WEIGHTS[payload.event_type]), 'Timestamp': datetime.now(timezone.utc).isoformat()}
        db.add(Outbox(id=str(uuid.uuid4()), kind='feedback', payload=json.dumps(feedback)))
    db.commit()
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
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f'{base}/api/recommend/{who}', params={'n': limit})
                resp.raise_for_status()
                ids = [r if isinstance(r, str) else r['Id'] for r in resp.json()]
            if ids:
                strategy = 'gorse'
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
    # Fallback: own positive events influence preference for categories.
    history = db.execute(select(Event.product_id, Event.event_type).where(Event.visitor == who).order_by(Event.at.desc()).limit(120)).all()
    seen = {pid for pid, _ in history}
    category_scores: dict[str, float] = {}
    if history:
        pmap = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(seen))).all()}
        for pid, etype in history:
            if pid in pmap:
                category = pmap[pid].category
                category_scores[category] = category_scores.get(category, 0) + WEIGHTS.get(etype, 0)
    all_products = db.scalars(select(Product).order_by(Product.id).limit(5000)).all()
    popularity = dict(db.execute(select(Event.product_id, func.count()).group_by(Event.product_id)).all())
    ranked = sorted(all_products, key=lambda p: (category_scores.get(p.category, 0) * 10 + popularity.get(p.id, 0), p.rating, p.id), reverse=True)
    ordered = []
    for pid in ids:
        p = next((x for x in all_products if x.id == pid), None)
        if p and p.id not in seen and p not in ordered:
            ordered.append(p)
    for p in ranked:
        if p.id not in seen and p not in ordered:
            ordered.append(p)
        if len(ordered) >= limit:
            break
    return {'items': [as_product(p) for p in ordered[:limit]], 'strategy': strategy, 'visitor': who}


@app.get('/api/recommendations/similar/{pid}')
def similar(pid: str, limit: int = Query(8, ge=1, le=30), db: Session = Depends(db_session)):
    p = ensure_product(db, pid)
    matches = db.scalars(select(Product).where(Product.category == p.category, Product.id != p.id).limit(limit)).all()
    return {'items': [as_product(x) for x in matches], 'strategy': 'category'}


@app.get('/api/admin/metrics', dependencies=[Depends(admin_key)])
def metrics(db: Session = Depends(db_session)):
    return {'products': db.scalar(select(func.count()).select_from(Product)), 'events': db.scalar(select(func.count()).select_from(Event)), 'pending_outbox': db.scalar(select(func.count()).select_from(Outbox).where(Outbox.delivered == False)), 'database': DRIVER}


@app.post('/api/admin/sync', dependencies=[Depends(admin_key)])
async def sync(batch: int = Query(100, ge=1, le=1000), db: Session = Depends(db_session)):
    url = CONFIG['services']['gorse'].rstrip('/')
    if not url:
        raise HTTPException(503, 'GORSE_URL not configured')
    rows = db.scalars(select(Outbox).where(Outbox.delivered == False).order_by(Outbox.id).limit(batch)).all()
    sent = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for row in rows:
            try:
                if row.kind == 'feedback':
                    response = await client.put(url + '/api/feedback', json=[json.loads(row.payload)])
                else:
                    response = await client.post(url + '/api/items', json=[json.loads(row.payload)])
                response.raise_for_status()
                row.delivered = True
                sent += 1
            except (httpx.HTTPError, ValueError):
                row.attempts += 1
    db.commit()
    return {'delivered': sent, 'remaining': db.scalar(select(func.count()).select_from(Outbox).where(Outbox.delivered == False))}
