"""Machine HTTP boundary checks using only isolated synthetic app state."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TOKEN = 'd' * 64
PATH = '/api/preparation-bridge/call'
REQUEST = {'operation': 'get_preparation_readiness', 'body': {}}


def reply(status='READY', reason='NONE'):
    return dict(protocol='argus.preparation.v1', status=status, reason=reason,
                run_id=None, pending=0, handoffs=[], next_cursor=None)


@pytest.fixture
def bridge_client(tmp_path):
    settings = Settings.load({'ARGUS_DATA_DIR': str(tmp_path / 'data'),
        'ARGUS_API_TOKEN': 'general-fixture-token', 'ARGUS_AUTOMATION_MODE': 'OFF',
        'ARGUS_SWEEP_INTERVAL_HOURS': '0'})
    app = create_app(settings)
    called = []
    bridge = SimpleNamespace(
        start=lambda: None,
        authenticate=lambda token: token == TOKEN,
        dispatch=lambda operation, body: called.append((operation, body)) or reply(),
    )
    app.state.preparation_bridge = bridge
    with TestClient(app, base_url='http://127.0.0.1:8787', client=('127.0.0.1', 54000)) as client:
        yield client, bridge, called


def post(client, **kwargs):
    return client.post(PATH, headers={'Authorization': 'Bearer ' + TOKEN}, **kwargs)


def test_authenticated_strict_request_reaches_dispatch(bridge_client):
    client, bridge, called = bridge_client
    result = post(client, json=REQUEST)
    assert result.status_code == 200
    assert result.json() == reply()
    assert called == [('get_preparation_readiness', {})]


@pytest.mark.parametrize('token', ['', 'Bearer general-fixture-token', 'Bearer ' + 'e' * 64])
def test_general_missing_or_wrong_credentials_never_dispatch(bridge_client, token):
    client, bridge, called = bridge_client
    result = client.post(PATH, headers={'Authorization': token}, json=REQUEST)
    assert result.status_code == 401
    assert result.json() == reply('REFUSED', 'UNAUTHENTICATED')
    assert called == []


@pytest.mark.parametrize('body', [
    {'operation': 'approve', 'body': {}},
    {'operation': 'get_preparation_readiness', 'body': {'private_value': 'PRIVATE_SENTINEL'}},
    {'operation': 'request_approved_preparation', 'body': {'idempotency_key': 'test-key1', 'url': 'PRIVATE_SENTINEL'}},
    {'operation': 'get_preparation_run', 'body': {'run_id': 'PRIVATE_SENTINEL'}},
    {'operation': 'list_preparation_handoffs', 'body': {'after_cursor': None, 'limit': True}},
    {'operation': 'pause_preparation', 'body': {'reason_code': 'PRIVATE_SENTINEL'}},
])
def test_invalid_request_never_echoes_or_dispatches(bridge_client, body):
    client, bridge, called = bridge_client
    result = post(client, json=body)
    assert result.status_code == 400
    assert result.json() == reply('REFUSED', 'INVALID_REQUEST')
    assert 'PRIVATE_SENTINEL' not in result.text
    assert called == []


@pytest.mark.parametrize('raw', [b'{"operation":"get_preparation_readiness","body":{},"body":{}}', b'['*2000, b' '*16385])
def test_duplicate_deep_or_oversized_json_is_refused(bridge_client, raw):
    client, bridge, called = bridge_client
    result = client.post(PATH, content=raw, headers={'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'})
    assert result.status_code == 400
    assert result.json() == reply('REFUSED', 'INVALID_REQUEST')
    assert called == []


def test_authentication_precedes_body_parsing(bridge_client):
    client, bridge, called = bridge_client
    result = client.post(PATH, content=b'PRIVATE_SENTINEL-not-json')
    assert result.status_code == 401
    assert 'PRIVATE_SENTINEL' not in result.text
    assert called == []


def test_default_off_refuses_without_creating_bridge_storage(bridge_client):
    client, bridge, called = bridge_client
    client.app.state.preparation_bridge = None
    result = post(client, json=REQUEST)
    assert result.status_code == 503
    assert result.json() == reply('REFUSED', 'DISABLED')
    assert not list(client.app.state.settings.data_dir.glob('*bridge*'))


@pytest.mark.parametrize('failure', ['exception', 'extra-field', 'unknown-enum'])
def test_dispatch_errors_and_private_result_fields_never_escape(bridge_client, failure):
    client, bridge, called = bridge_client
    def dispatch(*args):
        if failure == 'exception':
            raise RuntimeError('PRIVATE_SENTINEL')
        value = reply()
        value['private'] = 'PRIVATE_SENTINEL' if failure == 'extra-field' else None
        if failure == 'unknown-enum':
            value.pop('private')
            value['status'] = 'PRIVATE_SENTINEL'
        return value
    bridge.dispatch = dispatch
    result = post(client, json=REQUEST)
    assert result.status_code == 500
    assert result.json() == reply('REFUSED', 'INTEGRITY')
    assert 'PRIVATE_SENTINEL' not in result.text


def test_browser_origin_is_not_a_machine_client(bridge_client):
    client, bridge, called = bridge_client
    result = client.post(PATH, json=REQUEST, headers={'Authorization': 'Bearer '+TOKEN, 'Origin': 'http://127.0.0.1:8787'})
    assert result.status_code == 401
    assert called == []
