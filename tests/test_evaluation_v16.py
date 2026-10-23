"""Saved offline protocol: enforce one train cutoff, catalog and scoring contract."""
from datetime import datetime, timedelta, timezone
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ml'))
from evaluation_protocol import prepare_split, rankings, score, compare
from evaluation_cli import save_snapshot, load_snapshot, load_predictions


def test_future_interactions_excluded_and_repeat_history_not_target():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        ('alice','A',t), ('alice','B',t+timedelta(seconds=1)),
        ('bob','C',t), ('bob','B',t+timedelta(seconds=1)),
        ('alice','A',t+timedelta(seconds=4)), # repeated target ignored
        ('alice','C',t+timedelta(seconds=5)), # first novel target
        ('bob','Z',t+timedelta(seconds=6)),   # post-cut new product not in catalog
    ]
    split = prepare_split(rows, cutoff=t + timedelta(seconds=2))
    assert split.catalog == {'A','B','C'}
    assert split.targets == {'alice':'C'}
    assert 'Z' not in split.catalog
    assert all(at <= split.cutoff for _,_,at in split.train_events)
    assert compare(split, k=10)['results']['popularity']['Recall@10'] == 1.0


def test_saved_snapshot_external_predictions_share_candidate_pool(tmp_path):
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [('alice','A',t), ('alice','B',t+timedelta(seconds=1)),
            ('bob','C',t), ('bob','B',t+timedelta(seconds=1)),
            ('alice','C',t+timedelta(seconds=2))]
    split = prepare_split(rows, cutoff=t + timedelta(seconds=1))
    dest = tmp_path / 'snapshot'
    save_snapshot(dest, split, k=5)
    with pytest.raises(FileExistsError):
        save_snapshot(dest, split, k=5)
    restored, k = load_snapshot(dest)
    pred = load_predictions(dest / 'popularity.jsonl')
    assert score(restored, pred, k) == score(split, pred, k)
    assert 'C' not in (dest / 'train.inter').read_text().splitlines()[-1]  # latest training event is B
    assert all('alice\tC\t' not in line for line in (dest / 'train.inter').read_text().splitlines())
    assert json.loads((dest / 'manifest.json').read_text())['protocol'].endswith('-v1')
    assert score(restored, {}, k)['coverage'] == 0


def test_candidate_pool_validation_and_duplicates():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s = prepare_split([('a','x',t),('a','y',t),('b','z',t),('b','y',t),
                       ('a','z',t+timedelta(seconds=1))], cutoff=t)
    with pytest.raises(ValueError, match='Candidate pool'):
        score(s, {'a':['x']}, k=3)
    with pytest.raises(ValueError, match='Duplicate products'):
        score(s, {'a':['z','z']}, k=3)
    with pytest.raises(ValueError, match='unrecognized users'):
        score(s, {'fake':['z']}, k=3)


def test_model_ids_and_unordered_ties_are_deterministic():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [('x','a',t),('x','b',t),('y','b',t),('y','c',t),('x','c',t+timedelta(seconds=1))]
    s = prepare_split(rows, cutoff=t)
    assert rankings(s, k=4) == rankings(prepare_split(reversed(rows), cutoff=t), k=4)
