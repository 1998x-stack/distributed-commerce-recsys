"""Versioned, global-time, novel-item offline evaluation protocol.

No provider dependencies: saved predictions from Gorse, RecBole or any other
recommender are scored against the SAME eligible users, catalog and history.
This is logged-positive recall, not online CTR or an unbiased causal metric.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from math import log2, sqrt
from typing import Iterable
import json

PROTOCOL = 'global-temporal-novel-item-v1'


def utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


@dataclass
class Split:
    cutoff: datetime
    train: dict[str, list[str]]
    consumed: dict[str, set[str]]
    catalog: set[str]
    targets: dict[str, str]
    train_events: list[tuple[str, str, datetime]]
    fingerprint: str


def prepare_split(events: Iterable[tuple[str, str, datetime]], *, cutoff: datetime | None = None,
                  max_history: int = 50, min_history: int = 2) -> Split | None:
    if max_history < 2 or min_history < 1:
        raise ValueError('max_history must be >=2 and min_history >=1')
    rows = sorted(((str(u), str(p), utc(at)) for u, p, at in events),
                  key=lambda r: (r[2], r[0], r[1]))
    if not rows:
        return None
    cutoff = utc(cutoff) if cutoff else rows[max(0, int(len(rows) * .8) - 1)][2]
    consumed = defaultdict(set)
    history = defaultdict(list)
    future = defaultdict(list)
    train_events = []
    digest = sha256()
    for user, product, at in rows:
        digest.update(json.dumps([user, product, at.isoformat()], separators=(',', ':')).encode() + b'\n')
        if at <= cutoff:
            consumed[user].add(product)
            history[user].append(product)
            train_events.append((user, product, at))
        else:
            future[user].append(product)
    catalog = set().union(*consumed.values()) if consumed else set()
    # Retain recent unique interactions, but use ALL training positives to
    # identify truly new targets and prevent repeat-item leakage.
    recent = {user: list(reversed(list(dict.fromkeys(reversed(items)))[:max_history]))
              for user, items in history.items()}
    targets = {}
    for user, after in future.items():
        if len(consumed[user]) < min_history:
            continue
        target = next((p for p in after if p in catalog and p not in consumed[user]), None)
        if target is not None:
            targets[user] = target
    return Split(cutoff, recent, dict(consumed), catalog, targets, train_events, digest.hexdigest())


def rankings(split: Split, k: int, *, max_neighbors: int = 100) -> dict[str, dict[str, list[str]]]:
    if k < 1 or max_neighbors < 1:
        raise ValueError('k and max_neighbors must be >=1')
    # Distinct users prevent repeated bot clicks from inflating popularity.
    popularity = Counter(p for products in split.consumed.values() for p in products)
    neighbors = defaultdict(Counter)
    for hist in split.train.values():
        for pos, item in enumerate(hist):
            for other in hist[pos + 1:]:
                neighbors[item][other] += 1
                neighbors[other][item] += 1
    # Memory cap for highly popular items, deterministic ties.
    neighbors = {p: dict(sorted(v.items(), key=lambda pair: (-pair[1], pair[0]))[:max_neighbors])
                 for p, v in neighbors.items()}
    result = {'popularity': {}, 'itemcf': {}}
    for user in sorted(split.targets):
        seen = split.consumed[user]
        candidates = split.catalog - seen
        base = sorted(candidates, key=lambda p: (-popularity[p], p))
        result['popularity'][user] = base[:k]
        scores = defaultdict(float)
        for p in split.train[user]:
            for candidate, cooccurrences in neighbors.get(p, {}).items():
                if candidate in candidates:
                    scores[candidate] += cooccurrences / sqrt(popularity[p] * popularity[candidate])
        result['itemcf'][user] = sorted(candidates, key=lambda p: (-scores[p], -popularity[p], p))[:k]
    return result


def score(split: Split, predictions: dict[str, list[str]], k: int, *, strict: bool = True) -> dict:
    if k < 1:
        raise ValueError('k must be >=1')
    unexpected = set(predictions) - set(split.targets)
    if strict and unexpected:
        raise ValueError('Predictions contain unrecognized users')
    hits = dcg = submitted = 0
    for user, truth in split.targets.items():
        items = predictions.get(user, [])
        if not isinstance(items, list) or any(not isinstance(p, str) for p in items):
            raise ValueError(f'Invalid prediction list for user {user}')
        if len(items) != len(set(items)):
            raise ValueError(f'Duplicate products in prediction for user {user}')
        if strict and (set(items) - (split.catalog - split.consumed[user])):
            raise ValueError(f'Candidate pool violation for user {user}')
        submitted += bool(items)
        top = items[:k]
        if truth in top:
            hits += 1
            dcg += 1 / log2(top.index(truth) + 2)
    total = len(split.targets)
    return {'users': total, 'submitted_users': submitted,
            f'Recall@{k}': round(hits / total, 5) if total else None,
            f'NDCG@{k}': round(dcg / total, 5) if total else None,
            'coverage': round(submitted / total, 5) if total else None}


def compare(split: Split, k: int) -> dict:
    methods = rankings(split, k)
    return {'protocol': PROTOCOL, 'cutoff': split.cutoff.isoformat(),
            'fingerprint': split.fingerprint, 'train_products': len(split.catalog),
            'users': len(split.targets),
            'results': {name: score(split, preds, k) for name, preds in methods.items()}}
