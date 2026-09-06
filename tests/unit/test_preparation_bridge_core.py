from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import inspect

from app.config import Settings
from app.db import Database
from app.security.crypto import CryptoBox


@pytest.fixture
def env(tmp_path):
    settings = Settings.load({'ARGUS_DATA_DIR': str(tmp_path / 'data'), 'ARGUS_API_TOKEN': 'generic-test-token', 'ARGUS_AUTOMATION_MODE': 'OFF', 'ARGUS_ENABLE_LIVE_SUBMIT': 'false', 'ARGUS_ENABLE_TRACKR_LIVE': 'false'})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    calls = []
    nav = SimpleNamespace(sessions=[], all_sessions=lambda: nav.sessions)
    box = SimpleNamespace(database=database, settings=settings, crypto=CryptoBox(Fernet.generate_key()), nav=nav, calls=calls, now=[2000000000.0])
    yield box
    database.engine.dispose()


def bridge(env, *, enabled=True, execute=None):
    try:
        cls = import_module('app.services.preparation_bridge').PreparationBridge
    except ModuleNotFoundError:
        pytest.fail('PreparationBridge implementation is missing')
    return cls(env.database, env.settings, env.crypto, env.nav, execute or (lambda app, guard: env.calls.append(app)), enabled=enabled, clock=lambda: env.now[0])


def seed_application(env, **overrides):
    from datetime import datetime, timezone
    from app.models import Application, Opportunity
    from app.services.profile import ProfileService, ProfileUpdate
    with env.database.session_scope() as session:
        ProfileService(session, env.crypto).update(ProfileUpdate(first_name='Synthetic', graduation_year=2028, requires_sponsorship=False, work_authorisation='UK', work_authorisation_approved=True))
        values = dict(employer='Synthetic Bank', role_title='Summer Internship', programme_group='summer', cycle='2027', url='https://example.test/job', application_url='https://example.test/form', target_status='APPLICATION_FORM', resolved_at=datetime.now(timezone.utc), application_window_status='OPEN', cv_required=False)
        values.update(overrides)
        opportunity = Opportunity(**values)
        session.add(opportunity)
        session.flush()
        application = Application(opportunity_id=opportunity.id, state='QUEUED')
        session.add(application)
        session.flush()
        from app.services.applications import ApplicationService
        ApplicationService(session, env.settings, env.crypto).prepare(application.id)
        return application.id


def successful_executor(env):
    def execute(application_id, guard):
        from datetime import datetime, timezone
        from uuid import uuid4
        from app.models import AutomationRun
        from app.automation.types import SessionSnapshot, SessionState
        assert guard() is True
        env.calls.append(application_id)
        with env.database.session_scope() as session:
            run = AutomationRun(application_id=application_id, mode='prefill', state='NEEDS_USER')
            session.add(run)
            session.flush()
            run_id = run.id
        now = datetime.now(timezone.utc)
        snapshot = SessionSnapshot(session_id=str(uuid4()), application_id=application_id, mode='prefill', state=SessionState.FINAL_REVIEW, created_at=now, updated_at=now, expires_at=datetime.fromtimestamp(env.now[0]+60, timezone.utc), worker_alive=True, headed=True)
        env.nav.sessions.append(snapshot)
        return {'state': 'NEEDS_USER', 'run_id': run_id, 'handoff_session_id': snapshot.session_id, 'receipt': None}
    return execute


def test_approval_never_prepares_or_rewrites_application(env):
    from sqlalchemy import text
    from app.models import Application
    app_id = seed_application(env)
    service = bridge(env)
    with env.database.SessionLocal() as session:
        before = session.execute(text('SELECT * FROM applications')).all()
        audit_before = session.execute(text('SELECT * FROM audit_events')).all()
    service.approve(app_id, app_id, 60)
    with env.database.SessionLocal() as session:
        assert session.execute(text('SELECT * FROM applications')).all() == before
        assert session.execute(text('SELECT * FROM audit_events')).all() == audit_before
    with env.database.session_scope() as session:
        session.get(Application, app_id).state = 'QUEUED'
    with pytest.raises(ValueError):
        service.approve(app_id, app_id, 60)


