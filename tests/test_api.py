import importlib.util
import os
import sys
from pathlib import Path

os.environ['DB_DRIVER'] = 'sqlite'
os.environ['SQLITE_PATH'] = '/tmp/commerce-recsys-pytest.db'
os.environ['SESSION_SECRET'] = 'dev-test-long-secret'
os.environ['ADMIN_KEY'] = 'testing-admin-key'
module_path = Path(__file__).resolve().parents[1] / 'services/api/app.py'
spec = importlib.util.spec_from_file_location('commerce_api', module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

from fastapi.testclient import TestClient


def test_end_to_end():
    module.Base.metadata.create_all(module.engine)
    with module.SessionLocal() as db:
        db.merge(module.Product(id='test:headphone', source='test', source_id='headphone', title='Headphones', category='Audio', brand='Demo', price=50))
        db.merge(module.Product(id='test:speaker', source='test', source_id='speaker', title='Speaker', category='Audio', brand='Demo', price=30))
        db.commit()
    with TestClient(module.app) as client:
        assert client.get('/health/ready').status_code == 200
        assert client.get('/api/search', params={'q': 'Headphones'}).json()['items'][0]['id'] == 'test:headphone'
        r = client.post('/api/events', json={'event_type': 'click', 'product_id': 'test:headphone'})
        assert r.status_code == 201
        assert client.post('/api/cart', json={'product_id': 'test:headphone', 'quantity': 2}).status_code == 201
        cart = client.get('/api/cart').json()['items']
        assert cart[0]['quantity'] == 2
        home = client.get('/api/recommendations/home').json()
        assert 'items' in home and home['strategy'] == 'local-interest-popularity'
        assert client.get('/api/admin/metrics').status_code == 403
        stats = client.get('/api/admin/metrics', headers={'X-Admin-Key': 'testing-admin-key'}).json()
        assert stats['events'] >= 2 and stats['pending_outbox'] >= 2
