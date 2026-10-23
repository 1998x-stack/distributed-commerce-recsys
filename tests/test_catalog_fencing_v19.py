"""V1.9 revision fencing and anti-entropy coverage, no external services needed."""
from __future__ import annotations

import json
import subprocess
import sys
import os
from pathlib import Path
import uuid

import httpx
from sqlalchemy import select

ADMIN = {'X-Admin-Key': 'testing-admin-key'}
PID = 'test:headphone'
ROOT = Path(__file__).resolve().parents[1]


def test_new_catalog_update_preserves_active_lease(harness):
    app, client = harness
    with app.SessionLocal() as db:
        p = db.get(app.Product, PID)
        app.queue_catalog_updates(db, p)
        db.commit()
        item = db.scalar(select(app.Outbox).where(app.Outbox.kind == 'item'))
        before = item.catalog_revision
        item.lease_owner = 'old-worker'
        item.lease_until = app.datetime.now(app.timezone.utc) + app.timedelta(minutes=4)
        db.commit()
    assert client.patch(f'/api/admin/products/{PID}/availability', json={'active': False}, headers=ADMIN).status_code == 200
    with app.SessionLocal() as db:
        item = db.scalar(select(app.Outbox).where(app.Outbox.kind == 'item'))
        assert item.catalog_revision == before + 1
        assert item.lease_owner == 'old-worker'
        assert not item.delivered
        # Stale worker must release its lease, not mark a new revision delivered.
        old = app.Outbox(id=item.id, catalog_revision=before, attempts=0)
        assert app.acknowledge_catalog_delivery(db, old, 'old-worker', True, '', app.datetime.now(app.timezone.utc)) == (False, False)
        db.commit()
        db.refresh(item)
        assert item.lease_owner is None and item.delivered is False
        assert json.loads(item.payload)['IsHidden'] is True


def test_sync_gorse_queued_revision_advances_during_network_call(harness):
    app, client = harness
    with app.SessionLocal() as db:
        app.queue_catalog_updates(db, db.get(app.Product, PID))
        db.commit()
    first_calls = []
    def remote(request):
        payload = json.loads(request.content)
        first_calls.append(payload)
        if len(first_calls) == 1:
            with app.SessionLocal() as db:
                p = db.get(app.Product, PID)
                p.is_active = False
                app.queue_catalog_updates(db, p)
                db.commit()
        return httpx.Response(200, json={'RowAffected': 1})
    app.CONFIG['services']['gorse'] = 'http://gorse.local'
    with httpx.Client(transport=httpx.MockTransport(remote)) as upstream:
        app.app.state.provider_http = upstream
        first = client.post('/api/admin/sync', headers=ADMIN)
        assert first.status_code == 200 and first.json()['delivered'] == 0
        with app.SessionLocal() as db:
            row = db.scalar(select(app.Outbox).where(app.Outbox.kind == 'item'))
            assert not row.delivered and row.lease_owner is None
        second = client.post('/api/admin/sync', headers=ADMIN)
        assert second.status_code == 200 and second.json()['delivered'] == 1
    assert first_calls[0][0]['IsHidden'] is False
    assert first_calls[1][0]['IsHidden'] is True


def test_search_bulk_version_and_superseded_ack(harness):
    app, client = harness
    with app.SessionLocal() as db:
        app.queue_catalog_updates(db, db.get(app.Product, PID))
        db.commit()
    calls = []
    def remote(request):
        lines = request.content.decode().strip().splitlines()
        meta = json.loads(lines[0])['index']
        calls.append(meta)
        if len(calls) == 1:
            with app.SessionLocal() as db:
                p = db.get(app.Product, PID)
                p.title = 'Changed title'
                app.queue_catalog_updates(db, p)
                db.commit()
        return httpx.Response(200, json={'errors': False, 'items': [{'index': {'status': 200}}]})
    app.CONFIG['services']['search'] = 'http://search.local'
    with httpx.Client(transport=httpx.MockTransport(remote)) as upstream:
        app.app.state.provider_http = upstream
        first = client.post('/api/admin/search/sync', headers=ADMIN)
        assert first.status_code == 200 and first.json()['delivered'] == 0
        second = client.post('/api/admin/search/sync', headers=ADMIN)
        assert second.status_code == 200 and second.json()['delivered'] == 1
    assert calls[0]['version_type'] == 'external_gte'
    assert calls[1]['version'] > calls[0]['version']
    with app.SessionLocal() as db:
        assert db.scalar(select(app.Outbox.delivered).where(app.Outbox.kind == 'search')) is True


