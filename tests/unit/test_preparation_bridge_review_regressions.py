"""Adversarial regressions discovered after the first combined green run."""
import json

import pytest

from tests.unit.test_preparation_bridge_core import env, bridge, seed_application, successful_executor


def test_handoff_requires_operator_clear_even_after_browser_closes(env):
    app_id = seed_application(env)
    service = bridge(env, execute=successful_executor(env))
    service.approve(app_id, app_id, 60)
    service.approve(app_id, app_id, 60)
    first = service.dispatch('request_approved_preparation', {'idempotency_key': 'first-handoff'})
    assert first['status'] == 'PREFILLED_HANDOFF'
    env.nav.sessions.clear()
    assert service.dispatch('get_preparation_readiness', {})['status'] == 'PAUSED'
    assert service.dispatch('request_approved_preparation', {'idempotency_key': 'second-handoff'})['status'] == 'PAUSED'
    assert env.calls == [app_id]
    assert service.dispatch('request_approved_preparation', {'idempotency_key': 'first-handoff'}) == first
    service.clear_pause()
    assert service.dispatch('request_approved_preparation', {'idempotency_key': 'third-handoff'})['reason'] == 'REVOKED'


@pytest.mark.parametrize('state', ['CONFIRMED', 'UNKNOWN'])
def test_ambiguous_or_confirmed_submission_report_is_integrity_stop(env, state):
    app_id = seed_application(env)
    success = successful_executor(env)
    def execute(application_id, guard):
        result = success(application_id, guard)
        result['state'] = state
        return result
    service = bridge(env, execute=execute)
    service.approve(app_id, app_id, 60)
    result = service.dispatch('request_approved_preparation', {'idempotency_key': 'ambiguous-submit'})
    assert (result['status'], result['reason']) == ('BLOCKED', 'INTEGRITY')
    assert service.dispatch('get_preparation_readiness', {})['status'] == 'PAUSED'


def test_count_projection_does_not_silently_clamp_invalid_evidence():
    from app.services.preparation_bridge import wire
    with pytest.raises(ValueError):
        wire('READY', pending=100001)


def test_runtime_lock_has_explicit_shutdown_release(env):
    first = bridge(env)
    first.start()
    stop = getattr(first, 'stop', None)
    assert callable(stop), 'runtime lock needs deterministic release'
    stop()
    second = bridge(env)
    second.start()
    second.stop()


def test_main_preserves_private_jsonresponse_for_core_integrity_checks(env, monkeypatch):
    from app.main import create_app
    from app.routers import api
    from fastapi.responses import JSONResponse
    app = create_app(env.settings, preparation_bridge_enabled=True)
    raw = JSONResponse({'submitted': True}, status_code=409)
    monkeypatch.setattr(api, '_prefill_application', lambda *args, **kwargs: raw)
    try:
        assert app.state.preparation_bridge.execute_prefill('opaque-app', lambda: True) is raw
    finally:
        app.state.navigator.shutdown()
        app.state.db.engine.dispose()


def test_real_operator_provision_and_approval_use_existing_synthetic_root(env, capsys):
    from app.bridge.operator import main, _open_bridge
    from app.security.crypto import CryptoBox
    env.crypto = CryptoBox.from_path(env.settings.secret_key_path)
    app_id = seed_application(env)
    credential_path = env.settings.data_dir / 'machine-credential.json'
    base = ['--data-dir', str(env.settings.data_dir)]
    assert main(base + ['provision', '--endpoint', 'http://127.0.0.1:9123', '--credential-file', str(credential_path), '--ttl-seconds', '300']) == 0
    private = json.loads(credential_path.read_text())
    service, database = _open_bridge(env.settings.data_dir)
    try:
        assert service.authenticate(private['token'])
        assert main(base + ['approve', '--application-id', app_id, '--confirm-application-id', app_id, '--ttl-seconds', '60']) == 0
        output = capsys.readouterr().out
        assert private['token'] not in output
        assert private['token'] not in (env.settings.data_dir / 'preparation_bridge.sqlite3').read_bytes().decode('latin1')
        assert 'APPROVED' in output
    finally:
        database.engine.dispose()
