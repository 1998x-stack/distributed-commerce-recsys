"""Opt-in Amazon Reviews 2023 positive historical interaction importer.

Examples:
  PYTHONPATH=services/api python scripts/import_reviews.py --file Electronics.jsonl.gz --limit 100000

Only reviews rating >=4 are treated as *positive* implicit feedback. User IDs
are converted to deterministic pseudonymous UUIDs (not anonymization). Source
files are research data and must not be redistributed contrary to their license.
"""
from __future__ import annotations
import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
sys.path.insert(0, str(ROOT / 'scripts'))
from app import Event, Outbox, Product, SessionLocal, initialize_schema, insert_on_conflict  # noqa: E402
from import_products import stream


def normalize_review(raw):
    asin = raw.get('parent_asin') or raw.get('asin')
    user = raw.get('user_id')
    rating = raw.get('rating')
    stamp = raw.get('timestamp')
    if not asin or not user or not isinstance(rating, (int, float)) or rating < 4 or stamp is None:
        return None
    try:
        value = float(stamp)
        at = datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        return None
    who = str(uuid.uuid5(uuid.NAMESPACE_URL, 'amazon-2023-user:' + str(user)))
    pid = 'amazon:' + str(asin)
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'amazon-review:{user}:{asin}:{stamp}'))
    return who, pid, at, event_id


def import_reviews(rows, limit=100000):
    initialize_schema()
    inserted = skipped = 0
    with SessionLocal() as db:
        for raw in rows:
            if inserted >= limit:
                break
            parsed = normalize_review(raw)
            if parsed is None:
                skipped += 1
                continue
            who, pid, at, event_id = parsed
            if db.get(Product, pid) is None:
                skipped += 1
                continue
            result = db.execute(insert_on_conflict(Event).values(
                id=event_id, visitor=who, product_id=pid, event_type='review', at=at,
                request_id='amazon-2023', position=-1, idempotency_key=None,
            ).on_conflict_do_nothing(index_elements=['id']))
            if not result.rowcount:
                skipped += 1
                continue
            feedback = {'FeedbackType': 'review', 'UserId': who, 'ItemId': pid, 'Value': 1.0,
                        'Timestamp': at.isoformat()}
            db.add(Outbox(id=str(uuid.uuid4()), kind='feedback', payload=json.dumps(feedback)))
            inserted += 1
            if inserted % 500 == 0:
                db.commit()
                print(f'Reviews imported: {inserted}, skipped: {skipped}', flush=True)
        db.commit()
    print(f'Done: {inserted} positive reviews imported; {skipped} skipped')
    return inserted


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', required=True, type=Path)
    ap.add_argument('--limit', type=int, default=100000)
    args = ap.parse_args()
    if args.limit < 1:
        ap.error('--limit must be >=1')
    import_reviews(stream(args.file), limit=args.limit)
