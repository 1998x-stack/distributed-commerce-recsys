"""Leakage-aware leave-last-positive-out baselines: popularity and item co-visitation.

The last event of each eligible user is held out. Training excludes all held-out
items/events. This is a small-research baseline, not unbiased online CTR.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from math import log2
import sys
from pathlib import Path
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
from app import Event, SessionLocal  # noqa: E402


def evaluate(history: dict[str, list[str]], k: int = 10):
    training, held_out = {}, {}
    for user, items in history.items():
        if len(items) >= 2:
            training[user], held_out[user] = items[:-1], items[-1]
    global_pop = Counter(pid for items in training.values() for pid in items)
    pair_counts = defaultdict(Counter)
    for items in training.values():
        unique = list(dict.fromkeys(items))
        for src in unique:
            for dst in unique:
                if src != dst:
                    pair_counts[src][dst] += 1
    results = {}
    for algorithm in ('popularity', 'itemcf'):
        recall, ndcg, count = 0, 0., 0
        for user, true_pid in held_out.items():
            seen = set(training[user])
            if algorithm == 'popularity':
                scores = global_pop
            else:
                scores = Counter()
                for pid in seen:
                    scores.update(pair_counts[pid])
                if not scores:
                    scores = global_pop
            ranked = [pid for pid, _ in scores.most_common() if pid not in seen][:k]
            if true_pid in ranked:
                recall += 1
                ndcg += 1 / log2(ranked.index(true_pid) + 2)
            count += 1
        results[algorithm] = {'users': count, f'Recall@{k}': round(recall / count, 5) if count else None,
                              f'NDCG@{k}': round(ndcg / count, 5) if count else None}
    return results


if __name__ == '__main__':
    by_user = defaultdict(list)
    with SessionLocal() as db:
        for ev in db.scalars(select(Event).where(Event.event_type.in_(['click', 'cart', 'favorite', 'purchase_demo'])).order_by(Event.at, Event.id)):
            by_user[ev.visitor].append(ev.product_id)
    print(evaluate(by_user))
