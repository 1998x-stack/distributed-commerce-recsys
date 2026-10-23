"""Cross-process fencing and transactional search alias promotion tests."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, update

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
from search_coordination import (SearchRebuildBusy, begin_rebuild, end_rebuild,
                                 renew_rebuild, sync_gate_open)


def test_rebuild_gate_blocks_sync_and_releases(harness):
    app, client = harness
    app.CONFIG['services']['search'] = 'http://opensearch.internal'
    owner = begin_rebuild(app.SessionLocal, app.SearchMaintenance, app.Outbox)
    with app.SessionLocal() as db:
        db.add(app.Outbox(id=str(uuid.uuid4()), kind='search',
                          payload=json.dumps({'id': 'test:speaker', 'title': 'Speaker'})))
        db.commit()
    try:
        result = client.post('/api/admin/search/sync', headers={'X-Admin-Key': 'testing-admin-key'})
        assert result.status_code == 423
        with app.SessionLocal() as db:
            assert db.scalar(select(app.Outbox.delivered).where(app.Outbox.kind == 'search')) is False
        with pytest.raises(SearchRebuildBusy):
            begin_rebuild(app.SessionLocal, app.SearchMaintenance, app.Outbox, wait_seconds=0)
        renew_rebuild(app.SessionLocal, app.SearchMaintenance, owner)
    finally:
        end_rebuild(app.SessionLocal, app.SearchMaintenance, owner)
    calls = []
    def fake(request):
        calls.append(request)
        return httpx.Response(200, json={'errors': False, 'items': [{'index': {'status': 201}}]})
    app.app.state.provider_http = httpx.Client(transport=httpx.MockTransport(fake))
    result = client.post('/api/admin/search/sync', headers={'X-Admin-Key': 'testing-admin-key'})
    assert result.status_code == 200 and result.json()['delivered'] == 1
    assert len(calls) == 1


def test_stale_rebuild_owner_cannot_renew_or_unlock(harness):
    app, _ = harness
    owner = begin_rebuild(app.SessionLocal, app.SearchMaintenance, app.Outbox)
    with app.SessionLocal.begin() as db:
        db.execute(update(app.SearchMaintenance).where(app.SearchMaintenance.id == 1)
                   .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
    with pytest.raises(SearchRebuildBusy, match='lease lost'):
        renew_rebuild(app.SessionLocal, app.SearchMaintenance, owner)
    replacement = begin_rebuild(app.SessionLocal, app.SearchMaintenance, app.Outbox)
    end_rebuild(app.SessionLocal, app.SearchMaintenance, owner)  # old owner must not unlock
    with app.SessionLocal() as db:
        assert not sync_gate_open(db, app.SearchMaintenance)
        db.rollback()
    end_rebuild(app.SessionLocal, app.SearchMaintenance, replacement)


def test_previously_claimed_sync_must_drain_before_build(harness):
    app, _ = harness
    with app.SessionLocal.begin() as db:
        db.add(app.Outbox(id=str(uuid.uuid4()), kind='search', payload='{}', delivered=False,
                          lease_owner='sync-1', lease_until=datetime.now(timezone.utc) + timedelta(minutes=5)))
    with pytest.raises(SearchRebuildBusy, match='in flight'):
        begin_rebuild(app.SessionLocal, app.SearchMaintenance, app.Outbox,
                      wait_seconds=0, sleep=lambda _: None)
    # Timed out build should release the gate, leaving the old alias intact.
    with app.SessionLocal() as db:
        assert sync_gate_open(db, app.SearchMaintenance)
        db.rollback()
    with app.SessionLocal.begin() as db:
        db.execute(update(app.Outbox).where(app.Outbox.kind == 'search')
                   .values(lease_owner=None, lease_until=None))
    owner = begin_rebuild(app.SessionLocal, app.SearchMaintenance, app.Outbox, wait_seconds=0)
    end_rebuild(app.SessionLocal, app.SearchMaintenance, owner)


def test_full_build_promotes_alias_after_quiescence_and_preserves_deltas(harness, monkeypatch):
    app, client = harness
    module_name = 'search_index_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(module_name, ROOT / 'scripts/index_search.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'SessionLocal', app.SessionLocal)
    monkeypatch.setattr(module, 'SearchMaintenance', app.SearchMaintenance)
    monkeypatch.setattr(module, 'Outbox', app.Outbox)
    monkeypatch.setattr(module, 'Product', app.Product)
    hits = []

    class FakeReply:
        def __init__(self, code=200, data=None):
            self.status_code = code
            self.data = data or {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError('bad', request=httpx.Request('GET', 'http://search'), response=httpx.Response(self.status_code))
        def json(self):
            return self.data

    class FakeSearch:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def put(self, url, **kwargs):
            hits.append(('put', url))
            return FakeReply()
        def post(self, url, **kwargs):
            hits.append(('post', url))
            if url.endswith('/_bulk'):
                return FakeReply(data={'errors': False, 'items': []})
            return FakeReply(data={'errors': False})
        def get(self, url, **kwargs):
            hits.append(('get', url))
            return FakeReply(code=404)
        def head(self, url, **kwargs):
            return FakeReply(code=404)
        def delete(self, url, **kwargs):
            hits.append(('delete', url))
            return FakeReply()

    monkeypatch.setattr(module.httpx, 'Client', FakeSearch)
    try:
        module.index_catalog('http://search', batch_size=2)
        assert any(method == 'post' and url.endswith('/_aliases') for method, url in hits)
        assert any(method == 'post' and url.endswith('/_bulk') for method, url in hits)
        with app.SessionLocal() as db:
            gate = db.get(app.SearchMaintenance, 1)
            assert gate.owner is None
    finally:
        sys.modules.pop(module_name, None)


def test_v16_schema_upgrades_without_losing_business_rows(tmp_path, monkeypatch):
    """The 0001 -> 0002 migration is additive, and API external mode accepts it."""
    import os
    import subprocess
    import sqlite3
    path = tmp_path / 'legacy-v16.db'
    env = dict(os.environ, DB_DRIVER='sqlite', SQLITE_PATH=str(path),
               APP_CONFIG=str(tmp_path / 'none.yml'),
               SESSION_SECRET='v17-local-tests-secret')
    for revision in ('0001_v15_baseline', 'head'):
        if revision == 'head':
            with sqlite3.connect(path) as conn:
                conn.execute("INSERT INTO products(id,source,source_id,title,description,category,brand,price,currency,image_url,rating,rating_count) VALUES ('p:legacy','test','legacy','Old row','','Books','',0,'USD','',0,0)")
        result = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', revision],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone()[0] == '0002_v17_search_gate'
        assert conn.execute("SELECT title FROM products WHERE id='p:legacy'").fetchone()[0] == 'Old row'
        assert conn.execute('SELECT owner FROM search_maintenance WHERE id=1').fetchone() == (None,)