def test_operator_approval_freezes_prepared_application_and_persists_single_use_grant(env):
    from app.models import Application
    app_id = seed_application(env)
    service = bridge(env, execute=successful_executor(env))
    service.start()
    with pytest.raises(ValueError):
        service.approve(app_id, 'wrong-confirmation', 60)
    grant_id = service.approve(app_id, app_id, 60)
    assert isinstance(grant_id, str)
    with env.database.session_scope() as session:
        assert session.get(Application, app_id).state == 'PACKAGE_PREPARED'
    second = bridge(env, execute=successful_executor(env))
    result = second.dispatch('request_approved_preparation', {'idempotency_key': 'approved_key'})
    assert result['status'] == 'PREFILLED_HANDOFF'
    assert result['reason'] == 'HANDOFF'
    assert second.dispatch('get_preparation_run', {'run_id': result['run_id']}) == result
    assert second.dispatch('request_approved_preparation', {'idempotency_key': 'approved_key'}) == result
    env.nav.sessions.clear()
    assert second.dispatch('request_approved_preparation', {'idempotency_key': 'another_key'})['reason'] == 'NO_APPROVAL'
    assert env.calls == [app_id]
    assert (env.settings.data_dir / 'preparation_bridge.sqlite3').is_file()


@pytest.mark.parametrize('values', [{'resolved_at': None}, {'target_status': 'UNRESOLVED'}, {'cv_required': True}, {'programme_group': 'graduate'}, {'application_window_status': 'CLOSED'}, {'user_status': 'APPLIED'}])
def test_approval_obeys_existing_prepare_and_verified_target_rules(env, values):
    app_id = seed_application(env, **values)
    service = bridge(env)
    with pytest.raises(ValueError):
        service.approve(app_id, app_id, 60)
    assert service.dispatch('request_approved_preparation', {'idempotency_key': 'refused_key'})['reason'] == 'NO_APPROVAL'
    assert not env.calls


def seed_document(env):
    import hashlib
    from app.models import Document
    path = env.settings.documents_dir / 'synthetic-cover.txt'
    path.write_bytes(b'Synthetic approved cover letter')
    with env.database.session_scope() as session:
        doc = Document(name=path.name, kind='cover_letter', path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), approved=True, tags_json='["summer-cv"]')
        session.add(doc)
        session.flush()
        return doc.id, path


def drift(env, application_id, kind):
    from app.models import Application, CandidateProfile, AnswerEntry, Document
    with env.database.session_scope() as session:
        app = session.get(Application, application_id)
        if kind == 'profile':
            session.get(CandidateProfile, 1).first_name = 'Changed'
        elif kind == 'answers':
            session.add(AnswerEntry(canonical_key='synthetic.answer', answer_ciphertext=env.crypto.encrypt('changed'), approved=True))
        elif kind == 'programme':
            app.opportunity.programme_group = 'yii'
        elif kind == 'target':
            app.opportunity.application_url = 'https://example.test/changed'
        elif kind == 'document_metadata':
            session.get(Document, app.selected_cover_letter_id).approved = False
        elif kind == 'document_bytes':
            from pathlib import Path
            Path(session.get(Document, app.selected_cover_letter_id).path).write_bytes(b'Changed bytes')


@pytest.mark.parametrize('kind', ['profile', 'answers', 'programme', 'target', 'document_metadata', 'document_bytes'])
def test_server_fingerprint_detects_drift_before_execute(env, kind):
    seed_document(env)
    app_id = seed_application(env, cover_letter_required=True)
    service = bridge(env, execute=successful_executor(env))
    service.approve(app_id, app_id, 60)
    drift(env, app_id, kind)
    result = service.dispatch('request_approved_preparation', {'idempotency_key': 'drift_key'})
    assert result['reason'] == 'DRIFT'
    assert env.calls == []


