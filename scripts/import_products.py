"""Import Amazon Reviews 2023 metadata JSONL/JSONL.GZ or seed a synthetic demo catalog.

Usage: PYTHONPATH=services/api python scripts/import_products.py --demo
       PYTHONPATH=services/api python scripts/import_products.py --file meta_Electronics.jsonl.gz --limit 10000
"""
import argparse
import os
import gzip
import math
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/api'))
from app import Product, SessionLocal, initialize_schema, check_schema_revision, queue_catalog_updates  # noqa: E402

DEMO = [
 ('Wireless Studio Headphones', 'Audio', 'Novus', 79.95), ('Compact Bluetooth Earbuds', 'Audio', 'Sonora', 49.90),
 ('Noise Cancelling Headphones', 'Audio', 'Acousta', 159.00), ('Portable Speaker', 'Audio', 'Sonora', 64.00),
 ('Mechanical Keyboard', 'Computers', 'Keycraft', 89.00), ('Wireless Ergonomic Mouse', 'Computers', 'Mono', 35.00),
 ('14-inch Laptop Sleeve', 'Computers', 'Orbit', 24.90), ('Ultra Wide Monitor', 'Computers', 'Vista', 289.00),
 ('Smart Fitness Watch', 'Electronics', 'Tempo', 129.00), ('USB-C Charging Hub', 'Electronics', 'Orbit', 55.00),
 ('Compact Digital Camera', 'Electronics', 'Vista', 249.00), ('Portable Power Bank', 'Electronics', 'Novo', 43.00),
 ('Minimal Desk Lamp', 'Home', 'Lumio', 58.00), ('Ceramic Coffee Mug', 'Home', 'Atelier', 19.00),
 ('Wooden Bookshelf', 'Home', 'Haven', 179.00), ('Air Purifier', 'Home', 'Breeze', 199.00),
 ('Everyday Running Shoes', 'Fashion', 'Stride', 94.00), ('Canvas Tote Bag', 'Fashion', 'Field', 29.00),
 ('Classic Cotton Hoodie', 'Fashion', 'North', 69.00), ('Travel Backpack', 'Fashion', 'Atlas', 119.00),
 ('Reusable Water Bottle', 'Outdoor', 'Glacier', 32.00), ('Hiking Daypack', 'Outdoor', 'Trail', 75.00),
 ('Camping Lantern', 'Outdoor', 'Lumio', 39.00), ('Yoga Exercise Mat', 'Outdoor', 'Stride', 27.00),
]


def parse_price(value):
    if isinstance(value, (int, float)):
        return float(max(0, value)) if math.isfinite(float(value)) else 0.0
    if not isinstance(value, str):
        return 0.0
    m = re.search(r'\d+(?:,\d{3})*(?:\.\d+)?', value)
    result = float(m.group().replace(',', '')) if m else 0.0
    return result if math.isfinite(result) else 0.0


def normalize(raw, index, demo=False):
    if demo:
        name, cat, brand, price = raw
        source_id = str(index)
        return Product(id=f'demo:{source_id}', source='demo', source_id=source_id, title=name,
                       description=f'{brand} {name}. Sample item for local demo; not offered for sale.',
                       category=cat, brand=brand, price=price, currency='USD', rating=4.3 + (index % 7) * .1, is_active=True)
    asin = raw.get('parent_asin') or raw.get('asin')
    if not asin or not raw.get('title'):
        return None
    cats = raw.get('categories') or []
    if isinstance(cats, str): cats = [cats]
    img = raw.get('images') or []
    url = next((x.get('large') or x.get('hi_res') or x.get('thumb') for x in img if isinstance(x, dict) and (x.get('large') or x.get('hi_res') or x.get('thumb'))), '')
    desc = raw.get('description') or ''
    if isinstance(desc, list): desc = ' '.join(str(x) for x in desc)
    if isinstance(desc, dict): desc = json.dumps(desc, ensure_ascii=False)
    rating = raw.get('average_rating') or 0
    return Product(id='amazon:' + str(asin), source='amazon-2023', source_id=str(asin),
                   title=str(raw['title'])[:500], description=str(desc)[:10000],
                   category=str(cats[0] if cats else (raw.get('main_category') or 'Other'))[:100], brand=str(raw.get('store') or '')[:100],
                   price=parse_price(raw.get('price')), currency='USD', image_url=str(url or ''),
                   rating=float(rating) if isinstance(rating, (int, float)) else 0,
                   rating_count=max(0, int(raw.get('rating_number') or 0)), is_active=True)


def import_rows(rows, limit, demo=False):
    if os.getenv('SCHEMA_MODE') == 'external':
        check_schema_revision()
    else:
        initialize_schema()
    inserted = 0
    with SessionLocal() as db:
        for i, raw in enumerate(rows):
            if inserted >= limit:
                break
            product = normalize(raw, i, demo)
            if product is None:
                continue
            # A routine feed refresh must not undo an explicit administrator takedown.
            # Restoring a withdrawn item requires the authenticated availability API.
            existing = db.get(Product, product.id)
            if existing is not None:
                product.is_active = existing.is_active
            managed = db.merge(product)
            db.flush()
            queue_catalog_updates(db, managed)
            inserted += 1
            if inserted % 200 == 0:
                db.commit()
                print(f'Imported {inserted}', flush=True)
        db.commit()
    print(f'Done: {inserted} products; sync Gorse /api/admin/sync and search /api/admin/search/sync')


def stream(path):
    reader = gzip.open if path.suffix == '.gz' else open
    with reader(path, 'rt', encoding='utf-8') as fp:
        for line in fp:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', type=Path)
    ap.add_argument('--limit', type=int, default=10000)
    ap.add_argument('--demo', action='store_true')
    args = ap.parse_args()
    if not args.demo and not args.file:
        ap.error('Pass --demo or --file')
    import_rows(DEMO if args.demo else stream(args.file), args.limit, args.demo)
