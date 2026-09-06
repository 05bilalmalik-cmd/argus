"""Test-only source host. Adds a synthetic ATS, not a browser/debug API.

The only observation hooks wrap real methods and return their original values.
Snapshots run in HeadedSessionWorker._pump on the browser owner thread. They
cannot fill, upload, click, resume, approve, or override a production decision.
"""
from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit

from scripts.verify_preparation_bridge import (
    EXPECTED_FIELDS, TOOLS, assert_browser_proof, write_json,
)

_INIT = """(() => {
  window.__bridgeProof = {next:0, submit:0, submissions:0, input:0, change:0};
  document.addEventListener('click', e => {
    const b=e.target.closest('button,input[type=submit],input[type=button]');
    if(!b) return;
    if (/next|continue|apply/i.test(b.innerText || b.value || '')) window.__bridgeProof.next++;
    if (b.type === 'submit') window.__bridgeProof.submit++;
  }, true);
  document.addEventListener('submit', () => window.__bridgeProof.submissions++, true);
  document.addEventListener('input', () => window.__bridgeProof.input++, true);
  document.addEventListener('change', () => window.__bridgeProof.change++, true);
})();"""
_SNAPSHOT = """async () => {
 const fields={}, files={};
 for (const el of document.querySelectorAll('input[name],select[name],textarea[name]')) {
   if(el.type==='file') {
     files[el.name]=await Promise.all([...el.files].map(async f => ({name:f.name,size:f.size,
       sha256:[...new Uint8Array(await crypto.subtle.digest('SHA-256',await f.arrayBuffer()))]
         .map(x=>x.toString(16).padStart(2,'0')).join('')})));
   } else if (!['radio','checkbox'].includes(el.type) || el.checked) fields[el.name]=el.value;
 }
 return {fields,files,counts:window.__bridgeProof,url:location.href};
}"""
_LAB = """<!doctype html><html><head><title>Summer Analyst · ARGUS Test Capital</title></head>
<body><main data-employer="ARGUS Test Capital" data-role="Summer Analyst" data-argus-form-identity="loopbacklab">
<h1>Summer Analyst</h1><p>ARGUS Test Capital · Synthetic local fixture</p>
<form id="application" method="post" action="/lab/ats/loopbacklab/submit" enctype="multipart/form-data">
<label>First name<input name="first_name" required></label>
<label>Last name<input name="last_name" required></label>
<label>Email address<input name="email" type="email" required></label>
<label>University<input name="university" required></label>
<label>Graduation year<select name="graduation_year" required><option value="">Select</option><option>2028</option></select></label>
<label>Upload CV<input name="cv" type="file" required></label>
<button type="button">Next</button><button type="submit">Submit application</button>
</form></main></body></html>"""


class OwnerObserver:
    def __init__(self, expected_url):
        self.expected_url = expected_url
        self.lock = threading.Lock()
        self.requests = {}
        self.prepare_calls = 0
        self.mutations = []
        self.external = []
        self.hooked = set()

    @contextlib.contextmanager
    def installed(self):
        from app.services.navigator import HeadedSessionWorker
        from app.automation.runner import _OwnerThreadJourney
        register = HeadedSessionWorker._register_page
        pump = HeadedSessionWorker._pump
        prepare = _OwnerThreadJourney.prepare
        observer = self

        def observed_register(worker, page):
            result = register(worker, page)
            if id(page) not in observer.hooked:
                observer.hooked.add(id(page))
                worker._assert_owner('demo.observe.install')
                page.add_init_script(_INIT)
                def observed_request(request):
                    with observer.lock:
                        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                            observer.mutations.append(request.method)
                        if urlsplit(request.url).netloc != urlsplit(observer.expected_url).netloc:
                            observer.external.append(request.method)
                page.on('request', observed_request)
            return result

        def observed_prepare(journey, page):
            with observer.lock:
                observer.prepare_calls += 1
            return prepare(journey, page)

        def observed_pump(worker):
            result = pump(worker)
            with observer.lock:
                request = observer.requests.pop(str(worker.session_id), None)
            if request is not None:
                event, output = request
                try:
                    worker._assert_owner('demo.observe.snapshot')
                    output.update(worker._page.evaluate(_SNAPSHOT))
                    output['owner_thread_id'] = threading.get_ident()
                except Exception as exc:
                    output['error_type'] = type(exc).__name__
                finally:
                    event.set()
            return result

        HeadedSessionWorker._register_page = observed_register
        HeadedSessionWorker._pump = observed_pump
        _OwnerThreadJourney.prepare = observed_prepare
        try:
            yield
        finally:
            HeadedSessionWorker._register_page = register
            HeadedSessionWorker._pump = pump
            _OwnerThreadJourney.prepare = prepare

    def snapshot(self, app, application_id, lab_posts):
        navigator = app.state.navigator
        live = navigator.active_for_application(application_id)
        assert live is not None, 'production Navigator did not retain a handoff'
        output, event = {}, threading.Event()
        with self.lock:
            self.requests[str(live.session_id)] = (event, output)
        assert event.wait(10), 'owner-thread snapshot timed out'
        assert 'error_type' not in output, 'owner-thread snapshot failed'
        counts = output.pop('counts')
        assert isinstance(counts, dict), 'browser instrumentation was not installed before navigation'
        output.update(navigator_owner_thread_id=navigator.diagnostics(live.session_id).owner_thread_id,
                      worker_alive=live.worker_alive, prepare_calls=self.prepare_calls,
                      next_clicks=counts['next'], submit_clicks=counts['submit'],
                      submit_events=counts['submissions'], input_events=counts['input'],
                      change_events=counts['change'], browser_mutating_requests=list(self.mutations),
                      external_browser_requests=list(self.external), lab_posts=lab_posts,
                      lab_submissions=len(app.state.lab_submissions),
                      page_url_matches=output.pop('url') == self.expected_url)
        return output


