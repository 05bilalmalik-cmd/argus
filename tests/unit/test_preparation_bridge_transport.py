"""Restricted transport tests: synthetic values and owned loopback only."""
from __future__ import annotations

import importlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def http_fixture(tmp_path):
    state = SimpleNamespace(calls=[], status=200, raw=None, headers={}, token=secrets.token_hex(32))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            state.calls.append((self.path, self.headers.get('Authorization'), body))
            data = state.raw if state.raw is not None else json.dumps(reply()).encode()
            self.send_response(state.status)
            self.send_header('Content-Type', 'application/json')
            for key, value in state.headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(data)
                self.wfile.flush()
                if getattr(state, 'hold_open', None) is not None:
                    state.hold_open.wait(timeout=10)
            except (ConnectionError, OSError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.endpoint = 'http://127.0.0.1:' + str(server.server_port)
    state.credential_file = tmp_path / 'synthetic-credential.json'
    state.credentials = {'endpoint': state.endpoint, 'token': state.token, 'expires_at': time.time() + 300}
    state.credential_file.write_text(json.dumps(state.credentials), encoding='utf-8')
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def bridge_client(path):
    try:
        module = importlib.import_module('app.bridge.client')
    except ModuleNotFoundError:
        pytest.fail('private HTTP bridge client is not implemented')
    return module.BridgeClient(path)


def test_client_uses_real_owned_http_and_dedicated_private_credentials(http_fixture, monkeypatch):
    state = http_fixture
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('ALL_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '')
    client = bridge_client(state.credential_file)
    for operation, body in OPERATIONS.items():
        assert client.call(operation, body) == reply()
    assert len(state.calls) == 5
    for (path, authorization, raw), (operation, body) in zip(state.calls, OPERATIONS.items()):
        assert path == '/api/preparation-bridge/call'
        assert authorization == 'Bearer ' + state.token
        assert json.loads(raw) == {'operation': operation, 'body': body}
        assert len(raw) <= 16384

@pytest.mark.parametrize('changes', [
    {'endpoint': 'http://localhost:8787'}, {'endpoint': 'https://127.0.0.1:8787'},
    {'endpoint': 'http://127.0.0.1'}, {'endpoint': 'http://127.0.0.1:0'},
    {'endpoint': 'http://127.0.0.1:65536'}, {'endpoint': 'http://127.0.0.1:08787'},
    {'endpoint': 'http://127.0.0.1:8787/'}, {'endpoint': 'http://127.0.0.1:8787/path'},
    {'endpoint': 'http://user@127.0.0.1:8787'}, {'endpoint': 'http://127.0.0.1:8787?q=x'},
    {'endpoint': 'http://127.0.0.1:8787#x'}, {'endpoint': 'http://127.0.0.1:8787\n'},
    {'endpoint': 'http://[::1]:8787'}, {'endpoint': 'http://127.0.0.2:8787'},
    {'endpoint': 'http://example.invalid:8787'}, {'endpoint': 123},
    {'token': 'short'}, {'token': 'x' * 64}, {'token': 'a' * 129}, {'token': 'a' * 33},
    {'token': 'a' * 63 + '\n'}, {'token': 123},
    {'expires_at': 1}, {'expires_at': float('inf')}, {'expires_at': float('nan')},
    {'expires_at': '9999999999'}, {'expires_at': True}, {'extra': 'PRIVATE_MARKER'},
])
def test_client_refuses_bad_credentials_before_network(http_fixture, monkeypatch, changes):
    import socket

    def no_network(*args, **kwargs):
        pytest.fail('invalid credential reached network')

    monkeypatch.setattr(socket.socket, 'connect', no_network)
    state = http_fixture
    state.credential_file.write_text(json.dumps(state.credentials | changes), encoding='utf-8')
    result = bridge_client(state.credential_file).call('get_preparation_readiness', {})
    assert result == reply(status='REFUSED', reason='TRANSPORT_ERROR')
    assert state.calls == []


def test_client_credential_file_is_bounded_duplicate_free_and_reloaded(http_fixture):
    state = http_fixture
    client = bridge_client(state.credential_file)
    assert client.call('get_preparation_readiness', {}) == reply()
    for raw in [b'{', b'[]', b'null', b'\xff', b' ' * 16385,
                json.dumps(state.credentials).replace('{', '{"token":"PRIVATE_MARKER",', 1).encode(),
                json.dumps({key: val for key, val in state.credentials.items() if key != 'token'}).encode(),
                json.dumps(state.credentials | {'expires_at': 0}).encode()]:
        state.credential_file.write_bytes(raw)
        assert client.call('get_preparation_readiness', {}) == reply(status='REFUSED', reason='TRANSPORT_ERROR')
    state.credential_file.unlink()
    assert client.call('get_preparation_readiness', {}) == reply(status='REFUSED', reason='TRANSPORT_ERROR')
    assert len(state.calls) == 1


@pytest.mark.parametrize('status,raw,headers', [
    (302, None, {'Location': '/PRIVATE_MARKER'}),
    (307, None, {'Location': '/PRIVATE_MARKER'}),
    (401, None, {}), (500, None, {}), (204, b'', {}),
    (200, b'{"private":"PRIVATE_MARKER"}', {}),
    (200, b'{', {}), (200, b'\xff', {}),
    (200, b'[' * 2000 + b']' * 2000, {}),
    (200, b'{"protocol":"PRIVATE_MARKER","protocol":"argus.preparation.v1","status":"READY","reason":"NONE","run_id":null,"pending":0,"handoffs":[],"next_cursor":null}', {}),
    (200, b' ' * 65537, {}), (200, None, {'Content-Length': '65537'}),
    (200, b'PRIVATE_MARKER', {'Content-Length': '1000'}),
    (200, None, {'Content-Encoding': 'gzip'}),
], ids=['redirect302', 'redirect307', 'auth401', 'error500', 'empty204', 'extra',
        'malformed', 'utf8', 'nested', 'duplicate', 'oversize', 'declared-oversize',
        'truncated', 'encoded'])
def test_client_refuses_http_errors_or_malformed_bounded_reply(http_fixture, status, raw, headers):
    state = http_fixture
    state.status, state.raw, state.headers = status, raw, headers
    assert bridge_client(state.credential_file).call('get_preparation_readiness', {}) == reply(status='REFUSED', reason='TRANSPORT_ERROR')
    assert len(state.calls) == 1  # no redirects or retries


def test_client_invalid_arguments_are_refused_before_credential_read(tmp_path):
    client = bridge_client(tmp_path / 'must-not-be-read')
    for operation, body in [
        ('lookup_url', {'url': 'PRIVATE_MARKER'}),
        ('get_preparation_readiness', {'credential_file': 'PRIVATE_MARKER'}),
        ('get_preparation_readiness', []), ('get_preparation_readiness', None),
        ('request_approved_preparation', {'idempotency_key': 'a' * 16385}),
        ('list_preparation_handoffs', {'after_cursor': None, 'limit': True}),
    ]:
        assert client.call(operation, body) == reply(status='REFUSED', reason='INVALID_REQUEST')


def test_client_cuts_off_oversize_stream_before_peer_closes(http_fixture):
    from concurrent.futures import ThreadPoolExecutor

    state = http_fixture
    state.raw = b' ' * 65537
    state.hold_open = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bridge_client(state.credential_file).call, 'get_preparation_readiness', {})
        try:
            assert future.result(timeout=3) == reply(status='REFUSED', reason='TRANSPORT_ERROR')
        finally:
            state.hold_open.set()


def test_actual_mcp_sdk_subprocess_exact_surface_and_private_calls(http_fixture, tmp_path):
    import asyncio
    import sys
    from datetime import timedelta

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.shared.exceptions import McpError
    from mcp import types

    state = http_fixture
    stderr_path = tmp_path / 'facade-stderr.log'

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=['-m', 'app.bridge.mcp_server', '--credential-file', str(state.credential_file)],
            cwd=str(Path(__file__).resolve().parents[2]),
            env={'PYTHONDONTWRITEBYTECODE': '1'},
        )
        with stderr_path.open('w', encoding='utf-8') as stderr:
            async with stdio_client(params, errlog=stderr) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=10)) as session:
                    initialized = await session.initialize()
                    assert initialized.capabilities.resources is None
                    assert initialized.capabilities.prompts is None
                    tools = (await session.list_tools()).tools
                    assert {tool.name for tool in tools} == set(OPERATIONS)
                    assert len(tools) == 5
                    for tool in tools:
                        assert tool.inputSchema['additionalProperties'] is False
                        assert tool.outputSchema['additionalProperties'] is False
                        assert tool.annotations.openWorldHint is False
                        assert tool.annotations.idempotentHint is True
                        assert tool.annotations.readOnlyHint is (tool.name not in {
                            'request_approved_preparation', 'pause_preparation'})
                        assert tool.annotations.destructiveHint is (tool.name == 'pause_preparation')
                        assert 'credential' not in json.dumps(tool.inputSchema).lower()
                    for operation, body in OPERATIONS.items():
                        result = await session.call_tool(operation, body)
                        assert result.isError is False
                        assert result.structuredContent == reply()
                        assert json.loads(result.content[0].text) == reply()
                    assert len(state.calls) == 5
                    for operation, body in [
                        ('get_preparation_readiness', {'PRIVATE_MARKER': 'PRIVATE_MARKER'}),
                        ('PRIVATE_MARKER', {}),
                        ('get_preparation_run', {'run_id': 'PRIVATE_MARKER'}),
                        ('list_preparation_handoffs', {'after_cursor': None, 'limit': True}),
                        ('request_approved_preparation', {'idempotency_key': 'PRIVATE_MARKER' * 2000}),
                    ]:
                        result = await session.call_tool(operation, body)
                        assert result.isError is True
                        assert result.structuredContent == reply(status='REFUSED', reason='INVALID_REQUEST')
                        assert 'PRIVATE_MARKER' not in result.model_dump_json()
                    result = await session.send_request(types.ClientRequest(types.CallToolRequest(
                        method='tools/call', params=types.CallToolRequestParams(
                            name='get_preparation_readiness', arguments={}, PRIVATE_MARKER='PRIVATE_MARKER',
                        ),
                    )), types.CallToolResult)
                    assert result.structuredContent == reply(status='REFUSED', reason='INVALID_REQUEST')
                    assert len(state.calls) == 5
                    for forbidden in (session.list_resources, session.list_prompts):
                        with pytest.raises(McpError):
                            await forbidden()
                    state.raw = b'{"PRIVATE_MARKER":"PRIVATE_MARKER"}'
                    result = await session.call_tool('get_preparation_readiness', {})
                    assert result.isError is True
                    assert result.structuredContent == reply(status='REFUSED', reason='TRANSPORT_ERROR')
                    state.credential_file.unlink()
                    result = await session.call_tool('get_preparation_readiness', {})
                    assert result.structuredContent == reply(status='REFUSED', reason='TRANSPORT_ERROR')
                    assert len(state.calls) == 6

    asyncio.run(exercise())
    stderr = stderr_path.read_text(encoding='utf-8')
    for marker in ['PRIVATE_MARKER', state.token, str(state.credential_file)]:
        assert marker not in stderr
    for (path, authorization, raw), (operation, body) in zip(state.calls, OPERATIONS.items()):
        assert path == '/api/preparation-bridge/call'
        assert authorization == 'Bearer ' + state.token
        assert json.loads(raw) == {'operation': operation, 'body': body}


