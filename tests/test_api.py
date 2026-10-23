"""API contract, SQLite migration, idempotency, degraded backends and outbox."""
import importlib.util
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

MODULE_FILE = Path(__file__).resolve().parents[1] / 'services/api/app.py'


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_CONFIG', str(tmp_path / 'missing-config.yaml'))
    monkeypatch.setenv('DB_DRIVER', 'sqlite')
    monkeypatch.setenv('SQLITE_PATH', str(tmp_path / 'commerce.db'))
    monkeypatch.setenv('SESSION_SECRET', 'dev-test-long-secret-do-not-use-in-production')
    monkeypatch.setenv('ADMIN_KEY', 'testing-admin-key')
    monkeypatch.delenv('OPENSEARCH_URL', raising=False)
    monkeypatch.delenv('GORSE_URL', raising=False)
    name = 'commerce_api_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, MODULE_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    with TestClient(module.app) as client:
        with module.SessionLocal() as db:
            db.add_all([
                module.Product(id='test:headphone', source='test', source_id='headphone', title='Headphones', category='Audio', brand='Demo', price=50, rating=4.8),
                module.Product(id='test:speaker', source='test', source_id='speaker', title='Speaker', category='Audio', brand='Demo', price=30, rating=4.5),
                module.Product(id='test:percent', source='test', source_id='percent', title='100% Recycled Case', category='Accessories', price=15, rating=4.1),
            ])
            db.commit()
        yield module, client
    module.engine.dispose()
    sys.modules.pop(name, None)


def test_end_to_end(harness):
    app, client = harness
    assert client.get('/health/ready').status_code == 200
    assert client.get('/api/search', params={'q': 'Headphones'}).json()['items'][0]['id'] == 'test:headphone'
    r = client.post('/api/events', json={'event_type': 'click', 'product_id': 'test:headphone'})
    assert r.status_code == 201
    assert client.post('/api/cart', json={'product_id': 'test:headphone', 'quantity': 2}).status_code == 201
    assert client.get('/api/cart').json()['items'][0]['quantity'] == 2
    home = client.get('/api/recommendations/home').json()
    assert home['strategy'] == 'local-interest-popularity'
    assert 'visitor' not in home
    assert client.get('/api/admin/metrics').status_code == 403
    stats = client.get('/api/admin/metrics', headers={'X-Admin-Key': 'testing-admin-key'}).json()
    assert stats['events'] == 2 and stats['pending_outbox'] == 2


def test_idempotency_scope_conflicts(harness):
    app, client = harness
    event = {'event_type': 'click', 'product_id': 'test:headphone', 'idempotency_key': 'abc'}
    assert client.post('/api/events', json=event).status_code == 201
    assert client.post('/api/events', json=event).json()['duplicate'] is True
    assert client.post('/api/events', json={**event, 'product_id': 'test:speaker'}).status_code == 409
    other = TestClient(app.app)
    assert other.post('/api/events', json=event).json()['recorded'] is True
    with app.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(app.Event)) == 2


def test_search_wildcard_is_literal(harness):
    _, client = harness
    out = client.get('/api/search', params={'q': '%'}).json()
    assert [p['id'] for p in out['items']] == ['test:percent']
    assert client.get('/api/search', params={'q': ''}).status_code == 422


def test_origin_rejection(harness):
    _, client = harness
    r = client.post('/api/events', headers={'Origin': 'https://bad.example'},
                    json={'event_type': 'click', 'product_id': 'test:headphone'})
    assert r.status_code == 403
    assert client.post('/api/events', headers={'Origin': 'http://localhost:3000'},
                       json={'event_type': 'click', 'product_id': 'test:headphone'}).status_code == 201


def test_gorse_sync_retry_and_delivery(harness, monkeypatch):
    app, client = harness
    client.post('/api/events', json={'product_id': 'test:headphone', 'event_type': 'favorite'})
    app.CONFIG['services']['gorse'] = 'http://mockgorse'
    headers = {'X-Admin-Key': 'testing-admin-key'}
    attempts = []
    failure = [True]

    def handler(request):
        attempts.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(503 if failure[0] else 200, json={'RowAffected': 1})

    original = httpx.AsyncClient

    def factory(**kwargs):
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(app.httpx, 'AsyncClient', factory)
    first = client.post('/api/admin/sync', headers=headers).json()
    assert first['claimed'] == 1 and first['failed'] == 1
    assert attempts[0][0:2] == ('PUT', '/api/feedback')
    assert client.post('/api/admin/sync', headers=headers).json()['claimed'] == 0
    failure[0] = False
    with app.SessionLocal() as db:
        row = db.scalar(select(app.Outbox))
        assert row.attempts == 1 and row.next_attempt_at
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    second = client.post('/api/admin/sync', headers=headers).json()
    assert second['delivered'] == 1 and second['remaining'] == 0
    assert client.post('/api/admin/sync', headers=headers).json()['claimed'] == 0


