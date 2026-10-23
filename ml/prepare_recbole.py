"""Export interaction events in RecBole atomic .inter format.

RecBole is an OPTIONAL research dependency, not required to run the storefront.
Run: PYTHONPATH=services/api python ml/prepare_recbole.py
"""
import argparse
import sys
from datetime import timezone
from pathlib import Path
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
from app import Event, SessionLocal  # noqa: E402


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, default=ROOT / 'ml/data/commerce/commerce.inter')
    args = ap.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    weights = {'click': 1., 'favorite': 3., 'cart': 5., 'purchase_demo': 8.}
    count = 0
    with SessionLocal() as db, args.output.open('w') as out:
        out.write('user_id:token\titem_id:token\trating:float\ttimestamp:float\n')
        for ev in db.scalars(select(Event).order_by(Event.at, Event.id)):
            if ev.event_type not in weights:
                continue
            dt = ev.at.replace(tzinfo=timezone.utc) if ev.at.tzinfo is None else ev.at
            out.write(f'{ev.visitor}\t{ev.product_id}\t{weights[ev.event_type]}\t{dt.timestamp()}\n')
            count += 1
    print(f'Wrote {count} records to {args.output}')
