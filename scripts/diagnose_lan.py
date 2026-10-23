"""Read-only LAN tri-node service diagnostics. No credentials or mutations.

Usage: python scripts/diagnose_lan.py --mac 192.168.1.20 --ubuntu-a 192.168.1.21 --ubuntu-b 192.168.1.22
Returns 1 if any required service cannot be reached (not a full readiness proof).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


def private_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Use a literal private LAN IP, not a hostname') from exc
    allowed = tuple(ipaddress.ip_network(net) for net in
                    ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8'))
    if address.version != 4 or not any(address in network for network in allowed):
        raise argparse.ArgumentTypeError('Only RFC1918/loopback IPv4 addresses are supported')
    return str(address)


def check_tcp(name: str, address: str, port: int, timeout: float = 2.0) -> dict:
    start = time.monotonic()
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return {'name': name, 'ok': True, 'latency_ms': round((time.monotonic()-start)*1000, 1)}
    except OSError as exc:
        return {'name': name, 'ok': False, 'error': type(exc).__name__}


def check_http(name: str, address: str, port: int, path: str, timeout: float = 3.0) -> dict:
    from urllib.request import Request
    start = time.monotonic()
    opener = build_opener(ProxyHandler({}))  # prevent a local HTTP proxy from leaking probes
    try:
        with opener.open(Request(f'http://{address}:{port}{path}', method='GET'), timeout=timeout) as resp:
            status = resp.status
            return {'name': name, 'ok': 200 <= status < 300,
                    'status': status, 'latency_ms': round((time.monotonic()-start)*1000, 1)}
    except HTTPError as exc:
        return {'name': name, 'ok': False, 'status': exc.code}
    except (OSError, URLError) as exc:
        return {'name': name, 'ok': False, 'error': type(exc).__name__}


def diagnose(mac: str, ubuntu_a: str, ubuntu_b: str) -> list[dict]:
    return [
        check_http('mac: storefront /api/health', mac, 3000, '/api/health'),
        check_http('ubuntu-a: OpenSearch', ubuntu_a, 9200, '/_cluster/health'),
        check_tcp('ubuntu-a: Gorse Master RPC', ubuntu_a, 8086),
        check_tcp('ubuntu-b: Gorse recommendation server', ubuntu_b, 8087),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('mac', 'ubuntu-a', 'ubuntu-b'):
        parser.add_argument('--' + arg, required=True, type=private_ip)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    results = diagnose(args.mac, args.ubuntu_a, args.ubuntu_b)
    if args.json:
        print(json.dumps({'checks': results, 'all_ok': all(r['ok'] for r in results)}, indent=2))
    else:
        for r in results:
            print(f"{'OK' if r['ok'] else 'FAIL'} {r['name']}: {r.get('latency_ms', r.get('status', r.get('error', 'unknown')))}")
    return 0 if all(r['ok'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
