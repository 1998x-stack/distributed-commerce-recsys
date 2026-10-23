"""Read-only tri-node recovery observation for a *manual* LAN fault drill.

No services are stopped or restarted by this script. The operator controls the
experiment and should use a non-production lab environment. Every sample is
saved as JSONL, with a monotonic sample interval and explicit failure/recovery
transitions. Diagnostic probe URLs remain restricted to private LAN addresses.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from diagnose_lan import diagnose, private_ip


def observe(check, *, samples: int, interval: float, clock=time.monotonic, sleep=time.sleep):
    """Bounded generator, injectable for deterministic testing."""
    if not 1 <= samples <= 3600 or interval < 1 or interval > 60:
        raise ValueError('samples must be 1..3600 and interval 1..60 seconds')
    prior = None
    deadline = clock()
    for index in range(samples):
        results = check()
        healthy = all(x['ok'] for x in results)
        change = ('initial' if prior is None else
                  'recovered' if healthy and not prior else
                  'degraded' if not healthy and prior else 'unchanged')
        yield {'sample': index + 1, 'timestamp': datetime.now(timezone.utc).isoformat(),
               'all_ok': healthy, 'transition': change, 'checks': results}
        prior = healthy
        deadline += interval
        if index + 1 < samples:
            sleep(max(0, deadline - clock()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('mac', 'ubuntu-a', 'ubuntu-b'):
        parser.add_argument('--' + name, required=True, type=private_ip)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('Refusing to overwrite existing recovery report')
    if not 1 <= args.samples <= 3600 or not 1 <= args.interval <= 60:
        parser.error('samples must be 1..3600 and interval 1..60s')
    with args.output.open('x', encoding='utf-8') as dest:
        for sample in observe(lambda: diagnose(args.mac, args.ubuntu_a, args.ubuntu_b),
                              samples=args.samples, interval=args.interval):
            dest.write(json.dumps(sample) + '\n')
            dest.flush()
            print(f"{sample['sample']}: {'healthy' if sample['all_ok'] else 'degraded'} / {sample['transition']}", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