@pytest.mark.parametrize('interruption', ['pause', 'revoke', 'expiry', 'drift'])
def test_mutation_guard_rechecks_midrun_controls_and_latches_refusal(env, interruption):
    app_id = seed_application(env)
    seen = []
    grant = []
    operator = bridge(env)
    def execute(application_id, guard):
        assert guard() is True
        seen.append('first_mutation')
        if interruption == 'pause':
            operator.dispatch('pause_preparation', {'reason_code': 'OPERATOR_REQUESTED'})
        elif interruption == 'revoke':
            operator.revoke(grant[0])
        elif interruption == 'expiry':
            env.now[0] += 61
        else:
            drift(env, app_id, 'profile')
        with pytest.raises(Exception):
            guard()
        return {'state': 'NEEDS_USER', 'error': 'raw-secret-path'}
    service = bridge(env, execute=execute)
    grant.append(service.approve(app_id, app_id, 60))
    result = service.dispatch('request_approved_preparation', {'idempotency_key': 'interrupt_key'})
    assert result['reason'] == {'pause': 'PAUSED', 'revoke': 'REVOKED', 'expiry': 'EXPIRED', 'drift': 'DRIFT'}[interruption]
    assert seen == ['first_mutation']
    assert 'raw-secret-path' not in str(result)


def test_revocation_does_not_wait_for_main_database_writer(env):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import text
    app_id = seed_application(env)
    operator = bridge(env)
    grant = operator.approve(app_id, app_id, 60)
    with env.database.engine.begin() as connection:
        connection.execute(text("UPDATE applications SET next_action='held writer' WHERE id=:id"), {'id': app_id})
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(operator.revoke, grant).result(timeout=2)
    assert operator.dispatch('request_approved_preparation', {'idempotency_key': 'revoked_key'})['reason'] == 'REVOKED'


def test_global_navigator_handoff_blocks_other_application(env):
    from uuid import uuid4
    app_id = seed_application(env)
    service = bridge(env, execute=successful_executor(env))
    service.approve(app_id, app_id, 60)
    env.nav.sessions.append(SimpleNamespace(session_id=str(uuid4()), application_id=str(uuid4()), worker_alive=True, cleanup_complete=False, state='HUMAN_REQUIRED', mode='review', headed=True))
    assert service.dispatch('request_approved_preparation', {'idempotency_key': 'global_key'})['reason'] == 'BUSY'
    assert not env.calls


