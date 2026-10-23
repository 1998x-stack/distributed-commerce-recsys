"""External model score adapter must preserve evaluation candidate boundaries."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ml'))
from evaluation_protocol import prepare_split, score
from evaluation_cli import save_snapshot, load_snapshot
from adapt_predictions import adapt_rows


def snapshot(tmp_path):
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        ('a', 'one', t), ('a', 'four', t), ('b', 'two', t),
        ('b', 'five', t), ('c', 'three', t),
        ('a', 'two', t + timedelta(days=2)),
        ('b', 'three', t + timedelta(days=2)),
    ]
    split = prepare_split(events, cutoff=t + timedelta(days=1))
    assert split.targets == {'a': 'two', 'b': 'three'}
    output = tmp_path / 'snapshot'
    save_snapshot(output, split, 2)
    return output


def test_adapt_scores_deterministic_and_scorable(tmp_path):
    path = snapshot(tmp_path)
    scores = tmp_path / 'model.tsv'
    scores.write_text('user_id\titem_id\tscore\na\tthree\t0.3\na\ttwo\t0.8\nb\tthree\t1.0\nb\tone\t0.4\n')
    ranked, count = adapt_rows(path, scores)
    assert ranked == {'a': ['two', 'three'], 'b': ['three', 'one']}
    assert count == 4
    split, k = load_snapshot(path)
    assert score(split, ranked, k)['Recall@2'] == 1.0


@pytest.mark.parametrize('body', [
    'user_id\titem_id\tscore\na\tone\t1\n',  # training item
    'user_id\titem_id\tscore\nz\ttwo\t1\n',  # unknown user
    'user_id\titem_id\tscore\na\ttwo\tnan\n',  # unsafe float
    'user_id\titem_id\tscore\na\ttwo\t1\na\ttwo\t2\n',
    'user_id\titem_id\tscore\na\ttwo\t1\na\tunknown\t2\n',
    'user_id\titem_id\tscore\na\ttwo\t1\textra\n',
])
def test_adapter_refuses_invalid_candidate_rows(tmp_path, body):
    path = snapshot(tmp_path)
    file = tmp_path / 'wrong.tsv'
    file.write_text(body)
    with pytest.raises(ValueError):
        adapt_rows(path, file)
