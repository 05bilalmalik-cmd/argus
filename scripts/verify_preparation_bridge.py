#!/usr/bin/env python3
"""Real Hermes registry -> source MCP -> HTTP -> PREFILL acceptance harness.

Default execution is the full acceptance test. --observer-check exercises only
production PREFILL and the independent observer, never reports bridge success.
No model inference, user Hermes config, employer sites, or submit authority.
Evidence is append-only per attempt; private synthetic runtime/credential files
are segregated from publishable evidence and are removed on normal teardown.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOOLS = frozenset({
    'get_preparation_readiness', 'request_approved_preparation',
    'get_preparation_run', 'list_preparation_handoffs', 'pause_preparation',
})
EXPECTED_FIELDS = {
    'first_name': 'BridgeSynthetic', 'last_name': 'CandidateMarker',
    'email': 'bridge.synthetic@example.test', 'university': 'Synthetic University',
    'graduation_year': '2028',
}


def assert_browser_proof(proof, *, expected_fields, expected_cv_sha256):
    """An HTTP/bridge status cannot substitute for owner-thread DOM evidence."""
    assert proof.get('owner_thread_id'), 'missing owner-thread observation'
    assert proof['owner_thread_id'] == proof.get('navigator_owner_thread_id'), 'wrong owner thread'
    assert proof.get('worker_alive') is True, 'owned browser no longer alive'
    assert proof.get('actual_headless') is False, 'acceptance requires an actual headed browser'
    assert proof.get('prepare_calls') == 1, 'PREFILL must execute exactly once'
    assert proof.get('next_clicks') == 0, 'Next was clicked'
    assert proof.get('submit_clicks') == 0, 'Submit was clicked'
    assert proof.get('submit_events') == 0, 'form was submitted'
    assert proof.get('browser_mutating_requests') == [], 'browser emitted POST/mutation'
    assert proof.get('lab_posts') == 0, 'lab received POST'
    assert proof.get('lab_submissions') == 0, 'lab recorded submission'
    assert proof.get('external_browser_requests') == [], 'browser attempted external request'
    assert proof.get('page_url_matches') is True, 'owned page changed'
    fields = proof.get('fields', {})
    for key, expected in expected_fields.items():
        assert fields.get(key) == expected, f'wrong approved field: {key}'
    uploads = proof.get('files', {}).get('cv', [])
    assert len(uploads) == 1, 'CV must have one independently observed file'
    assert uploads[0].get('size', 0) > 0, 'CV has no bytes'
    assert uploads[0].get('sha256') == expected_cv_sha256, 'CV bytes differ from approved document'


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def source_hashes(root):
    return {str(p.relative_to(root)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
            for base in ('app', 'scripts', 'tests') for p in sorted((root / base).rglob('*.py'))}


def decode_hermes_result(raw):
    """Decode the installed Hermes result envelope, not a simulated SDK result."""
    envelope = json.loads(raw) if isinstance(raw, str) else raw
    assert isinstance(envelope, dict), 'Hermes registry returned non-object'
    value = envelope.get('error', envelope.get('result', envelope))
    if isinstance(value, str):
        value = json.loads(value)
    assert isinstance(value, dict), 'Hermes result is not a protocol object'
    from app.bridge.protocol import validate_reply
    value = validate_reply(value)
    if 'error' in envelope:
        assert value['status'] == 'REFUSED', 'unexpected error-wrapped success'
    return value


def native_worker(args):
    """Runs inside installed Hermes Python with a newly created HERMES_HOME."""
    # The parent has already scrubbed env; do not import any user CLI or auth.
    plan = json.loads(Path(args.native_plan).read_text(encoding='utf-8'))
    hermes_root = Path(args.hermes_python).resolve().parents[2]
    assert (hermes_root / 'tools/mcp_tool.py').is_file(), 'installed Hermes source not found'
    sys.path.insert(0, str(hermes_root))
    report = {'status': 'FAILED', 'calls': [], 'schemas': [], 'discovered': []}
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.mcp_tool_schema import mcp_prefixed_tool_name
    from tools.registry import registry
    try:
        report['discovered'] = discover_mcp_tools(allowed_mcp_names=['argus_preparation_demo'])
        names = {tool: mcp_prefixed_tool_name('argus_preparation_demo', tool) for tool in TOOLS}
        assert set(report['discovered']) == set(names.values()), 'native discovery must expose exactly five tools'
        report['schemas'] = [registry.get_schema(names[t]) for t in sorted(TOOLS)]
        report['native_modules'] = {
            'discovery': discover_mcp_tools.__module__, 'registry': type(registry).__module__,
            'mcp_sha256': hashlib.sha256((hermes_root / 'tools/mcp_tool.py').read_bytes()).hexdigest(),
        }
        for request in plan:
            operation, body = request['operation'], request['body']
            assert operation in TOOLS
            item = {'operation': operation, 'body': body, 'started': True}
            report['calls'].append(item)
            write_json(args.native_result, report)  # attempted calls survive a timeout
            raw = registry.dispatch(names[operation], body)
            item['raw_registry_result'] = raw
            item['reply'] = decode_hermes_result(raw)
            write_json(args.native_result, report)
        report['status'] = 'PASSED'
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        # All inputs are synthetic; keep traceback private for diagnosis.
        traceback.print_exc()
        raise
    finally:
        shutdown_mcp_servers()
        import logging
        logging.shutdown()
        write_json(args.native_result, report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--interpreter', default=sys.executable)
    parser.add_argument('--hermes-python', default=str(Path.home() / 'AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe'))
    parser.add_argument('--evidence-dir', type=Path)
    parser.add_argument('--observer-check', action='store_true', help='partial test only; does not claim MCP acceptance')
    parser.add_argument('--staged-form', action='store_true', help='negative boundary: leave visible Next untouched and require HUMAN_REQUIRED')
    parser.add_argument('--source-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--native-plan', help=argparse.SUPPRESS)
    parser.add_argument('--native-result', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.native_plan:
        native_worker(args)
        return 0
    if args.source_worker:
        from scripts.preparation_bridge_demo_server import run_demo
        return run_demo(args)

    root = args.root.resolve(strict=True)
    assert root == ROOT, '--root must select the checkout containing this verifier'
    if args.evidence_dir is None:
        args.evidence_dir = Path(tempfile.mkdtemp(prefix='argus-preparation-proof-'))
    evidence = args.evidence_dir.resolve()
    assert not evidence.is_relative_to(root), 'evidence must be outside the checkout'
    evidence.mkdir(parents=True, exist_ok=True)
    attempt = evidence / (time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
    attempt.mkdir()
    baseline = source_hashes(root)
    write_json(attempt / 'source-before.json', baseline)
    manifest = {'status': 'STARTED', 'scope': 'observer-only' if args.observer_check else 'full-native-mcp',
                'scenario': 'staged-boundary' if args.staged_form else 'single-page-preparation',
                'attempt_id': attempt.name, 'started_at': time.time(), 'model_inference': False}
    write_json(attempt / 'manifest.json', manifest)
    from scripts.verify import safe_environment
    env = safe_environment(root)
    with tempfile.TemporaryDirectory(prefix='argus-preparation-private-') as private:
        env.update({'ARGUS_DATA_DIR': str(Path(private) / 'data'),
                    'ARGUS_API_TOKEN': uuid.uuid4().hex, 'ARGUS_FORCE_HEADLESS': 'false',
                    'ARGUS_ENABLE_APPLY_CLICK': 'false', 'ARGUS_ENABLE_PREPARATION_BRIDGE': 'true',
                    'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
                    'ARGUS_DEMO_PRIVATE_ROOT': private})
        command = [args.interpreter, str(root / 'scripts/verify_preparation_bridge.py'),
                   '--source-worker', '--root', str(root), '--interpreter', args.interpreter,
                   '--hermes-python', args.hermes_python, '--evidence-dir', str(attempt)]
        if args.observer_check:
            command.append('--observer-check')
        if args.staged_form:
            command.append('--staged-form')
        manifest['command'] = command
        write_json(attempt / 'manifest.json', manifest)
        # No shell and no process-name cleanup. Child owns server and browser lifecycle.
        try:
            result = subprocess.run(command, cwd=root, env=env, capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=300)
            raw_log = result.stdout + result.stderr
            # Credential values are never deliberately logged. Redact exact private
            # root/general test token if an exception embeds them before publishing.
            raw_log = raw_log.replace(private, '[PRIVATE_RUNTIME]').replace(env['ARGUS_API_TOKEN'], '[TEST_TOKEN]')
            (attempt / 'execution.log').write_text(raw_log, encoding='utf-8')
            manifest['exit_code'] = result.returncode
        except subprocess.TimeoutExpired:
            manifest.update(status='BLOCKED', exit_code=124, reason='SOURCE_WORKER_TIMEOUT')
        finally:
            current = source_hashes(root)
            write_json(attempt / 'source-after.json', current)
            manifest['source_stable'] = current == baseline
            child_path = attempt / 'result.json'
            if child_path.exists():
                child = json.loads(child_path.read_text(encoding='utf-8'))
                manifest['result'] = child
                manifest['status'] = child['status']
            else:
                manifest['status'] = 'BLOCKED'
            if not manifest['source_stable']:
                manifest['status'] = 'FAILED'
            if manifest.get('exit_code') != 0 and manifest['status'] == 'PASSED':
                manifest['status'] = 'FAILED'
            manifest['finished_at'] = time.time()
            write_json(attempt / 'manifest.json', manifest)
    # Aggregate retained attempts from disk; never report only the last success.
    attempts = [json.loads(p.read_text(encoding='utf-8')) for p in evidence.glob('*/manifest.json')]
    write_json(evidence / 'attempts.json', {'count': len(attempts), 'attempts': attempts})
    print(json.dumps({'status': manifest['status'], 'scope': manifest['scope'],
                      'attempt_count': len(attempts), 'evidence': str(attempt)}, sort_keys=True))
    return 0 if manifest['status'] == 'PASSED' and manifest.get('exit_code') == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
