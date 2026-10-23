from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import os
import sqlite3
import pytest
from sqlalchemy import Boolean, DateTime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from migrate_sqlite_to_postgres import canonical, convert_row, source_snapshot, validate_source
from app import Product, Event


def create_source(path: Path):
    env = dict(os.environ, DB_DRIVER='sqlite', SQLITE_PATH=str(path), APP_CONFIG=str(path.parent/'none.yaml'),
               SESSION_SECRET='migration-test-session-secret')
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=ROOT,
                   env=env, check=True, capture_output=True)


def test_source_dry_run_and_integrity(tmp_path):
    path = tmp_path / 'source.db'
    create_source(path)
    with source_snapshot(path) as conn:
        counts = validate_source(conn)
        assert counts['products'] == 0
        assert counts['outbox'] == 0
    output = subprocess.run([sys.executable, 'scripts/migrate_sqlite_to_postgres.py',
                             '--sqlite', str(path)], cwd=ROOT, capture_output=True, text=True, check=True)
    assert 'dry-run' in output.stdout
    assert not (tmp_path / 'source-new.db').exists()


def test_source_missing_and_revision_rejected(tmp_path):
    with pytest.raises(ValueError, match='does not exist'):
        source_snapshot(tmp_path/'missing.db')
    path = tmp_path / 'bad.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE alembic_version (version_num TEXT)')
    conn.execute("INSERT INTO alembic_version VALUES ('unknown')")
    conn.commit()
    conn.close()
    with source_snapshot(path) as source, pytest.raises(ValueError, match='alembic'):
        validate_source(source)


def test_row_normalization_for_timestamptz_and_boolean():
    row = {'at': '2026-01-01 01:02:03.100000', 'id': '123', 'visitor': 'u',
           'product_id': 'p', 'event_type': 'click', 'request_id': '',
           'position': -1, 'idempotency_key': None}
    normalized = convert_row(row, Event.__table__)
    assert normalized['at'] == datetime(2026, 1, 1, 1, 2, 3, 100000, tzinfo=timezone.utc)
    assert canonical(row, Event.__table__) == canonical(normalized, Event.__table__)
