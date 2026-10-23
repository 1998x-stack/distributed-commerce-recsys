"""Offline item-retrieval baselines with a global temporal, novel-item split.

For reproducible saved snapshots and model comparisons use evaluation_cli.py.
"""
from __future__ import annotations
from datetime import datetime
import sys
from pathlib import Path
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
from app import Event, SessionLocal  # noqa: E402
from evaluation_protocol import PROTOCOL, compare, prepare_split


def evaluate_events(events: list[tuple[str, str, datetime]], k: int = 10,
                    cutoff: datetime | None = None, max_history: int = 50) -> dict:
    if k < 1 or max_history < 2:
        raise ValueError('k must be >=1 and max_history >=2')
    split = prepare_split(events, cutoff=cutoff, max_history=max_history)
    if split is None:
        return {'protocol': PROTOCOL, 'users': 0, 'results': {}}
    return compare(split, k)


def evaluate(history: dict[str, list[str]], k: int = 10):
    """Legacy convenience adapter; pseudo-time is NOT real chronological data."""
    from datetime import timezone, timedelta
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    events = [(user, pid, start + timedelta(seconds=pos))
              for user, items in history.items() for pos, pid in enumerate(items)]
    return evaluate_events(events, k=k)


if __name__ == '__main__':
    with SessionLocal() as db:
        events = [(ev.visitor, ev.product_id, ev.at) for ev in db.scalars(
            select(Event).where(Event.event_type.in_(['click', 'cart', 'favorite', 'purchase_demo', 'review']))
            .order_by(Event.at, Event.id))]
    print(evaluate_events(events))
