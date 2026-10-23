"""Process-local guards for a single-node LAN API deployment.

For multi-replica deployment, replace the rate limiter with a shared Redis gate.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class CircuitState:
    failures: int = 0
    open_until: float = 0.0
    probe_active: bool = False


class CircuitBreaker:
    """A bounded, thread-safe circuit with one half-open probe."""

    def __init__(self, threshold: int = 3, cooldown: float = 15):
        self.threshold = threshold
        self.cooldown = cooldown
        self._states: dict[str, CircuitState] = {}
        self._lock = threading.Lock()

    def allow(self, name: str) -> bool:
        with self._lock:
            state = self._states.setdefault(name, CircuitState())
            if state.open_until:
                if time.monotonic() < state.open_until:
                    return False
                if state.probe_active:
                    return False
                state.probe_active = True
                return True
            return True

    def success(self, name: str) -> None:
        with self._lock:
            self._states[name] = CircuitState()

    def failure(self, name: str) -> None:
        with self._lock:
            state = self._states.setdefault(name, CircuitState())
            state.probe_active = False
            state.failures += 1
            if state.failures >= self.threshold:
                state.open_until = time.monotonic() + self.cooldown

    def status(self) -> dict[str, dict[str, object]]:
        with self._lock:
            now = time.monotonic()
            return {name: {'failures': state.failures,
                           'open': state.open_until > now,
                           'retry_in_seconds': max(0, round(state.open_until - now, 1))}
                    for name, state in self._states.items()}


class TokenBuckets:
    """Process-local weighted token buckets, bounded memory and monotonic time."""

    def __init__(self, capacity: float = 120, refill_per_second: float = 2, max_entries: int = 10000):
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.max_entries = max_entries
        self._data: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._data) >= self.max_entries:
                # Remove inactive buckets; on overflow, evict oldest. This is
                # best-effort local throttling, never a distributed security gate.
                stale = [k for k, (_, at) in self._data.items()
                         if now - at > self.capacity / self.refill_per_second]
                for k in stale:
                    self._data.pop(k, None)
                if len(self._data) >= self.max_entries:
                    self._data.pop(min(self._data, key=lambda k: self._data[k][1]))
            tokens, last_at = self._data.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + max(0, now - last_at) * self.refill_per_second)
            if tokens < cost:
                self._data[key] = (tokens, now)
                return False
            self._data[key] = (tokens - cost, now)
            return True


class MetricsCounters:
    """In-process transport/fallback diagnostics, reset on restart."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def increment(self, name: str):
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)