@pytest.mark.parametrize('same_key', [True, False])
def test_concurrent_clients_claim_atomically_before_browser_without_long_transaction(env, same_key):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    app_id = seed_application(env)
    entered, release = Event(), Event()
    def execute(application_id, guard):
        assert guard() is True
        env.calls.append(application_id)
        entered.set()
        assert release.wait(5)
        return {'state': 'BLOCKED'}
    first = bridge(env, execute=execute)
    first.approve(app_id, app_id, 60)
    first.approve(app_id, app_id, 60)
    second = bridge(env, execute=execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(first.dispatch, 'request_approved_preparation', {'idempotency_key': 'concurrent_key'})
        assert entered.wait(3)
        try:
            other = pool.submit(second.dispatch, 'request_approved_preparation', {'idempotency_key': 'concurrent_key' if same_key else 'different_key'}).result(timeout=2)
            assert other['status'] == ('RUNNING' if same_key else 'BUSY')
        finally:
            release.set()
        result = future.result(timeout=3)
    assert env.calls == [app_id]
    assert second.dispatch('request_approved_preparation', {'idempotency_key': 'concurrent_key'}) == result


def test_startup_interrupts_orphan_without_retry_and_old_guard_cannot_resume(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    app_id = seed_application(env)
    entered, release = Event(), Event()
    def execute(application_id, guard):
        assert guard() is True
        env.calls.append(application_id)
        entered.set()
        assert release.wait(5)
        with pytest.raises(Exception):
            guard()
        return {'state': 'NEEDS_USER'}
    first = bridge(env, execute=execute)
    first.approve(app_id, app_id, 60)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first.dispatch, 'request_approved_preparation', {'idempotency_key': 'orphan_key'})
        assert entered.wait(3)
        try:
            restarted = bridge(env)
            restarted.start()
            old = restarted.dispatch('request_approved_preparation', {'idempotency_key': 'orphan_key'})
            assert (old['status'], old['reason']) == ('INTERRUPTED', 'INTERRUPTED')
            assert restarted.dispatch('get_preparation_readiness', {})['status'] == 'PAUSED'
            restarted.clear_pause()
        finally:
            release.set()
        assert future.result(timeout=3)['status'] == 'INTERRUPTED'
    assert env.calls == [app_id]


@pytest.mark.parametrize('tamper', ['receipt', 'submitted', 'submitted_flag', 'boundary_flag', 'click', 'nested_click', 'persisted_receipt', 'persisted_submitted'])
def test_submission_evidence_is_integrity_pause_never_success(env, tamper):
    from app.models import AutomationRun
    app_id = seed_application(env)
    success = successful_executor(env)
    def execute(application_id, guard):
        result = success(application_id, guard)
        if tamper == 'receipt':
            result['receipt'] = {'confirmation': 'private-receipt'}
        elif tamper == 'submitted':
            result['state'] = 'SUBMITTED'
        elif tamper == 'submitted_flag':
            result['submitted'] = True
        elif tamper == 'boundary_flag':
            result['click_boundary_crossed'] = True
        elif tamper == 'click':
            result['submit_clicked'] = True
        elif tamper == 'nested_click':
            result['diagnostics'] = {'final_click_attempted': True}
        else:
            with env.database.session_scope() as session:
                run = session.get(AutomationRun, result['run_id'])
                if tamper == 'persisted_receipt':
                    run.receipt_json = '{"receipt":"private-receipt"}'
                else:
                    run.state = 'SUBMITTED'
        return result
    service = bridge(env, execute=execute)
    service.approve(app_id, app_id, 60)
    result = service.dispatch('request_approved_preparation', {'idempotency_key': 'integrity_key'})
    assert result['reason'] == 'INTEGRITY'
    assert result['status'] == 'BLOCKED'
    assert service.dispatch('get_preparation_readiness', {})['status'] == 'PAUSED'
    assert 'private-receipt' not in str(result)


@pytest.mark.parametrize('tamper', ['missing_run', 'wrong_run_application', 'wrong_mode', 'closed_handoff', 'no_handoff', 'expired_handoff', 'wrong_run_state'])
def test_handoff_success_requires_underlying_correlated_run_and_live_navigator(env, tamper):
    from dataclasses import replace
    from datetime import datetime, timezone
    from uuid import uuid4
    from app.models import AutomationRun
    app_id = seed_application(env)
    other_id = seed_application(env, employer='Unrelated', url='https://example.test/other')
    success = successful_executor(env)
    def execute(application_id, guard):
        result = success(application_id, guard)
        if tamper == 'missing_run':
            result['run_id'] = str(uuid4())
        elif tamper in {'wrong_run_application', 'wrong_mode', 'wrong_run_state'}:
            with env.database.session_scope() as session:
                run = session.get(AutomationRun, result['run_id'])
                if tamper == 'wrong_mode':
                    run.mode = 'submit'
                elif tamper == 'wrong_run_state':
                    run.state = 'QUEUED'
                else:
                    # Reuse a second application created before approval.
                    run.application_id = other_id
        elif tamper == 'closed_handoff':
            env.nav.sessions[0] = replace(env.nav.sessions[0], worker_alive=False, cleanup_complete=True)
        elif tamper == 'expired_handoff':
            env.nav.sessions[0] = replace(env.nav.sessions[0], expires_at=datetime.fromtimestamp(env.now[0]-1, timezone.utc))
        else:
            env.nav.sessions.clear()
        return result
    service = bridge(env, execute=execute)
    service.approve(app_id, app_id, 60)
    result = service.dispatch('request_approved_preparation', {'idempotency_key': 'correlation_key'})
    assert result['status'] == 'FAILED'
    assert result['reason'] == 'EXECUTION_FAILED'


def test_no_grant_refuses_without_execution(env):
    service = bridge(env)
    service.start()
    result = service.dispatch('request_approved_preparation', {'idempotency_key': 'first_key'})
    assert result == {'protocol': 'argus.preparation.v1', 'status': 'REFUSED', 'reason': 'NO_APPROVAL', 'run_id': None, 'pending': 0, 'handoffs': [], 'next_cursor': None}
    assert env.calls == []


def test_existing_route_json_refusal_is_blocked_and_private(env):
    from fastapi.responses import JSONResponse
    app_id = seed_application(env)
    service = bridge(env, execute=lambda app, guard: JSONResponse({'message': 'private-path', 'submitted': False}, status_code=409))
    service.approve(app_id, app_id, 60)
    result = service.dispatch('request_approved_preparation', {'idempotency_key': 'route_refusal_key'})
    assert (result['status'], result['reason']) == ('BLOCKED', 'NOT_READY')
    assert 'private-path' not in str(result)


def test_runtime_start_is_exclusive_but_operator_store_open_is_not_restart(env):
    service = bridge(env)
    service.start()
    service.start()
    other = bridge(env)
    assert other.dispatch('get_preparation_readiness', {})['status'] == 'READY'
    with pytest.raises(ValueError, match='BUSY'):
        other.start()


def test_pause_revokes_pending_grants_even_after_operator_clear(env):
    app_id = seed_application(env)
    service = bridge(env)
    service.approve(app_id, app_id, 60)
    service.dispatch('pause_preparation', {'reason_code': 'OPERATOR_REQUESTED'})
    service.clear_pause()
    assert service.dispatch('request_approved_preparation', {'idempotency_key': 'pause_clear_key'})['reason'] == 'REVOKED'


def test_credentials_are_separate_expiring_rotatable_and_hash_only(env, tmp_path):
    import json
    service = bridge(env)
    assert service.authenticate('generic-test-token') is False
    first = tmp_path / 'first.json'
    service.provision(first, 'http://127.0.0.1:8765', 60)
    payload = json.loads(first.read_text())
    assert set(payload) == {'endpoint', 'token', 'expires_at'}
    assert service.authenticate(payload['token']) is True
    assert bridge(env).authenticate(payload['token']) is True
    assert payload['token'].encode() not in (env.settings.data_dir / 'preparation_bridge.sqlite3').read_bytes()
    second = tmp_path / 'second.json'
    service.provision(second, 'http://127.0.0.1:8765', 60)
    next_payload = json.loads(second.read_text())
    assert service.authenticate(payload['token']) is False
    assert service.authenticate(next_payload['token']) is True
    env.now[0] += 61
    assert service.authenticate(next_payload['token']) is False
    assert bridge(env, enabled=False).authenticate(next_payload['token']) is False
    with pytest.raises(ValueError):
        service.provision(tmp_path / 'unsafe.json', 'https://example.test', 60)
    assert not (tmp_path / 'unsafe.json').exists()


def test_handoff_listing_uses_opaque_cursor_and_closed_projection(env):
    service = bridge(env, execute=successful_executor(env))
    app_id = seed_application(env)
    service.approve(app_id, app_id, 60)
    result = service.dispatch('request_approved_preparation', {'idempotency_key': 'listing_key'})
    listing = service.dispatch('list_preparation_handoffs', {'after_cursor': None, 'limit': 1})
    assert listing['handoffs'] == [{'run_id': result['run_id'], 'status': 'PREFILLED_HANDOFF', 'reason': 'HANDOFF'}]
    assert listing['next_cursor'] is None
    assert service.dispatch('list_preparation_handoffs', {'after_cursor': result['run_id'], 'limit': 1})['handoffs'] == []


@pytest.mark.parametrize('operation,body', [
    ('get_preparation_readiness', {'extra': 1}),
    ('pause_preparation', {'reason_code': 'arbitrary-secret'}),
    ('request_approved_preparation', {'idempotency_key': 'short'}),
    ('request_approved_preparation', {'idempotency_key': 'correct_key', 'application_id': 'private'}),
    ('list_preparation_handoffs', {'after_cursor': None, 'limit': True}),
    ('get_preparation_run', {'run_id': 'private'}),
    ('approve', {}),
])
def test_dispatch_rejects_malformed_request_without_reflection(env, operation, body):
    result = bridge(env).dispatch(operation, body)
    assert result['reason'] == 'INVALID_REQUEST'
    assert not env.calls


def test_disabled_constructor_does_not_create_bridge_tables_or_file(env):
    before = inspect(env.database.engine).get_table_names()
    service = bridge(env, enabled=False)
    service.start()
    assert service.dispatch('get_preparation_readiness', {})['status'] == 'DISABLED'
    assert inspect(env.database.engine).get_table_names() == before
    assert not (env.settings.data_dir / 'preparation_bridge.sqlite3').exists()
