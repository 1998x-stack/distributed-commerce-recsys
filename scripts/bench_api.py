"""Bounded read-only LAN API latency benchmark (not an arbitrary-target load tool).

Example: python scripts/bench_api.py --url http://192.168.1.103:8000 --path '/api/search?q=headphones'
Only localhost and literal private/loopback IPs are accepted. Max 2,000
GETs at concurrency <= 32; no mutations, credential headers or redirects.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
from math import ceil
import ipaddress
import json
import time
from urllib.parse import urlsplit

import httpx


def validate_target(url: str, path: str):
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        raise ValueError('Use a plain http://LAN-IP:port or localhost base URL')
    if parsed.hostname != 'localhost':
        try:
            host = ipaddress.ip_address(parsed.hostname or '')
        except ValueError as exc:
            raise ValueError('Use a literal private/loopback IP or localhost') from exc
        if not (host.is_private or host.is_loopback) or host.is_link_local or host.is_multicast:
            raise ValueError('Public/multicast/link-local targets are not allowed')
    if not path.startswith(('/api/products', '/api/search', '/api/recommendations', '/health/')) or '://' in path:
        raise ValueError('Only read-only API paths supported')
    if '\n' in path or '\r' in path or path.startswith('//'):
        raise ValueError('Invalid path')


def percentile(values: list[float], pct: int):
    if not values:
        return None
    sorted_values = sorted(values)
    return round(sorted_values[max(0, ceil(len(values) * pct / 100) - 1)], 2)


async def benchmark(url: str, path: str, requests: int = 200, concurrency: int = 8, *,
                    transport: httpx.AsyncBaseTransport | None = None):
    validate_target(url, path)
    if not 1 <= requests <= 2000 or not 1 <= concurrency <= 32:
        raise ValueError('requests must be 1..2000 and concurrency 1..32')
    semaphore = asyncio.Semaphore(concurrency)
    timings = []
    statuses = Counter()
    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=url, timeout=httpx.Timeout(10, connect=2),
                                 transport=transport, follow_redirects=False, trust_env=False) as client:
        async def worker():
            async with semaphore:
                begin = time.perf_counter()
                try:
                    response = await client.get(path)
                    status = str(response.status_code)
                except httpx.HTTPError:
                    status = 'network_error'
                elapsed = (time.perf_counter() - begin) * 1000
                statuses[status] += 1
                timings.append(elapsed)
        await asyncio.gather(*(worker() for _ in range(requests)))
    duration = time.perf_counter() - started
    success = sum(v for s,v in statuses.items() if s.startswith('2'))
    return {'url': url.rstrip('/'), 'path': path, 'requests': requests,
            'concurrency': concurrency, 'success': success,
            'failures': requests - success, 'statuses': dict(sorted(statuses.items())),
            'p50_ms': percentile(timings, 50), 'p95_ms': percentile(timings, 95),
            'p99_ms': percentile(timings, 99), 'rps': round(requests/max(duration, 1e-6), 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--path', default='/api/products?limit=24')
    parser.add_argument('--requests', type=int, default=200)
    parser.add_argument('--concurrency', type=int, default=8)
    args = parser.parse_args()
    try:
        result = asyncio.run(benchmark(args.url, args.path, args.requests, args.concurrency))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