def test_remote_reconcile_read_only_then_repair(harness):
    app, client = harness
    app.CONFIG['services']['gorse'] = 'http://gorse.local'
    app.CONFIG['services']['search'] = ''
    def missing(request):
        assert request.url.path.startswith('/api/item/')
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(missing)) as upstream:
        app.app.state.provider_http = upstream
        original = client.post('/api/admin/catalog/reconcile', params={'limit': 3}, headers=ADMIN).json()
        assert original['checked'] == 3 and len(original['drift']) == 3
        assert original['requeued'] == 0
        with app.SessionLocal() as db:
            assert db.scalar(select(app.Outbox.id).where(app.Outbox.kind == 'item')) is None
        repaired = client.post('/api/admin/catalog/reconcile', params={'limit': 3, 'repair': True}, headers=ADMIN).json()
        assert repaired['requeued'] == 3
        with app.SessionLocal() as db:
            items = db.scalars(select(app.Outbox).where(app.Outbox.kind == 'item')).all()
            assert len(items) == 3 and all(not r.delivered for r in items)
    assert client.post('/api/admin/catalog/reconcile').status_code == 403


def test_reconcile_failed_backend_does_not_requeue(harness):
    app, client = harness
    app.CONFIG['services']['search'] = 'http://search.local'
    def outage(_):
        return httpx.Response(503)
    with httpx.Client(transport=httpx.MockTransport(outage)) as upstream:
        app.app.state.provider_http = upstream
        result = client.post('/api/admin/catalog/reconcile', params={'repair': True}, headers=ADMIN).json()
        assert result['requeued'] == 0 and result['errors']


def test_v18_sqlite_upgrade_with_catalog_revisions(tmp_path):
    import sqlite3
    path = tmp_path / 'upgraded.db'
    env = dict(os.environ, DB_DRIVER='sqlite', SQLITE_PATH=str(path),
               APP_CONFIG=str(tmp_path / 'empty.yaml'), SESSION_SECRET='test-secret')
    result = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', '0003_v18_catalog_visibility'],
                            cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO products(id,source,source_id,title,description,category,brand,price,currency,image_url,rating,rating_count,is_active) VALUES ('old:item','demo','item','Old','','Audio','',0,'USD','',0,0,0)")
        conn.execute("INSERT INTO outbox(id, kind, payload, attempts, delivered) VALUES ('old:outbox', 'item', '{}', 0, 0)")
    result = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'],
                            cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone()[0] == '0004_v19_catalog_fencing'
        assert conn.execute("SELECT is_active, catalog_revision FROM products WHERE id='old:item'").fetchone() == (0, 1)
        assert conn.execute("SELECT delivered, catalog_revision FROM outbox WHERE id='old:outbox'").fetchone() == (0, 1)


def test_indexer_bulk_uses_product_revision_without_mutation():
    import importlib.util
    spec = importlib.util.spec_from_file_location('indexer_v19_' + uuid.uuid4().hex,
                                                  ROOT / 'scripts/index_search.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    content = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'errors': False}
    class Client:
        def post(self, _, *, content: str, headers: dict):
            lines = content.splitlines()
            assert len(lines) == 2
            action = json.loads(lines[0])['index']
            assert action['version'] == 42 and action['version_type'] == 'external_gte'
            assert json.loads(lines[1]) == {'title': 'Screen'}
            return Response()
    product = {'id': 'p1', 'title': 'Screen'}
    try:
        module.write_bulk(Client(), 'http://search.local', 'commerce-products-v1', [(product, 42)])
        assert product == {'id': 'p1', 'title': 'Screen'}
    finally:
        sys.modules.pop(spec.name, None)


def test_catalog_requeue_cli_dry_run_is_noop(tmp_path):
    import sqlite3
    path = tmp_path / 'requeue.db'
    env = dict(os.environ, DB_DRIVER='sqlite', SQLITE_PATH=str(path),
               APP_CONFIG=str(tmp_path / 'empty.yaml'), SCHEMA_MODE='external', SESSION_SECRET='test-secret')
    setup = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'],
                           env=env, cwd=ROOT, capture_output=True, text=True)
    assert setup.returncode == 0, setup.stderr
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO products(id,source,source_id,title,description,category,brand,price,currency,image_url,rating,rating_count,is_active,catalog_revision) VALUES ('x','demo','x','X','','C','',0,'USD','',0,0,1,1)")
    argv = [sys.executable, 'scripts/requeue_catalog.py', '--batch-size', '1']
    dry = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
    assert dry.returncode == 0, dry.stderr
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
    run = subprocess.run(argv + ['--execute'], cwd=ROOT, env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 2
        assert conn.execute("SELECT catalog_revision FROM products WHERE id='x'").fetchone()[0] == 1
