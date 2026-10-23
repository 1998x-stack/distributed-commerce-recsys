"""Run in CI against a disposable PostgreSQL service: PG_TEST_URL=postgresql://..."""
import importlib.util
import os
import sys
import uuid
from pathlib import Path
import pytest


@pytest.mark.skipif(not os.getenv('PG_TEST_URL'), reason='No PostgreSQL test service')
def test_postgres_business_mode(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_CONFIG', str(tmp_path / 'none.yaml'))
    monkeypatch.setenv('DB_DRIVER', 'postgresql')
    monkeypatch.setenv('DATABASE_URL_POSTGRES', os.environ['PG_TEST_URL'])
    monkeypatch.setenv('SESSION_SECRET', 'pg-integration-test-secret')
    monkeypatch.setenv('ADMIN_KEY', 'pg-admin-test')
    monkeypatch.delenv('OPENSEARCH_URL', raising=False)
    monkeypatch.delenv('GORSE_URL', raising=False)
    path = Path(__file__).resolve().parents[1] / 'services/api/app.py'
    name = 'pg_api_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    from fastapi.testclient import TestClient
    try:
        mod.Base.metadata.drop_all(mod.engine)
        with TestClient(mod.app) as client:
            with mod.SessionLocal() as db:
                db.add(mod.Product(id='test:pg-product', title='PG item', category='PG', rating=4.5))
                db.commit()
            data = {'product_id': 'test:pg-product', 'quantity': 2}
            headers = {'Idempotency-Key': 'same-key'}
            assert client.post('/api/cart', json=data, headers=headers).status_code == 201
            assert client.post('/api/cart', json=data, headers=headers).status_code == 200
            assert client.get('/api/cart').json()['items'][0]['quantity'] == 2
            assert client.post('/api/events', json={'product_id': 'test:pg-product',
                          'event_type': 'click'}).status_code == 201
            assert client.get('/api/recommendations/home').status_code == 200
        mod.Base.metadata.drop_all(mod.engine)
    finally:
        mod.engine.dispose()
        sys.modules.pop(name, None)
