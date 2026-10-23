"""Paged, read-only-by-default catalog anti-entropy audit.

Use --repair to enqueue current business state for Gorse and OpenSearch.
Run after initial full-index rebuild, then periodically from a private admin host.
"""
import argparse
import json
import os
import time

import httpx


def run(api: str, key: str, *, repair: bool, limit: int, max_pages: int):
    after = ''
    totals = {'checked': 0, 'drift': 0, 'requeued': 0, 'errors': 0}
    with httpx.Client(timeout=120, trust_env=False) as client:
        for _ in range(max_pages):
            response = client.post(api.rstrip('/') + '/api/admin/catalog/reconcile',
                                   params={'after': after, 'limit': limit, 'repair': str(repair).lower()},
                                   headers={'X-Admin-Key': key})
            response.raise_for_status()
            result = response.json()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            totals['checked'] += result['checked']
            totals['drift'] += len(result['drift'])
            totals['requeued'] += result['requeued']
            totals['errors'] += len(result['errors'])
            if not result['next_after'] or result['checked'] < limit:
                break
            after = result['next_after']
    print('TOTAL ' + json.dumps(totals), flush=True)
    return totals


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--api', default=os.getenv('API_URL', 'http://127.0.0.1:8000'))
    parser.add_argument('--repair', action='store_true')
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--max-pages', type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.limit <= 25 or args.max_pages < 1:
        parser.error('Limit must be 1..25 and max-pages must be positive')
    key = os.getenv('ADMIN_KEY')
    if not key:
        parser.error('Set ADMIN_KEY')
    result = run(args.api, key, repair=args.repair, limit=args.limit, max_pages=args.max_pages)
    if result['errors']:
        raise SystemExit(2)
