import importlib.util
import sys
import uuid
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
MODULE_FILE = Path(__file__).resolve().parents[1] / "services/api/app.py"

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

