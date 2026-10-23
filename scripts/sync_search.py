"""Sync durable product changes from the Mac API to OpenSearch through its alias."""
import os
import time
import httpx

api = os.environ.get('API_URL', 'http://127.0.0.1:8000').rstrip('/')
key = os.environ.get('ADMIN_KEY', '')
if not key:
    raise SystemExit('ADMIN_KEY is required')
with httpx.Client(timeout=40, trust_env=False) as client:
    while True:
        delay = 10
        try:
            response = client.post(api + '/api/admin/search/sync', params={'batch': 500},
                                   headers={'X-Admin-Key': key})
            response.raise_for_status()
            data = response.json()
            print({'search_sync': data}, flush=True)
            if data.get('delivered', 0):
                delay = 0.5
        except httpx.HTTPError as exc:
            print('Search sync temporarily unavailable:', exc, flush=True)
        time.sleep(delay)