RUN_ID = '12345678-1234-4123-8123-123456789abc'
OPERATIONS = {
    'get_preparation_readiness': {},
    'request_approved_preparation': {'idempotency_key': 'synthetic_key-123'},  # gitleaks:allow -- synthetic request identifier, not a credential
    'get_preparation_run': {'run_id': RUN_ID},
    'list_preparation_handoffs': {'after_cursor': None, 'limit': 100},
    'pause_preparation': {'reason_code': 'OPERATOR_REQUESTED'},
}


def protocol():
    try:
        return importlib.import_module('app.bridge.protocol')
    except ModuleNotFoundError:
        pytest.fail('strict preparation wire protocol is not implemented')


def reply(**changes):
    value = {
        'protocol': 'argus.preparation.v1', 'status': 'READY', 'reason': 'NONE',
        'run_id': None, 'pending': 0, 'handoffs': [], 'next_cursor': None,
    }
    return value | changes


def test_reply_is_exact_bounded_projection_with_fixed_refusal():
    wire = protocol()
    assert hasattr(wire, 'validate_reply'), 'reply validation is missing'
    assert wire.validate_reply(reply()) == reply()
    valid = reply(run_id=RUN_ID, pending=100000, next_cursor=RUN_ID,
                  handoffs=[{'run_id': RUN_ID, 'status': 'HUMAN_REQUIRED', 'reason': 'HANDOFF'}] * 100)
    assert wire.validate_reply(valid) == valid
    for value in [
        reply(raw_text='SENSITIVE'), reply(protocol='other'), reply(status='SUBMITTED'),
        reply(reason='SENSITIVE'), reply(run_id=RUN_ID.upper()), reply(pending=True),
        reply(pending='0'), reply(pending=0.0), reply(pending=-1), reply(pending=100001),
        reply(next_cursor='invalid'), reply(handoffs=valid['handoffs'] * 2),
        reply(handoffs=[{'run_id': RUN_ID, 'status': 'READY', 'reason': 'NONE', 'name': 'SENSITIVE'}]),
        reply(handoffs=[{'run_id': None, 'status': 'READY', 'reason': 'NONE'}]),
        reply(handoffs=[{'run_id': RUN_ID, 'status': 'READY'}]),
        {key: val for key, val in reply().items() if key != 'next_cursor'}, [], None,
    ]:
        with pytest.raises(ValueError):
            wire.validate_reply(value)
    assert wire.refusal('TRANSPORT_ERROR') == reply(status='REFUSED', reason='TRANSPORT_ERROR')
    assert wire.refusal('SENSITIVE') == reply(status='REFUSED', reason='INVALID_REQUEST')


