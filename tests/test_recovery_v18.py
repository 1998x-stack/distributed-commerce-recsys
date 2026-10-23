"""LAN recovery observation records transitions without modifying remote nodes."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from probe_recovery import observe


def test_recovery_transitions_and_bounded_sampling():
    state = iter([False, False, True, True, False])
    current = [0.0]
    sleeps = []
    def check():
        return [{'name': 'test', 'ok': next(state)}]
    def clock():
        return current[0]
    def sleep(delay):
        sleeps.append(delay)
        current[0] += delay
    rows = list(observe(check, samples=5, interval=2.0, clock=clock, sleep=sleep))
    assert [r['transition'] for r in rows] == ['initial', 'unchanged', 'recovered', 'unchanged', 'degraded']
    assert sleeps == [2.0] * 4
    assert [r['sample'] for r in rows] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize('samples,interval', [(0, 1), (3601, 1), (5, .5), (5, 61)])
def test_invalid_sampling_bounds(samples, interval):
    with pytest.raises(ValueError):
        list(observe(lambda: [], samples=samples, interval=interval))
