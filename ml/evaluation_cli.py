"""Reproducible offline protocol and provider-neutral prediction scoring.

Prepare: python ml/evaluation_cli.py prepare --output ml/data/eval-v1 --cutoff 2026-01-01T00:00:00Z
Score:   python ml/evaluation_cli.py score --snapshot ml/data/eval-v1 --name recbole --predictions prediction.jsonl

Prediction JSONL: {"user_id":"...", "items":["product_id", ...]}.
For model comparisons, only consume train.inter and the catalog; never train
on targets.jsonl (which contains held-out labels).
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from hashlib import sha256
from pathlib import Path
import sys

from evaluation_protocol import PROTOCOL, Split, compare, prepare_split, rankings, score


def write_jsonl(path: Path, rows):
    with path.open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')


def load_predictions(path: Path) -> dict[str, list[str]]:
    result = {}
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            user = row['user_id']
            if not isinstance(user, str) or user in result or not isinstance(row['items'], list):
                raise ValueError('Invalid or duplicate user_id in prediction file')
            result[user] = row['items']
    return result


def save_snapshot(path: Path, split: Split, k: int):
    path.mkdir(parents=True, exist_ok=False)  # never silently overwrite a prior experiment
    manifest = {'protocol': PROTOCOL, 'cutoff': split.cutoff.isoformat(),
                'fingerprint': split.fingerprint, 'k': k,
                'users': len(split.targets), 'train_products': len(split.catalog),
                'train_events': len(split.train_events)}
    (path / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    (path / 'catalog.json').write_text(json.dumps(sorted(split.catalog)), encoding='utf-8')
    write_jsonl(path / 'targets.jsonl', (
        {'user_id': user, 'target': split.targets[user], 'seen': sorted(split.consumed[user])}
        for user in sorted(split.targets)))
    with (path / 'train.inter').open('w', encoding='utf-8') as dest:
        dest.write('user_id:token\titem_id:token\trating:float\ttimestamp:float\n')
        for user, product, at in split.train_events:
            dest.write(f'{user}\t{product}\t1.0\t{at.timestamp()}\n')
    for name, results in rankings(split, k).items():
        write_jsonl(path / (name + '.jsonl'),
                    ({'user_id': user, 'items': results[user]} for user in sorted(results)))
    (path / 'metrics.json').write_text(json.dumps(compare(split, k), indent=2), encoding='utf-8')
    manifest['checksums'] = {name: sha256((path / name).read_bytes()).hexdigest()
                             for name in ('catalog.json', 'targets.jsonl', 'train.inter',
                                          'popularity.jsonl', 'itemcf.jsonl', 'metrics.json')}
    (path / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def load_snapshot(path: Path) -> tuple[Split, int]:
    manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('protocol') != PROTOCOL:
        raise ValueError('Unsupported offline evaluation protocol version')
    for name, expected in manifest.get('checksums', {}).items():
        if name not in ('catalog.json', 'targets.jsonl', 'train.inter', 'popularity.jsonl', 'itemcf.jsonl', 'metrics.json'):
            raise ValueError('Unexpected snapshot checksum key')
        if sha256((path / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Snapshot modified or corrupted: {name}')
    if len(manifest.get('checksums', {})) != 6:
        raise ValueError('Snapshot checksums incomplete')
    catalog = set(json.loads((path / 'catalog.json').read_text(encoding='utf-8')))
    rows = [json.loads(line) for line in (path / 'targets.jsonl').read_text(encoding='utf-8').splitlines() if line]
    targets = {row['user_id']: row['target'] for row in rows}
    if len(targets) != len(rows) or len(targets) != manifest['users']:
        raise ValueError('Corrupted target file')
    consumed = {row['user_id']: set(row['seen']) for row in rows}
    if any(t not in catalog or t in consumed[user] for user, t in targets.items()):
        raise ValueError('Corrupted candidate pool')
    cutoff = datetime.fromisoformat(manifest['cutoff'])
    return Split(cutoff, {}, consumed, catalog, targets, [], manifest['fingerprint']), int(manifest['k'])


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--output', type=Path, required=True)
    prepare.add_argument('--cutoff', type=str, help='UTC ISO timestamp; omit for chronological 80%% cutoff')
    prepare.add_argument('--k', type=int, default=10)
    prepare.add_argument('--max-history', type=int, default=50)
    assess = sub.add_parser('score')
    assess.add_argument('--snapshot', type=Path, required=True)
    assess.add_argument('--name', required=True)
    assess.add_argument('--predictions', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        if args.k < 1:
            parser.error('--k must be positive')
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / 'services/api'))
        from sqlalchemy import select
        from app import Event, SessionLocal
        with SessionLocal() as db:
            events = [(ev.visitor, ev.product_id, ev.at) for ev in db.scalars(
                select(Event).where(Event.event_type.in_(['click', 'cart', 'favorite', 'purchase_demo', 'review']))
                .order_by(Event.at, Event.id))]
        cutoff = datetime.fromisoformat(args.cutoff.replace('Z', '+00:00')) if args.cutoff else None
        split = prepare_split(events, cutoff=cutoff, max_history=args.max_history)
        if split is None:
            parser.error('No positive events to evaluate')
        print(json.dumps(save_snapshot(args.output, split, args.k), indent=2))
    else:
        split, k = load_snapshot(args.snapshot)
        predictions = load_predictions(args.predictions)
        outcome = score(split, predictions, k)
        outcome.update({'model': args.name, 'protocol': PROTOCOL, 'fingerprint': split.fingerprint})
        print(json.dumps(outcome, indent=2))


if __name__ == '__main__':
    main()