def test_request_protocol_accepts_only_five_strict_closed_bodies():
    wire = protocol()
    for operation, body in OPERATIONS.items():
        result = wire.parse_request(json.dumps({'operation': operation, 'body': body}).encode())
        assert result.operation == operation
        assert result.body == body
    invalid = [
        {'operation': 'submit', 'body': {}},
        {'operation': 'get_preparation_readiness', 'body': {'url': 'SENSITIVE'}},
        {'operation': 'get_preparation_readiness', 'body': {}, 'extra': 'SENSITIVE'},
        {'operation': 'request_approved_preparation', 'body': {'idempotency_key': 'x' * 65}},
        {'operation': 'request_approved_preparation', 'body': {'idempotency_key': 'short'}},
        {'operation': 'request_approved_preparation', 'body': {'idempotency_key': 'unicode_é'}},
        {'operation': 'request_approved_preparation', 'body': {'idempotency_key': {'x': 'SENSITIVE'}}},
        {'operation': 'get_preparation_run', 'body': {'run_id': RUN_ID.upper()}},
        {'operation': 'get_preparation_run', 'body': {'run_id': RUN_ID.replace('-', '')}},
        {'operation': 'list_preparation_handoffs', 'body': {'after_cursor': None, 'limit': True}},
        {'operation': 'list_preparation_handoffs', 'body': {'after_cursor': None, 'limit': '1'}},
        {'operation': 'list_preparation_handoffs', 'body': {'after_cursor': None, 'limit': 0}},
        {'operation': 'list_preparation_handoffs', 'body': {'after_cursor': None, 'limit': 101}},
        {'operation': 'list_preparation_handoffs', 'body': {'limit': 1}},
        {'operation': 'pause_preparation', 'body': {'reason_code': 'RESUME'}},
        {'operation': 'get_preparation_run', 'body': []},
        [], None, True,
    ]
    for value in invalid:
        with pytest.raises(ValueError):
            wire.parse_request(json.dumps(value).encode())
    for raw in [
        b'{"operation":"get_preparation_readiness","operation":"pause_preparation","body":{}}',
        b'{"operation":"list_preparation_handoffs","body":{"after_cursor":null,"limit":1,"limit":2}}',
        b'{"operation":"get_preparation_readiness","body":{}} trailing',
        b'\xff', b'{' + b' ' * 16384 + b'}', b'[' * 2000 + b']' * 2000,
    ]:
        with pytest.raises(ValueError):
            wire.parse_request(raw)