def test_gorse_recommendation_does_not_load_first_5000_only(harness, monkeypatch):
    app, client = harness
    # Force >5000 rows; the highest-rated product must remain eligible.
    with app.SessionLocal() as db:
        db.add_all(app.Product(id=f'bulk:{i:05d}', source='test', source_id=str(i), title='Bulk', category='Bulk', rating=0) for i in range(5100))
        db.add(app.Product(id='zzz:recommended', source='test', title='Recommended', category='Bulk', rating=5))
        db.commit()
    ids = [p['id'] for p in client.get('/api/recommendations/home', params={'limit': 5}).json()['items']]
    assert 'zzz:recommended' in ids


def test_validation(harness):
    _, client = harness
    assert client.post('/api/events', json={'product_id': 'missing', 'event_type': 'click'}).status_code == 404
    assert client.post('/api/cart', json={'product_id': 'test:headphone', 'quantity': 0}).status_code == 422
    assert client.get('/api/products', params={'limit': 200}).status_code == 422


def test_existing_v1_schema_is_upgraded(tmp_path, monkeypatch):
    """An old database keeps all rows and gains the lease columns."""
    import sqlite3
    filename = tmp_path / 'old.db'
    conn = sqlite3.connect(filename)
    conn.execute('CREATE TABLE outbox (id VARCHAR(36) PRIMARY KEY, kind VARCHAR(20), payload TEXT, attempts INTEGER, delivered BOOLEAN)')
    conn.execute("INSERT INTO outbox (id,kind,payload,attempts,delivered) VALUES ('old','item','{}',0,0)")
    conn.commit()
    conn.close()
    monkeypatch.setenv('DB_DRIVER', 'sqlite')
    monkeypatch.setenv('SQLITE_PATH', str(filename))
    monkeypatch.setenv('APP_CONFIG', str(tmp_path / 'absent.yaml'))
    spec = importlib.util.spec_from_file_location('upgrade_api_'+uuid.uuid4().hex, MODULE_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        module.initialize_schema()
        with module.SessionLocal() as db:
            row = db.get(module.Outbox, 'old')
            assert row is not None and row.lease_owner is None and row.delivered is False
    finally:
        module.engine.dispose()
        sys.modules.pop(spec.name, None)


def test_outbox_leasing_is_exclusive(harness):
    app, client = harness
    from sqlalchemy import update
    with app.SessionLocal() as db:
        db.add(app.Outbox(id='test-lease', kind='item', payload='{}'))
        db.commit()
        now = datetime.now(timezone.utc)
        claim = update(app.Outbox).where(
            app.Outbox.id == 'test-lease',
            app.Outbox.lease_until.is_(None),
            app.Outbox.delivered.is_(False),
        ).values(lease_owner='worker-A', lease_until=now+timedelta(minutes=5)).returning(app.Outbox.id)
        assert db.scalars(claim).all() == ['test-lease']
        db.commit()
        assert db.scalars(claim).all() == []
        db.rollback()


def test_reimport_changed_item_requeues_delivered_outbox(harness, monkeypatch):
    app, _ = harness
    importer_path = Path(__file__).resolve().parents[1] / 'scripts/import_products.py'
    monkeypatch.setitem(sys.modules, 'app', app)
    spec = importlib.util.spec_from_file_location('importer_'+uuid.uuid4().hex, importer_path)
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    importer.import_rows([('Desk Lamp', 'Home', 'Maker A', 10)], 1, demo=True)
    with app.SessionLocal() as db:
        row = db.scalar(select(app.Outbox).where(app.Outbox.kind == 'item'))
        assert row is not None
        row.delivered = True
        db.commit()
    importer.import_rows([('Desk Lamp 2', 'Home', 'Maker B', 10)], 1, demo=True)
    with app.SessionLocal() as db:
        row = db.scalar(select(app.Outbox).where(app.Outbox.kind == 'item'))
        assert row.delivered is False
        assert 'Maker B' in row.payload
        assert db.get(app.Product, 'demo:0').title == 'Desk Lamp 2'
        assert db.scalar(select(func.count()).select_from(app.Outbox)) == 1


def test_search_provider_falls_back_when_down(harness, monkeypatch):
    app, client = harness
    app.CONFIG['services']['search'] = 'http://mock-search'
    original = httpx.AsyncClient

    def factory(**kwargs):
        return original(transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs)

    monkeypatch.setattr(app.httpx, 'AsyncClient', factory)
    result = client.get('/api/search', params={'q': 'Headphones'}).json()
    assert result['provider'] == 'sql'
    assert result['items'][0]['id'] == 'test:headphone'
