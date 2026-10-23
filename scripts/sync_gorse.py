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
        try:
            response = client.post(api + '/api/admin/sync', headers={'X-Admin-Key': key})
            response.raise_for_status()
            print(response.json(), flush=True)
        except httpx.HTTPError as exc:
            print('Sync temporarily unavailable:', exc, flush=True)
        time.sleep(30)
