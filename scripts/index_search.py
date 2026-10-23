"""Idempotent OpenSearch product index rebuild from business DB.

PYTHONPATH=services/api OPENSEARCH_URL=http://ubuntu-a:9200 python scripts/index_search.py
"""
import json
import os
import sys
from pathlib import Path
import httpx
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
from app import Product, SessionLocal  # noqa: E402

url = os.environ.get('OPENSEARCH_URL', '').rstrip('/')
if not url:
    raise SystemExit('Set OPENSEARCH_URL')
index = url + '/commerce-products'
with httpx.Client(timeout=30.0) as client:
    mapping = {'settings': {'index': {'number_of_shards': 1, 'number_of_replicas': 0}}, 'mappings': {'properties': {
        'title': {'type': 'text'}, 'description': {'type': 'text'}, 'brand': {'type': 'text'},
        'category': {'type': 'text', 'fields': {'keyword': {'type': 'keyword'}}},
        'price': {'type': 'float'}, 'rating': {'type': 'float'}, 'source': {'type': 'keyword'}}}}
    if client.head(index).status_code == 404:
        r = client.put(index, json=mapping)
        r.raise_for_status()
    with SessionLocal() as db:
        products = db.scalars(select(Product)).all()
        for i in range(0, len(products), 250):
            lines = []
            for p in products[i:i + 250]:
                lines.append(json.dumps({'index': {'_index': 'commerce-products', '_id': p.id}}))
                lines.append(json.dumps({k: getattr(p, k) for k in ('title', 'description', 'brand', 'category', 'price', 'rating', 'source')}, ensure_ascii=False))
            r = client.post(url + '/_bulk', content='\n'.join(lines) + '\n', headers={'Content-Type': 'application/x-ndjson'})
            r.raise_for_status()
            if r.json().get('errors'):
                raise RuntimeError('OpenSearch bulk reported item errors')
            print(f'Indexed {min(i + 250, len(products))}/{len(products)}')
    client.post(index + '/_refresh').raise_for_status()
