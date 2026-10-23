"""Global chronological evaluation never trains on events after the cutoff."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("offline_baselines", Path(__file__).resolve().parents[1] / "ml/offline_baselines.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
evaluate_events = mod.evaluate_events


def test_global_cut_prevents_future_information():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    data = [
        ('u1', 'a', t), ('u1', 'b', t + timedelta(seconds=1)),
        ('u2', 'c', t), ('u2', 'd', t + timedelta(seconds=1)),
        ('u2', 'e', t + timedelta(seconds=2)),
        ('u1', 'c', t + timedelta(seconds=3)),
        ('u2', 'new-unseen-after-cut', t + timedelta(seconds=4)),
    ]
    r = evaluate_events(data, k=4, cutoff=t + timedelta(seconds=2))
    assert r['train_products'] == 5
    assert r['users'] == 1
    assert r['results']['popularity']['users'] == 1


def test_repeat_item_not_considered_a_novel_target():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [('u1','x',t),('u1','y',t+timedelta(seconds=1)),
              ('u2','z',t),('u2','y',t+timedelta(seconds=1)),
              ('u1','x',t+timedelta(seconds=3)),('u1','z',t+timedelta(seconds=4))]
    r = evaluate_events(events, cutoff=t+timedelta(seconds=2))
    assert r['users'] == 1
    assert r['results']['popularity']['Recall@10'] == 1.0
