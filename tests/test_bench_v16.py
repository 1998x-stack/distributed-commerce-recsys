import asyncio
import importlib.util
from pathlib import Path
import pytest
import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bench_api', ROOT/'scripts/bench_api.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def test_bench_private_targets_and_bounded_requests():
    for bad in ('https://example.com', 'http://8.8.8.8', 'http://example.com',
                'http://192.168.1.10/api/admin', 'http://127.0.0.1@8.8.8.8'):
        with pytest.raises(ValueError):
            bench.validate_target(bad, '/api/search?q=hi')
    with pytest.raises(ValueError):
        bench.validate_target('http://127.0.0.1:8000', '/api/admin/sync')
    bench.validate_target('http://192.168.0.7:8000', '/api/products')
    bench.validate_target('http://localhost:8000', '/api/search?q=hi')


def test_bench_local_mock_latency_and_errors():
    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200 if request.url.path == '/api/products' else 503))
        return await bench.benchmark('http://localhost:8000','/api/products?limit=1',
                                    requests=15, concurrency=3, transport=transport)
    outcome = asyncio.run(run())
    assert outcome['requests'] == outcome['success'] == 15
    assert outcome['p95_ms'] is not None and outcome['p99_ms'] >= outcome['p50_ms']
