"""Export train-only interactions from an immutable V1.6 evaluation snapshot.

Unlike the deprecated direct-db export, this never includes held-out future
labels. RecBole has its OWN internal train/validation policy; score exported
model predictions with evaluation_cli.py for actual apples-to-apples metrics.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil
from evaluation_cli import load_snapshot

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'ml/data/commerce/commerce.inter')
    args = parser.parse_args(argv)
    split, _ = load_snapshot(args.snapshot)
    if args.output.resolve() == (args.snapshot / 'train.inter').resolve():
        parser.error('Cannot overwrite immutable snapshot train.inter')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.snapshot / 'train.inter', args.output)
    print(f'Exported pre-cutoff training interactions to {args.output} (cutoff={split.cutoff.isoformat()})')


if __name__ == '__main__':
    main()
