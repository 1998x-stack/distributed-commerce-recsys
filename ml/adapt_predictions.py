"""Convert offline model scores to the shared V1.6 recommendation evaluation format.

Scores MUST come from models trained on the snapshot's train.inter only.
This tool never calls a live recommendation service (which would leak test data).

Example:
 python ml/adapt_predictions.py --snapshot ml/data/eval-v16 \
   --input recbole-scores.tsv --delimiter tab --output recbole.jsonl
 python ml/evaluation_cli.py score --snapshot ml/data/eval-v16 \
   --name recbole --predictions recbole.jsonl

Input columns default to user_id, item_id, score; one candidate per row.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
from pathlib import Path

from evaluation_cli import load_snapshot, write_jsonl


def adapt_rows(snapshot: Path, file: Path, *, delimiter: str = '\t',
               user_column: str = 'user_id', item_column: str = 'item_id',
               score_column: str = 'score') -> tuple[dict[str, list[str]], int]:
    """Validate candidates before generating deterministic per-user top-K rankings."""
    if delimiter not in ('\t', ','):
        raise ValueError('delimiter must be comma or tab')
    split, k = load_snapshot(snapshot)
    if not split.targets:
        raise ValueError('No evaluation users in the snapshot')
    by_user: dict[str, dict[str, float]] = {}
    count = 0
    with file.open(newline='', encoding='utf-8-sig') as source:
        reader = csv.DictReader(source, delimiter=delimiter)
        if not reader.fieldnames or not {user_column, item_column, score_column} <= set(reader.fieldnames):
            raise ValueError('Missing required score columns')
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError('Malformed score row: wrong number of columns')
            user, item = row[user_column], row[item_column]
            if user not in split.targets:
                raise ValueError(f'Unexpected user: {user}')
            if not item or item not in split.catalog or item in split.consumed[user]:
                raise ValueError(f'Candidate pool violation: {user}/{item}')
            try:
                value = float(row[score_column])
            except (TypeError, ValueError) as exc:
                raise ValueError('Score is not numeric') from exc
            if not math.isfinite(value):
                raise ValueError('Score must be finite')
            scores = by_user.setdefault(user, {})
            if item in scores:
                raise ValueError(f'Duplicate candidate: {user}/{item}')
            scores[item] = value
            count += 1
    ranked = {user: [item for item, _ in sorted(by_user.get(user, {}).items(),
                         key=lambda pair: (-pair[1], pair[0]))[:k]]
              for user in sorted(split.targets)}
    return ranked, count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--delimiter', choices=['tab', 'comma'], default='tab')
    parser.add_argument('--user-column', default='user_id')
    parser.add_argument('--item-column', default='item_id')
    parser.add_argument('--score-column', default='score')
    args = parser.parse_args(argv)
    if args.output.resolve() == args.input.resolve():
        parser.error('Output must not overwrite input scores')
    if args.output.exists():
        parser.error('Output file already exists; refusing overwrite')
    ranked, count = adapt_rows(args.snapshot, args.input,
                               delimiter='\t' if args.delimiter == 'tab' else ',',
                               user_column=args.user_column,
                               item_column=args.item_column, score_column=args.score_column)
    write_jsonl(args.output, ({'user_id': u, 'items': items} for u, items in ranked.items()))
    print(json.dumps({'evaluated_users': len(ranked), 'scored_candidates': count,
                      'predictions': str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
