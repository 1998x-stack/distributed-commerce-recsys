"""Time-aware offline popularity / cosine ItemCF evaluation.

The CLI uses one GLOBAL timestamp cutoff, not per-user leave-last-out.
The target is each eligible user's first NEW positive product after that cutoff:
repeat purchases/clicks of previously consumed items are not counted as novel-item targets.
This evaluates ranking of new items, not online CTR or causal lift.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime
from math import log2, sqrt
import sys
from pathlib import Path
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
from app import Event, SessionLocal  # noqa: E402


def evaluate_events(events: list[tuple[str, str, datetime]], k: int = 10,
                    cutoff: datetime | None = None, max_history: int = 100) -> dict:
    """Evaluate on a global time cut with a common train catalog/candidate pool.

    Input rows are (visitor, product_id, timestamp). Users must have >=2 distinct
    pre-cutoff positives; their target is the first post-cutoff *novel* product.
    """
    if k < 1 or max_history < 2:
        raise ValueError('k must be >=1 and max_history >=2')
    if not events:
        return {'protocol': 'global-temporal-novel-item', 'users': 0, 'results': {}}
    rows = sorted(events, key=lambda row: (row[2], row[0], row[1]))
    if cutoff is None:
        cutoff = rows[max(0, int(len(rows) * 0.8) - 1)][2]
    training: dict[str, list[str]] = defaultdict(list)
    tests: dict[str, list[str]] = defaultdict(list)
    for user, pid, at in rows:
        if at <= cutoff:
            training[user].append(pid)
        else:
            tests[user].append(pid)
    training = {user: list(dict.fromkeys(items[-max_history:]))
                for user, items in training.items()}
    train_items = set(pid for items in training.values() for pid in items)
    targets = {}
    for user, after in tests.items():
        before = set(training.get(user, []))
        if len(before) < 2:
            continue
        # Only evaluate targets that are already in the pre-cutoff item universe.
        target = next((pid for pid in after if pid not in before and pid in train_items), None)
        if target is not None:
            targets[user] = target
    popularity = Counter(pid for history in training.values() for pid in history)
    degree = Counter(pid for history in training.values() for pid in history)
    neighbors = defaultdict(Counter)
    for history in training.values():
        for i, item in enumerate(history):
            for other in history[i + 1:]:
                neighbors[item][other] += 1
                neighbors[other][item] += 1

    def rank(user, method):
        before = set(training[user])
        if method == 'popularity':
            scores = {pid: float(value) for pid, value in popularity.items()}
        else:
            scores = defaultdict(float)
            for seen in before:
                for candidate, count in neighbors.get(seen, {}).items():
                    scores[candidate] += count / sqrt(degree[seen] * degree[candidate])
        candidates = ((pid, value) for pid, value in scores.items()
                      if pid in train_items and pid not in before)
        return [pid for pid, _ in sorted(candidates, key=lambda pair: (-pair[1], pair[0]))[:k]]

    result = {}
    for method in ('popularity', 'itemcf'):
        hit = ndcg = 0.0
        for user, truth in targets.items():
            predicted = rank(user, method)
            if truth in predicted:
                hit += 1
                ndcg += 1 / log2(predicted.index(truth) + 2)
        n = len(targets)
        result[method] = {'users': n, f'Recall@{k}': round(hit / n, 5) if n else None,
                          f'NDCG@{k}': round(ndcg / n, 5) if n else None}
    return {'protocol': 'global-temporal-novel-item', 'cutoff': cutoff.isoformat(),
            'train_products': len(train_items), 'users': len(targets), 'results': result}


def evaluate(history: dict[str, list[str]], k: int = 10):
    """Legacy convenience adapter: position-based pseudo-time is NOT real temporal data."""
    from datetime import timezone, timedelta
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    events = [(user, pid, start + timedelta(seconds=pos))
              for user, items in history.items() for pos, pid in enumerate(items)]
    return evaluate_events(events, k=k)


if __name__ == '__main__':
    rows = []
    with SessionLocal() as db:
        for ev in db.scalars(select(Event).where(Event.event_type.in_(['click', 'cart', 'favorite', 'purchase_demo', 'review']))
                             .order_by(Event.at, Event.id)):
            if ev.at:
                rows.append((ev.visitor, ev.product_id, ev.at))
    print(evaluate_events(rows))
