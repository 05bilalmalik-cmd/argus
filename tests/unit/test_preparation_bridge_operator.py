"""Local operator parser is not a machine transport or implicit app launcher."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def operator():
    try:
        return importlib.import_module('app.bridge.operator')
    except ModuleNotFoundError:
        pytest.fail('local preparation operator CLI is missing')


@pytest.mark.parametrize('args', [[], ['--data-dir', 'relative', 'clear-pause', '--confirm-clear-pause']])
def test_operator_requires_explicit_existing_absolute_root(args, capsys):
    assert operator().main(args) != 0
    assert json.loads(capsys.readouterr().out)['status'] == 'REFUSED'


def test_operator_does_not_create_a_missing_data_directory(tmp_path, capsys):
    root = tmp_path / 'missing'
    assert operator().main(['--data-dir', str(root), 'clear-pause', '--confirm-clear-pause']) != 0
    assert not root.exists()


def test_operator_exact_confirmation_precedes_core_access(tmp_path, monkeypatch, capsys):
    module = operator()
    calls = []
    monkeypatch.setattr(module, '_open_bridge', lambda *args: calls.append(args), raising=False)
    assert module.main(['--data-dir', str(tmp_path), 'approve', '--application-id', 'PRIVATE_SENTINEL', '--confirm-application-id', 'different']) != 0
    assert calls == []
    assert 'PRIVATE_SENTINEL' not in capsys.readouterr().out


def test_operator_methods_are_local_only_and_output_is_closed(tmp_path, monkeypatch, capsys):
    module = operator()
    calls = []
    bridge = SimpleNamespace(provision=lambda *args: calls.append(args), clear_pause=lambda: calls.append('clear'))
    monkeypatch.setattr(module, '_open_bridge', lambda *args: (bridge, SimpleNamespace(engine=SimpleNamespace(dispose=lambda: None))), raising=False)
    credential = tmp_path / 'credential.json'
    assert module.main(['--data-dir', str(tmp_path), 'provision', '--endpoint', 'http://127.0.0.1:9123', '--credential-file', str(credential), '--ttl-seconds', '300']) == 0
    assert calls == [(credential, 'http://127.0.0.1:9123', 300)]
    assert json.loads(capsys.readouterr().out) == {'status': 'PROVISIONED'}
    assert module.main(['--data-dir', str(tmp_path), 'clear-pause', '--confirm-clear-pause']) == 0
    assert calls[-1] == 'clear'


def test_operator_failure_never_prints_raw_error(tmp_path, monkeypatch, capsys):
    module = operator()
    def fail(*args):
        raise RuntimeError('PRIVATE_SENTINEL')
    monkeypatch.setattr(module, '_open_bridge', fail, raising=False)
    assert module.main(['--data-dir', str(tmp_path), 'clear-pause', '--confirm-clear-pause']) == 1
    assert capsys.readouterr().out.strip() == '{"status": "REFUSED"}'
