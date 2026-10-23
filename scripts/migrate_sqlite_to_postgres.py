"""Offline, verifiable SQLite -> PostgreSQL commerce data migration.

Requires *matching Alembic head* on both databases; target business tables
must be EMPTY. Use during a maintenance window with all writers stopped.
The destination insert + verification is one transaction: failure rolls back.
Gorse's independent PostgreSQL store is never read or modified here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote

from sqlalchemy import Boolean, DateTime, create_engine, func, inspect, insert, select, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
from app import Base  # noqa: E402

# Operational gate is a singleton control row, not business data. It is
# initialized on the empty target by Alembic and intentionally not copied.
def business_tables():
    return [t for t in Base.metadata.sorted_tables if t.name != "search_maintenance"]



def canonical(row: dict, table) -> bytes:
    cleaned = {}
    for column in table.columns:
        v = row[column.name]
        if v is not None and isinstance(column.type, Boolean):
            v = bool(v)
        if v is not None and isinstance(column.type, DateTime):
            if not isinstance(v, datetime):
                v = datetime.fromisoformat(str(v))
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            v = v.astimezone(timezone.utc).isoformat(timespec='microseconds')
        cleaned[column.name] = v
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def convert_row(source: dict, table) -> dict:
    """SQLite stores timestamps as strings and booleans as integers."""
    row = dict(source)
    for col in table.columns:
        value = row[col.name]
        if value is None:
            continue
        if isinstance(col.type, DateTime) and not isinstance(value, datetime):
            value = datetime.fromisoformat(value)
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            row[col.name] = value
        elif isinstance(col.type, Boolean):
            row[col.name] = bool(value)
    return row


def primary_key(table):
    return [column.name for column in table.primary_key.columns]


def source_rows(conn, table):
    columns = ', '.join(f'"{c.name}"' for c in table.columns)
    order = ', '.join(f'"{key}"' for key in primary_key(table))
    cursor = conn.execute(f'SELECT {columns} FROM "{table.name}" ORDER BY {order}')
    for record in cursor:
        yield convert_row(dict(record), table)


def source_snapshot(sqlite_path: Path):
    if not sqlite_path.is_file():
        raise ValueError('SQLite source file does not exist')
    uri = 'file:' + quote(str(sqlite_path.resolve()), safe='/') + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('BEGIN')
    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        connection.close()
        raise ValueError('SQLite integrity_check failed')
    if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
        connection.close()
        raise ValueError('SQLite foreign_key_check failed')
    return connection


def check_revision_sqlite(conn):
    found = conn.execute('SELECT version_num FROM alembic_version').fetchone()
    if not found or found[0] != '0003_v18_catalog_visibility':
        raise ValueError('SQLite source needs alembic upgrade head (V1.5 or newer)')


def validate_source(conn):
    check_revision_sqlite(conn)
    counts = {}
    for table in business_tables():
        expected = {c.name for c in table.columns}
        actual = {r['name'] for r in conn.execute(f'PRAGMA table_info("{table.name}")')}
        if not expected <= actual:
            raise ValueError(f'Source schema mismatch: {table.name}: {expected-actual}')
        counts[table.name] = conn.execute(f'SELECT count(*) FROM "{table.name}"').fetchone()[0]
    return counts


def _hash(rows, table):
    h = hashlib.sha256()
    count = 0
    for row in rows:
        h.update(canonical(row, table) + b'\n')
        count += 1
    return count, h.hexdigest()


def copy_to_postgres(conn, url: str, *, batch_size: int = 500):
    if not url.startswith(('postgresql://', 'postgresql+psycopg://')):
        raise ValueError('Destination must be a PostgreSQL URL')
    if batch_size < 1 or batch_size > 5000:
        raise ValueError('batch_size must be 1..5000')
    pg_url = url.replace('postgresql://', 'postgresql+psycopg://', 1)
    pg = create_engine(pg_url, pool_pre_ping=True, connect_args={'connect_timeout': 10})
    try:
        with pg.begin() as dest:
            tables = business_tables()
            expected_tables = {t.name for t in tables}
            if not expected_tables <= set(inspect(dest).get_table_names()):
                raise ValueError('Destination schema missing; run Alembic upgrade head on PostgreSQL first')
            # Require fully offline migration. Blocking table locks make accidental
            # concurrent writers wait rather than silently interleave their rows.
            quoted_tables = ', '.join('"' + t.name + '"' for t in tables)
            dest.execute(text(f'LOCK TABLE {quoted_tables} IN ACCESS EXCLUSIVE MODE'))
            revision = dest.execute(text('SELECT version_num FROM alembic_version')).scalar_one_or_none()
            if revision != '0003_v18_catalog_visibility':
                raise ValueError('PostgreSQL target schema revision does not match SQLite source')
            counts = {}
            for table in tables:
                if dest.scalar(select(func.count()).select_from(table)):
                    raise ValueError(f'Destination not empty: {table.name}; no data overwritten')
            for table in tables:
                batch = []
                source_hash = hashlib.sha256()
                count = 0
                for row in source_rows(conn, table):
                    source_hash.update(canonical(row, table) + b'\n')
                    batch.append(row)
                    if len(batch) >= batch_size:
                        dest.execute(insert(table), batch)
                        count += len(batch)
                        batch.clear()
                if batch:
                    dest.execute(insert(table), batch)
                    count += len(batch)
                keycols = list(table.primary_key.columns)
                actual_count, actual_hash = _hash(dest.execute(select(table).order_by(*keycols).execution_options(stream_results=True, yield_per=batch_size)).mappings(), table)
                if count != actual_count or source_hash.hexdigest() != actual_hash:
                    raise ValueError(f'PostgreSQL verification failed for {table.name}; transaction rolled back')
                counts[table.name] = count
            # Preserve sequence continuity when explicitly importing serial PK IDs.
            for name in ('cart', 'product_visitors'):
                sequence = dest.scalar(text('SELECT pg_get_serial_sequence(:table, :pk)'), {'table': name, 'pk': 'id'})
                if sequence:
                    dest.execute(text(f'SELECT setval(CAST(:seq AS regclass), COALESCE((SELECT MAX(id) FROM "{name}"), 1), '
                                      f'(SELECT COUNT(*) > 0 FROM "{name}"))'), {'seq': sequence})
            return counts
    finally:
        pg.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sqlite', type=Path, required=True)
    parser.add_argument('--pg-url-env', default='DATABASE_URL_POSTGRES', help='Environment variable containing the target URL')
    parser.add_argument('--batch-size', type=int, default=500)
    parser.add_argument('--execute', action='store_true', help='Actually copy rows; default is dry-run')
    args = parser.parse_args(argv)
    with closing(source_snapshot(args.sqlite)) as source:
        counts = validate_source(source)
        if not args.execute:
            print(json.dumps({'mode': 'dry-run', 'source_counts': counts}, indent=2))
            return
        target = os.environ.get(args.pg_url_env, '')
        if not target:
            parser.error(f'Set {args.pg_url_env} to business PostgreSQL DSN')
        migrated = copy_to_postgres(source, target, batch_size=args.batch_size)
        if counts != migrated:
            raise RuntimeError('Unexpected count mismatch')
        print(json.dumps({'mode': 'committed', 'verified_counts': migrated}, indent=2))


if __name__ == '__main__':
    main()
