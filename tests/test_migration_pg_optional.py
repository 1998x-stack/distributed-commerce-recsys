"""Real source->target migration, executed by CI PostgreSQL service container."""
from __future__ import annotations
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'services/api'))
from migrate_sqlite_to_postgres import source_snapshot, copy_to_postgres
from app import Base, Event, Product, Outbox


def run_migration(env):
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'],
                   env=env, cwd=ROOT, capture_output=True, text=True, check=True)


@pytest.mark.skipif(not os.getenv('PG_TEST_URL'), reason='Requires disposable PostgreSQL service')
def test_copy_sqlite_to_postgres_and_refuse_nonempty(tmp_path):
    sqlite = tmp_path / 'commerce.db'
    sqlite_env = dict(os.environ, DB_DRIVER='sqlite', SQLITE_PATH=str(sqlite),
                      APP_CONFIG=str(tmp_path / 'no-config.yaml'), SESSION_SECRET='pg-migrate-test-key')
    run_migration(sqlite_env)
    src = sqlite3.connect(sqlite)
    src.execute("INSERT INTO products(id,source,source_id,title,description,category,brand,price,currency,image_url,rating,rating_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ('p1','test','p1','test item','desc','misc','brand',19.99,'USD','',4.2,10))
    src.execute("INSERT INTO events (id,visitor,product_id,event_type,at,request_id,position) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()),str(uuid.uuid4()),'p1','click','2026-01-01 12:30:00.000000','req',2))
    src.execute("INSERT INTO outbox(id,kind,payload,attempts,delivered,created_at) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),'search','{"id":"p1"}',0,0,'2026-01-01 12:00:00.000000'))
    src.execute("INSERT INTO cart (visitor,product_id,quantity) VALUES(?,?,?)", ('visitor1','p1',2))
    src.commit()
    src.close()

    pg_url = os.environ['PG_TEST_URL']
    pg_engine = create_engine(pg_url.replace('postgresql://','postgresql+psycopg://',1))
    try:
        Base.metadata.drop_all(pg_engine)
        with pg_engine.begin() as dest:
            dest.execute(text('DROP TABLE IF EXISTS alembic_version'))
        pg_env = dict(os.environ, DB_DRIVER='postgresql', DATABASE_URL_POSTGRES=pg_url,
                      APP_CONFIG=str(tmp_path / 'no-config.yaml'), SESSION_SECRET='pg-migrate-test-key')
        run_migration(pg_env)
        with source_snapshot(sqlite) as snapshot:
            counts = copy_to_postgres(snapshot, pg_url)
            assert counts['products'] == 1
            assert counts['events'] == 1
            assert counts['cart'] == 1
            with pytest.raises(ValueError, match='not empty'):
                copy_to_postgres(snapshot, pg_url)
        with pg_engine.begin() as dest:
            assert dest.scalar(select(func.count()).select_from(Event)) == 1
            assert dest.scalar(select(Product.title).where(Product.id == 'p1')) == 'test item'
            assert dest.scalar(select(Outbox.delivered).limit(1)) is False
            # SERIAL/IDENTITY sequence should continue after explicit id import.
            seq = dest.scalar(text("SELECT pg_get_serial_sequence('cart','id')"))
            assert seq is not None
            with dest.begin_nested():
                new_id = dest.scalar(text("INSERT INTO cart(visitor,product_id,quantity) VALUES ('v2','p1',1) RETURNING id"))
                assert new_id > 1
    finally:
        Base.metadata.drop_all(pg_engine)
        with pg_engine.begin() as dest:
            dest.execute(text('DROP TABLE IF EXISTS alembic_version'))
        pg_engine.dispose()
