"""V1.9 one-time catalog Outbox resnapshot after upgrading legacy schemas.

This is non-destructive and defaults to a dry run. Stop API/imports/sync first;
then --execute to enqueue fresh per-item snapshots and increment revisions.
"""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path
from sqlalchemy import select, func

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
from app import Product, SessionLocal, check_schema_revision, queue_catalog_updates


def run(*, execute: bool = False, batch_size: int = 200):
    check_schema_revision()
    processed = 0
    last = ''
    while True:
        with SessionLocal() as db:
            rows = db.scalars(select(Product).where(Product.id > last).order_by(Product.id).limit(batch_size)).all()
            if not rows:
                break
            for p in rows:
                if execute:
                    queue_catalog_updates(db, p, force=True)
                processed += 1
            if execute:
                db.commit()
            last = rows[-1].id
    return {'products': processed, 'requeued': processed if execute else 0, 'dry_run': not execute}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--batch-size', type=int, default=200)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 2000:
        parser.error('--batch-size must be in 1..2000')
    print(run(execute=args.execute, batch_size=args.batch_size))