def seed(app, client, base_url):
    """Use production profile/document/eligibility/package paths; seed target locally."""
    from app.models import Opportunity
    from app.automation.targets import TargetResolution
    from app.automation.host_policy import origin_for_url
    from app.domain.targets import TargetKind
    from app.services.target_resolution import TargetResolutionService
    from tests.document_helpers import cv_docx_bytes

    def call(method, path, **kw):
        response = client.request(method, path, **kw)
        response.raise_for_status()
        return response.json()

    profile = dict(EXPECTED_FIELDS, degree='BSc Finance', graduation_year=2028,
                   work_authorisation='Approved synthetic wording', requires_sponsorship=False,
                   work_authorisation_approved=True)
    call('PUT', '/api/profile', json=profile)
    cv = cv_docx_bytes(2028, 'BRIDGE_SYNTHETIC_CV_MARKER')
    document = call('POST', '/api/documents',
                    data={'kind': 'cv', 'approved': 'true', 'tags': 'london,summer-cv'},
                    files={'file': ('Synthetic Bridge CV.docx', cv,
                           'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
    assert document['approved'] is True, 'synthetic CV not approved'
    target_url = base_url + '/lab/ats/loopbacklab'
    opportunity = call('POST', '/api/opportunities', json={
        'employer': 'ARGUS Test Capital', 'role_title': 'Summer Analyst',
        'division': 'Investment Banking', 'programme_group': 'summer', 'location': 'London',
        'cycle': '2027', 'url': target_url, 'source': 'lab', 'ats_type': 'loopbacklab',
        'sponsorship_supported': True, 'cv_required': True,
    })
    # This is trusted synthetic fixture setup, not a public target-verification API.
    assert urlsplit(target_url).hostname == '127.0.0.1'
    with app.state.db.session_scope() as session:
        row = session.get(Opportunity, opportunity['id'])
        TargetResolutionService(session).record(row.id, TargetResolution(
            source_url=row.navigation_url, final_url=target_url, kind=TargetKind.APPLICATION_FORM,
            provider='loopbacklab', identity_verified=True, form_verified=True,
            reason_codes=('synthetic_loopback_lab_fixture',), evidence={
                'synthetic_lab': True, 'provider': 'loopbacklab',
                'application_origin': origin_for_url(target_url), 'employer': 'ARGUS Test Capital',
                'role': 'Summer Analyst', 'requisition': '/lab/ats/loopbacklab',
                'form_identity': 'loopbacklab',
            }))
    evaluated = call('POST', f"/api/opportunities/{opportunity['id']}/evaluate")
    application_id = evaluated['application_id']
    call('POST', f'/api/applications/{application_id}/queue')
    package = call('POST', f'/api/applications/{application_id}/prepare')
    assert package['ready'] is True, 'synthetic application is not eligible/prepared'
    return application_id, hashlib.sha256(cv).hexdigest()


def invoke_native(args, private, credential, sequence, phase):
    from scripts.verify import safe_environment
    plan = private / f'{phase}-plan.json'
    result_path = private / f'{phase}-result.json'
    write_json(plan, sequence)
    home = private / f'hermes-{phase}'
    home.mkdir()
    env = safe_environment(args.root)
    env.update(HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE='1')
    # Explicit essential env survives Hermes's separate stdio allowlist too.
    child_env = {k: v for k, v in env.items() if k.upper() in {
        'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
        'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PYTHONPATH', 'PYTHONDONTWRITEBYTECODE'}}
    write_json(home / 'config.yaml', {'mcp_servers': {'argus_preparation_demo': {
        'command': args.interpreter,
        'args': ['-m', 'app.bridge.mcp_server', '--credential-file', str(credential)],
        'env': child_env, 'enabled': True, 'lazy': False, 'trust': 'full',
        'sampling': {'enabled': False}, 'timeout': 90, 'connect_timeout': 20,
    }}})  # JSON is valid YAML; no inherited profile or credentials.
    command = [args.hermes_python, str(args.root / 'scripts/verify_preparation_bridge.py'),
               '--hermes-python', args.hermes_python, '--native-plan', str(plan),
               '--native-result', str(result_path)]
    result = subprocess.run(command, cwd=args.root, env=env, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=150)
    raw = result.stdout + result.stderr
    token = json.loads(credential.read_text())['token']
    raw = raw.replace(token, '[BRIDGE_CREDENTIAL]').replace(str(private), '[PRIVATE_RUNTIME]')
    (args.evidence_dir / f'{phase}-hermes.log').write_text(raw, encoding='utf-8')
    report = json.loads(result_path.read_text()) if result_path.exists() else {'status': 'FAILED', 'calls': []}
    # Unique synthetic markers must not reach the model-visible schemas/results/logs.
    outward = json.dumps(report) + raw
    for marker in [*EXPECTED_FIELDS.values(), 'BRIDGE_SYNTHETIC_CV_MARKER', 'Synthetic Bridge CV.docx']:
        if len(marker) > 4:
            assert marker not in outward, 'synthetic data leaked through MCP'
    assert token not in outward, 'bridge credential leaked through MCP'
    write_json(args.evidence_dir / f'{phase}-native.json', report)
    assert result.returncode == 0 and report['status'] == 'PASSED', 'installed Hermes native call failed'
    from app.bridge.protocol import validate_reply
    return [validate_reply(item['reply']) for item in report['calls']]


def request(operation, **body):
    return {'operation': operation, 'body': body}


def durable_projection(app, application_id, bridge_run_id=None):
    from sqlalchemy import select, text
    from app.models import AutomationRun
    with app.state.db.session_scope() as session:
        runs = list(session.scalars(select(AutomationRun).where(AutomationRun.application_id == application_id)))
        assert len(runs) == 1, 'expected one durable production run'
        run = runs[0]
        result = {'production_run_id': run.id, 'mode': run.mode, 'state': run.state,
                  'production_run_count': len(runs)}
        assert run.mode == 'prefill', 'production run was not PREFILL'
        if bridge_run_id is not None:
            with app.state.preparation_bridge._store().transaction() as connection:
                rows = connection.execute('SELECT * FROM bridge_runs WHERE id=?', (bridge_run_id,)).fetchall()
            assert len(rows) == 1, 'missing durable bridge run'
            row = rows[0]
            assert row['underlying_run_id'] == run.id, 'bridge and production run disagree'
            assert row['application_id'] == application_id, 'wrong application correlation'
            live = app.state.navigator.active_for_application(application_id)
            assert row['session_id'] == str(live.session_id), 'wrong Navigator session correlation'
            result.update(bridge_run_id=bridge_run_id, bridge_status=row['status'], bridge_reason=row['reason'])
    return result


def run_demo(args):
    private = Path(os.environ['ARGUS_DEMO_PRIVATE_ROOT']).resolve()
    assert Path(os.environ['ARGUS_DATA_DIR']).resolve().is_relative_to(private)
    output = {'status': 'FAILED', 'observer_verified': False, 'bridge_verified': False,
              'scope': 'observer-only' if args.observer_check else 'full-native-mcp'}
    server = thread = app = None
    try:
        import httpx
        import uvicorn
        from fastapi.responses import HTMLResponse, Response
        from app.config import Settings
        from app.main import create_app
        if not args.observer_check:
            required = ('app/services/preparation_bridge.py', 'app/bridge/mcp_server.py',
                        'app/bridge/protocol.py', 'app/routers/preparation_bridge.py')
            missing = [p for p in required if not (args.root / p).is_file()]
            if missing or 'preparation_bridge_enabled' not in inspect.signature(create_app).parameters:
                output.update(status='BLOCKED', missing_prerequisites=missing,
                              reason='SOURCE_BRIDGE_INTEGRATION_NOT_PRESENT')
                return 2
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        settings = Settings.load(dict(os.environ, ARGUS_PORT=str(port), ARGUS_AUTOMATION_MODE='REVIEW_ONLY',
                                      ARGUS_ENABLE_LIVE_SUBMIT='false', ARGUS_ENABLE_TRACKR_LIVE='false',
                                      ARGUS_SWEEP_INTERVAL_HOURS='0'))
        app = create_app(settings, **({} if args.observer_check else {'preparation_bridge_enabled': True}))
        base_url = f'http://127.0.0.1:{port}'
        posts = []
        @app.get('/lab/ats/loopbacklab', response_class=HTMLResponse)
        def synthetic_form():
            return HTMLResponse(_LAB)
        # Specific test lab must precede the built-in catch-all lab route.
        app.router.routes.insert(0, app.router.routes.pop())
        @app.post('/lab/ats/loopbacklab/submit')
        def synthetic_post():
            posts.append('POST')
            return Response(status_code=409)
        app.router.routes.insert(0, app.router.routes.pop())
        observer = OwnerObserver(base_url + '/lab/ats/loopbacklab')
        with observer.installed(), httpx.Client(base_url=base_url, trust_env=False, timeout=90) as client:
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', access_log=False))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
            thread.start()
            deadline = time.monotonic() + 20
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.05)
            assert server.started, 'source HTTP server did not start'
            health = client.get('/healthz').json()
            assert health['live_submit'] is False and health['trackr_live'] is False
            application_id, digest = seed(app, client, base_url)
            if args.observer_check:
                response = client.post(f'/api/applications/{application_id}/prefill')
                response.raise_for_status()
                # Diagnostic data only from a wholly synthetic runtime.
                write_json(args.evidence_dir / 'observer-public-prefill.json', response.json())
            else:
                bridge = app.state.preparation_bridge
                credential = private / 'bridge-credential.json'
                bridge.provision(credential, base_url, 900)
                credential_payload = json.loads(credential.read_text())
                assert bridge.authenticate(credential_payload['token']) is True
                before = invoke_native(args, private, credential, [
                    request('request_approved_preparation', idempotency_key='no_approval_001')], 'no-grant')
                assert before[0]['reason'] == 'NO_APPROVAL'
                assert not app.state.navigator.all_sessions()
                from app.models import Application
                from app.domain.states import user_status_exclusion_reason
                from app.domain.opportunity_scope import out_of_scope_programme_reason
                from app.scouting.programmes import resolve_programme_framing
                with app.state.db.session_scope() as session:
                    candidate = session.get(Application, application_id)
                    target = candidate.opportunity
                    write_json(args.evidence_dir / 'approval-readiness.json', {
                        'state': candidate.state, 'automation_url_present': bool(target.automation_url),
                        'resolved_at_present': bool(target.resolved_at), 'is_archived': target.is_archived,
                        'is_open_for_applications': target.is_open_for_applications,
                        'user_excluded': bool(user_status_exclusion_reason(target.user_status)),
                        'programme_framed': bool(resolve_programme_framing(target.programme_group)),
                        'out_of_scope': bool(out_of_scope_programme_reason(target.role_title, target.programme_group))})
                approval_trace = []
                def trace_approval(frame, event, arg):
                    if event == 'exception' and frame.f_code.co_filename.endswith('preparation_bridge.py'):
                        approval_trace.append({'function': frame.f_code.co_name,
                                               'line': frame.f_lineno,
                                               'error_type': arg[0].__name__})
                    return trace_approval
                sys.settrace(trace_approval)
                try:
                    bridge.approve(application_id, application_id, 900)
                finally:
                    sys.settrace(None)
                    write_json(args.evidence_dir / 'approval-trace.json', approval_trace)
                replies = invoke_native(args, private, credential, [
                    request('get_preparation_readiness'),
                    request('request_approved_preparation', idempotency_key='synthetic_demo_001')], 'prepare')
                assert replies[0]['status'] == 'READY' and replies[0]['pending'] == 1
                prepared = replies[1]
                if prepared['status'] != 'PREFILLED_HANDOFF':
                    write_json(args.evidence_dir / 'blocked-readback-native-replies.json', invoke_native(
                        args, private, credential, [
                            request('get_preparation_run', run_id=prepared['run_id']),
                            request('list_preparation_handoffs', after_cursor=None, limit=100),
                            request('request_approved_preparation', idempotency_key='synthetic_demo_001'),
                            request('pause_preparation', reason_code='OPERATOR_REQUESTED'),
                        ], 'blocked-readback'))
                    try:
                        blocked_proof = observer.snapshot(app, application_id, len(posts))
                        write_json(args.evidence_dir / 'blocked-browser.json', blocked_proof)
                    except AssertionError as exc:
                        output['blocked_browser_error'] = str(exc)
                    from sqlalchemy import select
                    from app.models import AutomationRun
                    with app.state.db.session_scope() as session:
                        write_json(args.evidence_dir / 'blocked-production.json', [
                            {'run_id': r.id, 'state': r.state, 'mode': r.mode,
                             'receipt_present': r.receipt_json not in (None, '', '{}', 'null'),
                             'receipt_keys': sorted(json.loads(r.receipt_json or '{}'))}
                            for r in session.scalars(select(AutomationRun))])
                assert prepared['status'] == 'PREFILLED_HANDOFF', 'bridge did not report verified completed PREFILL'
                assert prepared['reason'] == 'HANDOFF'
                output['bridge_run_id'] = prepared['run_id']
            proof = observer.snapshot(app, application_id, len(posts))
            write_json(args.evidence_dir / 'browser-before-replay.json', proof)
            assert_browser_proof(proof, expected_fields=EXPECTED_FIELDS, expected_cv_sha256=digest)
            durable = durable_projection(app, application_id, output.get('bridge_run_id'))
            write_json(args.evidence_dir / 'durable-before-replay.json', durable)
            output['observer_verified'] = True
            if not args.observer_check:
                replies = invoke_native(args, private, credential, [
                    request('get_preparation_run', run_id=prepared['run_id']),
                    request('list_preparation_handoffs', after_cursor=None, limit=100),
                    request('request_approved_preparation', idempotency_key='synthetic_demo_001'),
                    request('get_preparation_readiness'),
                    request('request_approved_preparation', idempotency_key='new_handoff_001'),
                    request('pause_preparation', reason_code='OPERATOR_REQUESTED'),
                    request('request_approved_preparation', idempotency_key='after_pause_001'),
                ], 'readback-replay-pause')
                assert replies[0] == prepared == replies[2], 'run/replay mismatch'
                assert replies[1]['handoffs'] == [{'run_id': prepared['run_id'], 'status': prepared['status'], 'reason': prepared['reason']}]
                assert replies[1]['next_cursor'] is None
                assert replies[3]['status'] == replies[4]['status'] == 'BUSY'
                assert replies[5]['status'] == replies[6]['status'] == 'PAUSED'
                after = observer.snapshot(app, application_id, len(posts))
                write_json(args.evidence_dir / 'browser-after-replay.json', after)
                assert after == proof, 'duplicate replay or new request mutated retained browser'
                assert durable_projection(app, application_id, prepared['run_id']) == durable
                assert durable['bridge_status'] == prepared['status']
                output['bridge_verified'] = True
            # Close while observer hooks remain installed, via the production owner lifecycle.
            server.should_exit = True
            thread.join(30)
            assert not thread.is_alive(), 'owned source server failed graceful teardown'
            app.state.db.engine.dispose()
        output['status'] = 'PASSED'
        return 0
    except Exception as exc:
        output['error_type'] = type(exc).__name__
        traceback.print_exc()
        return 1
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(30)
        if app is not None:
            app.state.db.engine.dispose()
        output['source_server_stopped'] = thread is None or not thread.is_alive()
        write_json(args.evidence_dir / 'result.json', output)
