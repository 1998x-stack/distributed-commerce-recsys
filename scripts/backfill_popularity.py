"""Rebuild transactional unique-visitor popularity from historical events.

Run while API writes are paused to prevent races between recomputation and
incoming live events. Works with both SQLite and PostgreSQL business DBs.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import func, select, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
from app import Event, ProductStat, ProductVisitor, SessionLocal, initialize_schema  # noqa: E402


def backfill() -> dict[str, int]:
    initialize_schema()
    with SessionLocal.begin() as db:
        # Single SQL statement for the distinct positives, using database-side
        # conflict handling; never materialize the interaction history in Python.
        db.execute(text("""
            INSERT INTO product_visitors (visitor, product_id)
            SELECT visitor, product_id FROM events
            WHERE event_type IN ('click', 'favorite', 'cart', 'purchase_demo', 'review')
            GROUP BY visitor, product_id
            ON CONFLICT(visitor, product_id) DO NOTHING
        """))
        db.execute(text("""
            INSERT INTO product_stats (product_id, unique_visitors)
            SELECT product_id, COUNT(*) FROM product_visitors
            WHERE 1=1 GROUP BY product_id
            ON CONFLICT(product_id) DO UPDATE
                SET unique_visitors = excluded.unique_visitors
        """))
        visitors = db.scalar(select(func.count()).select_from(ProductVisitor))
        products = db.scalar(select(func.count()).select_from(ProductStat))
        return {'unique_product_visitors': visitors, 'products_with_popularity': products}


if __name__ == '__main__':
    print('Stop API and sync writes before backfill; take a database backup first.')
    print(backfill())
