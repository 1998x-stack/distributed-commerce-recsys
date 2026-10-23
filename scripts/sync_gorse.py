"""Run on macOS or Ubuntu B against admin API to drain durable event/item outbox."""
import os
import time
import httpx

api = os.getenv('API_URL', 'http://127.0.0.1:8000').rstrip('/')
key = os.environ.get('ADMIN_KEY', '')
if not key:
    raise SystemExit('ADMIN_KEY is required')
with httpx.Client(timeout=30) as client:
    while True:
        delay = 30
        try:
            response = client.post(api + '/api/admin/sync', params={'batch': 500}, headers={'X-Admin-Key': key})
            response.raise_for_status()
            result = response.json()
            print(result, flush=True)
            # Drain healthy backlogs quickly, but pause when every row failed or leased.
            if result.get('claimed', 0) > 0 and result.get('delivered', 0) > 0:
                delay = 0.5
        except httpx.HTTPError as exc:
            print('Sync temporarily unavailable:', exc, flush=True)
        time.sleep(delay)
