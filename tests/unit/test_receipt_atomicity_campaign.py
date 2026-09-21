"""Strict receipt evidence and terminal records share the request transaction."""
from dataclasses import asdict
from types import SimpleNamespace
import pytest

from app.automation.runner import AutomationRunner
from app.models import Application, SubmissionIntent
from app.services.submission_intents import SubmissionIntentService
from tests.unit.test_submission_intents import _database, _seed
from tests.unit.test_receipts import _evidence


@pytest.mark.parametrize('fault', [None, RuntimeError, SystemExit])
def test_final_receipt_commit_is_atomic(tmp_path, monkeypatch, fault):
    db = _database(tmp_path)
    _, application_id = _seed(db)
    with db.session_scope() as session:
        service = SubmissionIntentService(session)
        intent = service.prepare_intent(application_id=application_id, attempt_id='atomic-receipt',
            manifest={'role':'Summer Analyst'}, destination_url='https://jobs.example.test/api/apply')
        service.mark_clicked(intent)
        intent_id, nonce = intent.id, intent.nonce
    before = _evidence(request_id='', response_request_id='', bound_intent_id=intent_id, bound_intent_nonce=nonce)
    after = _evidence(request_id='fresh-confirm', response_request_id='fresh-confirm',
        reference='ARG-5678', confirmation_text='Application submitted',
        bound_intent_id=intent_id, bound_intent_nonce=nonce)
    target = dict(target_fingerprint='target-1', control_fingerprint='control-1', page_id='page-1',
        frame_url='https://jobs.example.test/apply', root_selector='#application', control_selector='#submit',
        form_action='https://jobs.example.test/api/apply', method='POST',
        destination='https://jobs.example.test/confirmation', provider='example')
    result = {'receipt_evidence':{'before':asdict(before),'after':asdict(after),'bound_target':target},
              'expected_final_url':'https://jobs.example.test/confirmation'}
    reached = []
    runner = object.__new__(AutomationRunner)
    try:
        with db.SessionLocal() as session:
            application = session.get(Application, application_id)
            intent = session.get(SubmissionIntent, intent_id)
            # Match the existing request transaction, already writing an AutomationRun.
            application.next_action = 'synthetic request transaction'
            session.flush()
            def fail_commit():
                reached.append((application.state, intent.status))
                raise fault('synthetic commit boundary')
            if fault is not None:
                monkeypatch.setattr(session, 'commit', fail_commit)
                with pytest.raises(fault, match='synthetic commit boundary'):
                    runner._finalize_confirmed_receipt(session, application, intent, result, SimpleNamespace(reference='ARG-5678'))
                assert reached == [('CONFIRMATION_VERIFIED', 'CONFIRMED')]
                session.rollback()
            else:
                runner._finalize_confirmed_receipt(session, application, intent, result, SimpleNamespace(reference='ARG-5678'))
        with db.SessionLocal() as session:
            application = session.get(Application, application_id)
            intent = session.get(SubmissionIntent, intent_id)
            assert (application.state, intent.status) == (
                ('CONFIRMATION_VERIFIED','CONFIRMED') if fault is None else ('READY_TO_SUBMIT','CLICKED'))
            assert not SubmissionIntentService(session).can_start(application_id)
            if fault is None:
                assert application.submission_reference == 'ARG-5678'
            else:
                assert not application.submission_reference
    finally:
        db.engine.dispose()
