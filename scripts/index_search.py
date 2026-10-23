"""Streaming OpenSearch catalog index build with atomic alias promotion.

The index is built without touching the live alias. On successful bulk indexing,
promote the new generation with one _aliases operation. First deployment also
supports replacing the old V1 physical 'commerce-products' index atomically.
"""
import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
from app import Product, SessionLocal  # noqa: E402

ALIAS = 'commerce-products'
MAPPING = {'settings': {'index': {'number_of_shards': 1, 'number_of_replicas': 0}},
           'mappings': {'properties': {
               'title': {'type': 'text'}, 'description': {'type': 'text'},
               'brand': {'type': 'text'}, 'category': {'type': 'keyword'},
               'price': {'type': 'float'}, 'rating': {'type': 'float'},
               'source': {'type': 'keyword'}}}}


def index_catalog(base: str, batch_size: int = 250):
    generation = f'{ALIAS}-v{uuid.uuid4().hex[:12]}'
    count = 0
    attempted_promotion = False
    with httpx.Client(timeout=60.0) as client:
        reply = client.put(f'{base}/{generation}', json=MAPPING)
        reply.raise_for_status()
        try:
            with SessionLocal() as db:
                result = db.scalars(select(Product).order_by(Product.id).execution_options(yield_per=batch_size))
                buffer = []
                for p in result:
                    buffer.append({'id': p.id, **{key: getattr(p, key) for key in ('title', 'description', 'brand', 'category', 'price', 'rating', 'source')}})
                    if len(buffer) >= batch_size:
                        write_bulk(client, base, generation, buffer)
                        count += len(buffer)
                        buffer.clear()
                if buffer:
                    write_bulk(client, base, generation, buffer)
                    count += len(buffer)
            client.post(f'{base}/{generation}/_refresh').raise_for_status()
            # Safe promotion: do not delete the live index until indexing succeeded.
            aliases = client.get(f'{base}/_alias/{ALIAS}')
            actions = []
            if aliases.status_code == 200:
                for name in aliases.json():
                    actions.append({'remove': {'index': name, 'alias': ALIAS}})
            elif aliases.status_code != 404:
                aliases.raise_for_status()
            legacy = client.head(f'{base}/{ALIAS}')
            if legacy.status_code == 200 and not actions:
                # Old V1 direct index; OpenSearch supports remove_index + add in one action.
                actions.append({'remove_index': {'index': ALIAS}})
            elif legacy.status_code not in (200, 404):
                legacy.raise_for_status()
            actions.append({'add': {'index': generation, 'alias': ALIAS}})
            attempted_promotion = True
            response = client.post(f'{base}/_aliases', json={'actions': actions})
            response.raise_for_status()
            if response.json().get('errors'):
                raise RuntimeError('Alias promotion returned errors')
            print(f'Indexed {count} products into {generation}; promoted {ALIAS}')
        except Exception:
            # A network failure during promotion is ambiguous; never delete an index
            # that might already have become the live alias target.
            if not attempted_promotion:
                client.delete(f'{base}/{generation}')
            raise


def write_bulk(client, base, generation, products):
    lines = []
    for data in products:
        pid = data.pop('id')
        lines.append(json.dumps({'index': {'_index': generation, '_id': pid}}))
        lines.append(json.dumps(data, ensure_ascii=False))
    reply = client.post(f'{base}/_bulk', content='\n'.join(lines) + '\n',
                        headers={'Content-Type': 'application/x-ndjson'})
    reply.raise_for_status()
    output = reply.json()
    if output.get('errors'):
        sample = next((item for item in output.get('items', []) if next(iter(item.values())).get('error')), None)
        raise RuntimeError(f'OpenSearch bulk error: {sample}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-size', type=int, default=250)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 2000:
        parser.error('--batch-size must be 1..2000')
    url = os.environ.get('OPENSEARCH_URL', '').rstrip('/')
    if not url:
        parser.error('Set OPENSEARCH_URL')
    index_catalog(url, args.batch_size)
