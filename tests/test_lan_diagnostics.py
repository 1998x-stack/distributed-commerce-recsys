"""LAN probe safety and deterministic result formatting."""
import importlib.util
from pathlib import Path
import argparse
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lan_diagnostics', ROOT / 'scripts/diagnose_lan.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

@pytest.mark.parametrize('value', ['example.com','8.8.8.8','169.254.1.2','2001:db8::1','not-an-ip'])
def test_reject_external_endpoints(value):
    with pytest.raises(argparse.ArgumentTypeError):
        module.private_ip(value)

@pytest.mark.parametrize('value', ['127.0.0.1','10.4.5.6','192.168.1.10','172.16.8.7'])
def test_accept_private_endpoints(value):
    assert module.private_ip(value) == value


def test_probes_distinguish_failures(monkeypatch):
    monkeypatch.setattr(module, 'check_http', lambda name, *args: {'name': name, 'ok': True})
    monkeypatch.setattr(module, 'check_tcp', lambda name, *args: {'name': name, 'ok': False})
    checks = module.diagnose('127.0.0.1','192.168.1.1','192.168.1.2')
    assert len(checks) == 4 and [x['ok'] for x in checks] == [True, True, False, False]
