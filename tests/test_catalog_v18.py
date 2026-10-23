"""V1.8 catalog availability, outbox, index deletion and migration tests."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import httpx
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
ADMIN = {'X-Admin-Key': 'testing-admin-key'}
PID = 'test:headphone'


def test_soft_delete_is_idempotent_and_preserves_history(harness):
    app, client = harness
    assert client.post('/api/events', json={'product_id': PID, 'event_type': 'click'}).status_code == 201
    assert client.post('/api/cart', json={'product_id': PID}).status_code == 201
    denied = client.patch(f'/api/admin/products/{PID}/availability', json={'active': False})
    assert denied.status_code == 403
    changed = client.patch(f'/api/admin/products/{PID}/availability', json={'active': False}, headers=ADMIN)
    assert changed.status_code == 200 and changed.json()['changed'] is True
    assert client.get(f'/api/products/{PID}').status_code == 404
    assert PID not in [x['id'] for x in client.get('/api/products').json()['items']]
    assert PID not in [x['id'] for x in client.get('/api/search?q=Headphones').json()['items']]
    assert PID not in [x['id'] for x in client.get('/api/recommendations/home').json()['items']]
    assert client.get(f'/api/recommendations/similar/{PID}').status_code == 404
    assert client.get('/api/cart').json()['items'] == []
    assert client.post('/api/events', json={'product_id': PID, 'event_type': 'click'}).status_code == 404
    assert client.post('/api/cart', json={'product_id': PID}).status_code == 404
    with app.SessionLocal() as db:
        assert db.scalar(select(app.Event).where(app.Event.product_id == PID)) is not None
        assert db.get(app.Product, PID).is_active is False
        pending = {x.kind: json.loads(x.payload) for x in db.scalars(select(app.Outbox)).all()}
        assert pending['search'] == {'id': PID, '_op': 'delete'}
        assert pending['item']['IsHidden'] is True
    duplicate = client.patch(f'/api/admin/products/{PID}/availability', json={'active': False}, headers=ADMIN)
    assert duplicate.status_code == 200 and duplicate.json()['changed'] is False
    revived = client.patch(f'/api/admin/products/{PID}/availability', json={'active': True}, headers=ADMIN)
    assert revived.status_code == 200 and revived.json()['changed'] is True
    assert client.get(f'/api/products/{PID}').status_code == 200
    with app.SessionLocal() as db:
        outbox = {x.kind: json.loads(x.payload) for x in db.scalars(select(app.Outbox)).all()}
        assert outbox['item']['IsHidden'] is False
        assert '_op' not in outbox['search']


def test_search_bulk_delete_acknowledges_missing_document(harness):
    app, client = harness
    assert client.patch(f'/api/admin/products/{PID}/availability', json={'active': False}, headers=ADMIN).status_code == 200
    requests = []
    def bulk(request):
        requests.append(request)
        body = request.content.decode()
        assert '"delete"' in body and '"index"' not in body
        assert len(body.strip().splitlines()) == 1
        return httpx.Response(200, json={'errors': False, 'items': [
            {'delete': {'status': 404, 'result': 'not_found'}}]})
    app.CONFIG['services']['search'] = 'http://opensearch.local:9200'
    with httpx.Client(transport=httpx.MockTransport(bulk)) as client_http:
        app.app.state.provider_http = client_http
        result = client.post('/api/admin/search/sync', headers=ADMIN)
    assert result.status_code == 200
    assert result.json()['delivered'] == 1 and result.json()['failed'] == 0
    assert len(requests) == 1
    with app.SessionLocal() as db:
        assert db.scalar(select(app.Outbox.delivered).where(app.Outbox.kind == 'search')) is True


def test_search_delete_failure_keeps_outbox(harness):
    app, client = harness
    client.patch(f'/api/admin/products/{PID}/availability', json={'active': False}, headers=ADMIN)
    app.CONFIG['services']['search'] = 'http://opensearch.local:9200'
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        'errors': True, 'items': [{'delete': {'status': 429, 'error': 'busy'}}]}))) as transport:
        app.app.state.provider_http = transport
        result = client.post('/api/admin/search/sync', headers=ADMIN)
    assert result.status_code == 200 and result.json()['failed'] == 1
    with app.SessionLocal() as db:
        record = db.scalar(select(app.Outbox).where(app.Outbox.kind == 'search'))
        assert not record.delivered and record.attempts == 1


def test_indexer_skips_inactive_catalog_records(harness, monkeypatch):
    app, client = harness
    client.patch(f'/api/admin/products/{PID}/availability', json={'active': False}, headers=ADMIN)
    name = 'search_indexer_v18_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/index_search.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'SessionLocal', app.SessionLocal)
    monkeypatch.setattr(module, 'Product', app.Product)
    monkeypatch.setattr(module, 'SearchMaintenance', app.SearchMaintenance)
    monkeypatch.setattr(module, 'Outbox', app.Outbox)
    indexed = []
    class FakeResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {'errors': False}
    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def put(self, *a, **kw): return FakeResponse()
        def post(self, path, **kw):
            if path.endswith('/_bulk'):
                indexed.extend(json.loads(x) for x in kw['content'].splitlines()[1::2])
            return FakeResponse()
        def get(self, *a, **kw):
            r = FakeResponse(); r.status_code = 404; return r
        def head(self, *a, **kw):
            r = FakeResponse(); r.status_code = 404; return r
    monkeypatch.setattr(module.httpx, 'Client', FakeClient)
    try:
        module.index_catalog('http://fake', batch_size=2)
        assert all(item['title'] != 'Headphones' for item in indexed)
        assert {x['title'] for x in indexed} == {'Speaker', '100% Recycled Case'}
    finally:
        sys.modules.pop(name, None)


def test_v17_db_upgrade_to_v18_preserves_data(tmp_path):
    path = tmp_path / 'upgrade.db'
    env = dict(os.environ, DB_DRIVER='sqlite', SQLITE_PATH=str(path),
               APP_CONFIG=str(tmp_path / 'empty.yaml'), SESSION_SECRET='v18-test-secret')
    for revision in ('0002_v17_search_gate', 'head'):
        if revision == 'head':
            import sqlite3
            with sqlite3.connect(path) as conn:
                conn.execute('INSERT INTO products(id,source,source_id,title,description,category,brand,price,currency,image_url,rating,rating_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                             ('p:old', 'test', 'old', 'Old', '', 'Catalog', '', 12, 'USD', '', 0, 0))
        result = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', revision],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    import sqlite3
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone()[0] == '0004_v19_catalog_fencing'
        assert conn.execute("SELECT title, is_active FROM products WHERE id='p:old'").fetchone() == ('Old', 1)


def test_reimport_does_not_undo_manual_withdrawal(harness, monkeypatch):
    app, client = harness
    monkeypatch.setitem(sys.modules, 'app', app)
    name = 'import_v18_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/import_products.py')
    importer = importlib.util.module_from_spec(spec)
    sys.modules[name] = importer
    spec.loader.exec_module(importer)
    try:
        importer.import_rows([('Test keyboard', 'Audio', 'Demo', 29)], 1, demo=True)
        assert client.patch('/api/admin/products/demo:0/availability',
                            json={'active': False}, headers=ADMIN).status_code == 200
        importer.import_rows([('Test keyboard v2', 'Audio', 'Demo', 30)], 1, demo=True)
        assert client.get('/api/products/demo:0').status_code == 404
        with app.SessionLocal() as db:
            p = db.get(app.Product, 'demo:0')
            assert p.title == 'Test keyboard v2' and p.is_active is False
            row = db.get(app.Outbox, str(uuid.uuid5(uuid.NAMESPACE_URL, 'search-item-demo:0')))
            assert json.loads(row.payload) == {'id': 'demo:0', '_op': 'delete'}
    finally:
        sys.modules.pop(name, None)


def test_gorse_hides_withdrawn_item(harness):
    app, client = harness
    client.patch(f'/api/admin/products/{PID}/availability', json={'active': False}, headers=ADMIN)
    observed = []
    def post(request):
        observed.append(request)
        assert request.url.path == '/api/items'
        assert request.method == 'POST'
        payload = json.loads(request.content)
        assert len(payload) == 1 and payload[0]['IsHidden'] is True
        return httpx.Response(200, json={'RowAffected': 1})
    app.CONFIG['services']['gorse'] = 'http://gorse.internal:8087'
    with httpx.Client(transport=httpx.MockTransport(post)) as transport:
        app.app.state.provider_http = transport
        response = client.post('/api/admin/sync', headers=ADMIN)
    assert response.status_code == 200
    assert response.json()['delivered'] == 1
    assert len(observed) == 1
