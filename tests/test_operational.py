"""Deterministic unit tests for process-local resilience primitives."""

import importlib.util
from pathlib import Path
import sys

import pytest

MODULE = Path(__file__).resolve().parents[1] / 'services/api/operational.py'
spec = importlib.util.spec_from_file_location('commerce_operations', MODULE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_circuit_opens_skips_and_recovers(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    breaker = module.CircuitBreaker(threshold=2, cooldown=10)
    assert breaker.allow('search')
    breaker.failure('search')
    assert breaker.allow('search')
    breaker.failure('search')
    assert not breaker.allow('search')
    assert breaker.status()['search']['open']
    clock[0] = 110.0
    assert breaker.allow('search')
    assert not breaker.allow('search')  # single half-open probe
    breaker.success('search')
    assert breaker.allow('search')
    assert not breaker.status()['search']['open']


def test_failed_half_open_probe_reopens(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    cb = module.CircuitBreaker(threshold=1, cooldown=15)
    cb.failure('gorse')
    clock[0] = 16
    assert cb.allow('gorse')
    cb.failure('gorse')
    assert not cb.allow('gorse')
    assert cb.status()['gorse']['retry_in_seconds'] == 15


def test_rate_buckets_weighted_and_refill(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    limiter = module.TokenBuckets(capacity=3, refill_per_second=1)
    assert limiter.allow('visitor:a', cost=2)
    assert not limiter.allow('visitor:a', cost=2)
    assert limiter.allow('visitor:b', cost=3)
    clock[0] = 1
    assert limiter.allow('visitor:a', cost=2)
    assert not limiter.allow('visitor:b', cost=2)


def test_rate_bucket_memory_is_bounded(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    limiter = module.TokenBuckets(capacity=10, refill_per_second=1, max_entries=2)
    for name in ('a', 'b', 'c'):
        assert limiter.allow(name)
    assert len(limiter._data) <= 2


def test_metric_counters_are_independent():
    metric = module.MetricsCounters()
    metric.increment('search_sql')
    metric.increment('search_sql')
    metric.increment('recommend_local')
    assert metric.snapshot() == {'search_sql': 2, 'recommend_local': 1}
