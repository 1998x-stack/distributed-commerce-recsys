"""V1.5 migration and incremental search outbox regression suite."""
from __future__ import annotations
import importlib.util
import json
import sys
import uuid
from pathlib import Path
import os
import subprocess

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

ROOT = Path(__file__).resolve().parents[1]


def run_alembic(path: Path, monkeypatch):
    monkeypatch.setenv('DB_DRIVER', 'sqlite')
    monkeypatch.setenv('APP_CONFIG', str(path.parent / 'none.yaml'))
    monkeypatch.setenv('SQLITE_PATH', str(path))
    monkeypatch.setenv('SESSION_SECRET', 'local-test-secret-not-production')
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'],
                   cwd=ROOT, env=os.environ.copy(), check=True, capture_output=True, text=True)


def test_alembic_fresh_and_idempotent_upgrade(tmp_path, monkeypatch):
    path = tmp_path / 'fresh.db'
    run_alembic(path, monkeypatch)
    run_alembic(path, monkeypatch)
    with create_engine('sqlite:///' + str(path)).connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0004_v19_catalog_fencing'
        assert conn.execute(text('SELECT count(*) FROM products')).scalar_one() == 0
        assert conn.execute(text("SELECT name FROM sqlite_master WHERE name='products_fts'")).scalar_one() == 'products_fts'


def test_alembic_adopts_existing_v14_data(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.db'
    # A V1.4 database already has business data and no alembic_version.
    # The migration must not truncate data or regenerate visitor ids.
    monkeypatch.setenv('DB_DRIVER', 'sqlite')
    monkeypatch.setenv('APP_CONFIG', str(tmp_path / 'none.yaml'))
    monkeypatch.setenv('SQLITE_PATH', str(path))
    name = 'v14_before_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, ROOT / 'services/api/app.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.initialize_schema()
    with module.SessionLocal() as db:
        db.add(module.Product(id='legacy:1', title='Legacy product', category='Demo', price=25))
        db.commit()
    module.engine.dispose()
    sys.modules.pop(name, None)
    run_alembic(path, monkeypatch)
    with create_engine('sqlite:///' + str(path)).connect() as conn:
        assert conn.execute(text("SELECT title FROM products WHERE id='legacy:1'")).scalar_one() == 'Legacy product'
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0004_v19_catalog_fencing'


def test_search_sync_partial_failures_and_retry(harness):
    app, client = harness
    app.CONFIG['services']['search'] = 'http://opensearch.local:9200'
    with app.SessionLocal() as db:
        for idx in range(3):
            db.add(app.Outbox(id=str(uuid.uuid4()), kind='search',
                              payload=json.dumps(app.search_document(db.get(app.Product, 'test:headphone')))))
        db.commit()
    recorded = []
    def fake_bulk(request):
        recorded.append(request)
        assert request.url.path == '/_bulk'
        assert request.headers.get('content-type') == 'application/x-ndjson'
        assert b'Headphones' in request.content
        return httpx.Response(200, json={'errors': True, 'items': [
            {'index': {'status': 201}},
            {'index': {'status': 429, 'error': {'type': 'es_rejected_execution_exception'}}},
            {'index': {'status': 200}},
        ]})
    app.app.state.provider_http = httpx.Client(transport=httpx.MockTransport(fake_bulk))
    sync = client.post('/api/admin/search/sync?batch=10', headers={'X-Admin-Key': 'testing-admin-key'})
    assert sync.status_code == 200
    assert sync.json()['delivered'] == 2 and sync.json()['failed'] == 1
    assert sync.json()['remaining'] == 1
    assert len(recorded) == 1
    with app.SessionLocal() as db:
        failed = db.scalar(select(app.Outbox).where(app.Outbox.kind == 'search', app.Outbox.delivered.is_(False)))
        assert failed.attempts == 1
        assert failed.last_error
    # Gorse endpoint must never lease/consume pending search updates.
    app.CONFIG['services']['gorse'] = 'http://gorse.local'
    sync_gorse = client.post('/api/admin/sync?batch=10', headers={'X-Admin-Key': 'testing-admin-key'})
    assert sync_gorse.json()['claimed'] == 0
    assert sync_gorse.json()['remaining'] == 0


def test_search_sync_failure_does_not_ack(harness):
    app, client = harness
    app.CONFIG['services']['search'] = 'http://opensearch.local:9200'
    with app.SessionLocal() as db:
        db.add(app.Outbox(id=str(uuid.uuid4()), kind='search', payload=json.dumps({'id': 'test:headphone', 'title': 'Headphones'})))
        db.commit()
    app.app.state.provider_http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    result = client.post('/api/admin/search/sync', headers={'X-Admin-Key': 'testing-admin-key'})
    assert result.status_code == 200
    assert result.json()['failed'] == 1
    assert result.json()['remaining'] == 1


def test_external_mode_refuses_unmigrated_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_DRIVER', 'sqlite')
    monkeypatch.setenv('APP_CONFIG', str(tmp_path / 'none.yaml'))
    monkeypatch.setenv('SQLITE_PATH', str(tmp_path / 'no-migration.db'))
    monkeypatch.setenv('SCHEMA_MODE', 'external')
    monkeypatch.setenv('SESSION_SECRET', 'local-test-long-secret')
    name = 'unversioned_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, ROOT / 'services/api/app.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    try:
        with pytest.raises(RuntimeError, match='run `alembic upgrade head`'):
            with TestClient(module.app):
                pass
    finally:
        module.engine.dispose()
        sys.modules.pop(name, None)


def test_reimport_does_not_duplicate_search_outbox(harness, monkeypatch):
    app, _ = harness
    monkeypatch.setitem(sys.modules, 'app', app)
    name = 'importer_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/import_products.py')
    importer = importlib.util.module_from_spec(spec)
    sys.modules[name] = importer
    spec.loader.exec_module(importer)
    importer.import_rows([('Headphones', 'Audio', 'A', 50)], 1, demo=True)
    with app.SessionLocal() as db:
        search = db.scalar(select(app.Outbox).where(app.Outbox.kind == 'search'))
        assert search is not None
        original_id = search.id
        search.delivered = True
        db.commit()
    importer.import_rows([('Headphones Plus', 'Audio', 'A', 55)], 1, demo=True)
    with app.SessionLocal() as db:
        search = db.scalar(select(app.Outbox).where(app.Outbox.kind == 'search'))
        assert search.id == original_id
        assert search.delivered is False
        assert 'Headphones Plus' in search.payload
        assert db.scalar(select(__import__('sqlalchemy').func.count()).select_from(app.Outbox).where(app.Outbox.kind == 'search')) == 1


def test_index_bulk_does_not_modify_source_documents():
    name = 'indexer_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/index_search.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    data = [{'id': 'sample:1', 'title': 'Monitor', 'category': 'PC'}]
    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {'errors': False}
    class FakeClient:
        def post(self, path, content, headers):
            assert '"_id": "sample:1"' in content
            return FakeResponse()
    try:
        module.write_bulk(FakeClient(), 'http://search', 'commerce-products-v123', data)
        assert data[0]['id'] == 'sample:1'
    finally:
        sys.modules.pop(name, None)
