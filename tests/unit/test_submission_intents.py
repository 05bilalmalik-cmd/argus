# TDD: R7+R8 — durable submission intents, SUBMISSION_UNKNOWN, correlated
# receipts. The final click must be preceded by an atomically persisted
# intent; a post-click local failure must yield SUBMISSION_UNKNOWN with no
# automatic retry; a receipt must correlate to the exact click.
from __future__ import annotations

import json
import multiprocessing
import sqlite3
import threading

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, Opportunity, SubmissionIntent


def _process_click_worker(data_dir: str, intent_id: str, gate, results) -> None:
    """Attempt one exact click from an independent spawned process."""

    settings = Settings.load({"ARGUS_DATA_DIR": data_dir, "ARGUS_API_TOKEN": "t"})
    database = Database(settings)
    session = database.SessionLocal()
    try:
        intent = session.get(SubmissionIntent, intent_id)
        assert intent is not None
        gate.wait(timeout=15)

        from app.services.submission_intents import IntentStateError, SubmissionIntentService

        effects: list[str] = []
        try:
            SubmissionIntentService(session).execute_click(
                intent,
                lambda: effects.append("A"),
            )
        except IntentStateError:
            results.put(("loser", effects))
        else:
            results.put(("winner", effects))
    finally:
        session.close()
        database.engine.dispose()


def _database(tmp_path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path / "d"), "ARGUS_API_TOKEN": "t"})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return database


def _seed(database) -> tuple[str, str]:
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Intent Bank",
            role_title="Summer Analyst",
            cycle="2027",
            url="https://boards.example.com/intent/1",
            source="test",
            cv_required=False,
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.READY_TO_SUBMIT.value,
        )
        session.add(application)
        session.flush()
        return opportunity.id, application.id


def test_submission_intent_model_exists_and_round_trips(tmp_path):
    database = _database(tmp_path)
    _, application_id = _seed(database)
    with database.session_scope() as session:
        intent = SubmissionIntent(
            application_id=application_id,
            attempt_id="attempt-1",
            nonce="nonce-1",
            manifest_json=json.dumps({"employer": "Intent Bank"}),
            destination_url="https://boards.example.com/intent/1",
        )
        session.add(intent)
        session.flush()
        stored = session.get(SubmissionIntent, intent.id)
    assert stored is not None
    assert stored.status == "PENDING"  # default before click
    assert stored.manifest_fingerprint  # derived at construction


def test_one_terminal_intent_per_application(tmp_path):
    database = _database(tmp_path)
    _, application_id = _seed(database)
    with database.session_scope() as session:
        first = SubmissionIntent(
            application_id=application_id,
            attempt_id="a1",
            nonce="n1",
            manifest_json="{}",
            destination_url="https://x.example.com/1",
        )
        session.add(first)
        session.flush()
    # A second intent for the same application must be refused by the model
    # helper while the first is not terminal-failed.
    from app.services.submission_intents import SubmissionIntentService

    with database.session_scope() as session:
        service = SubmissionIntentService(session)
        with pytest.raises(Exception, match="terminal"):
            service.create_intent(
                application_id=application_id,
                attempt_id="a2",
                manifest={"employer": "Intent Bank"},
                destination_url="https://x.example.com/1",
            )


def test_unknown_state_exists_in_domain():
    # SUBMISSION_UNKNOWN must be a real state with honest semantics:
    # reachable from SUBMITTED when evidence fails, never auto-retried.
    from app.domain.states import ApplicationState, validate_transition

    assert ApplicationState.SUBMISSION_UNKNOWN.value == "SUBMISSION_UNKNOWN"
    validate_transition(ApplicationState.SUBMITTED, ApplicationState.SUBMISSION_UNKNOWN)


def test_receipt_requires_click_correlation():
    from app.automation.receipts import ReceiptEvidence, receipt_is_correlated

    baseline = ReceiptEvidence(
        url_before_click="https://x.example.com/form",
        dom_had_reference=False,
        dom_confirmation_text_present=False,
        baseline_captured=True,
        bound_target_fingerprint="target-1",
        bound_intent_id="intent-1",
        bound_intent_nonce="nonce-1",
        page_id="page-1",
        frame_url="https://x.example.com/form",
        root_selector="#application",
        control_selector="#submit",
        control_fingerprint="target-1",
        target_fingerprint="target-1",
        form_action="https://x.example.com/api/submit",
        target_method="POST",
        target_url="https://x.example.com/confirmation",
        destination="https://x.example.com/confirmation",
        provider="example",
        request_url="https://x.example.com/api/submit",
    )
    after = ReceiptEvidence(
        url_before_click="https://x.example.com/form",
        dom_had_reference=True,
        dom_confirmation_text_present=True,
        final_url="https://x.example.com/confirmation",
        baseline_captured=True,
        bound_target_fingerprint="target-1",
        bound_intent_id="intent-1",
        bound_intent_nonce="nonce-1",
        page_id="page-1",
        frame_url="https://x.example.com/form",
        root_selector="#application",
        control_selector="#submit",
        control_fingerprint="target-1",
        target_fingerprint="target-1",
        form_action="https://x.example.com/api/submit",
        target_method="POST",
        target_url="https://x.example.com/confirmation",
        destination="https://x.example.com/confirmation",
        provider="example",
        request_url="https://x.example.com/api/submit",
        request_method="POST",
        response_url="https://x.example.com/api/submit",
        response_status=201,
        request_id="request-1",
        response_request_id="request-1",
        reference="ARG-1234",
        confirmation_text="Application submitted",
    )
    # A reference that ALREADY existed pre-click is not evidence.
    preexisting = ReceiptEvidence(
        url_before_click="https://x.example.com/form",
        dom_had_reference=True,
        dom_confirmation_text_present=True,
        reference_seen_before_click=True,
        baseline_captured=True,
        bound_target_fingerprint="target-1",
        bound_intent_id="intent-1",
        bound_intent_nonce="nonce-1",
    )
    assert receipt_is_correlated(
        baseline,
        after,
        bound_target={
            "target_fingerprint": "target-1",
            "control_fingerprint": "target-1",
            "page_id": "page-1",
            "frame_url": "https://x.example.com/form",
            "root_selector": "#application",
            "control_selector": "#submit",
            "form_action": "https://x.example.com/api/submit",
            "method": "POST",
            "destination": "https://x.example.com/confirmation",
            "provider": "example",
        },
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
        navigation_only=False,
    )
    assert not receipt_is_correlated(
        baseline,
        preexisting,
        bound_target="target-1",
        bound_intent={"id": "intent-1", "nonce": "nonce-1"},
        navigation_only=False,
    )
    # Navigation-only change (intermediate Workday step) is NOT a receipt.
    assert not receipt_is_correlated(baseline, after, navigation_only=True)


def test_service_prepares_durably_and_refuses_duplicate_intent(tmp_path):
    from app.services.submission_intents import IntentExistsError, SubmissionIntentService

    database = _database(tmp_path)
    _, application_id = _seed(database)
    manifest = {"employer": "Intent Bank", "role": "Summer Analyst"}
    with database.session_scope() as session:
        intent = SubmissionIntentService(session).prepare_intent(
            application_id=application_id,
            attempt_id="prepared-1",
            manifest=manifest,
            destination_url="https://x.example.com/apply",
        )
        assert intent.status == "PREPARED"

    with database.session_scope() as session:
        stored = session.scalar(
            select(SubmissionIntent).where(SubmissionIntent.application_id == application_id)
        )
        assert stored is not None
        assert stored.status == "PREPARED"
        with pytest.raises(IntentExistsError):
            SubmissionIntentService(session).prepare_intent(
                application_id=application_id,
                attempt_id="prepared-2",
                manifest=manifest,
                destination_url="https://x.example.com/apply",
            )


def test_click_boundary_is_one_shot_and_duplicate_click_is_refused(tmp_path):
    from app.services.submission_intents import IntentStateError, SubmissionIntentService

    database = _database(tmp_path)
    _, application_id = _seed(database)
    with database.session_scope() as session:
        service = SubmissionIntentService(session)
        intent = service.prepare_intent(
            application_id=application_id,
            attempt_id="click-1",
            manifest={"role": "Summer Analyst"},
            destination_url="https://x.example.com/apply",
        )
        seen: list[str] = []
        assert service.execute_click(intent, lambda: seen.append("one")) is None
        assert seen == ["one"]
        assert intent.status == "CLICKED"
        with pytest.raises(IntentStateError):
            service.execute_click(intent, lambda: seen.append("duplicate"))
        assert seen == ["one"]


def test_post_click_callback_crash_marks_unknown_and_loopback_side_effect_is_not_retried(tmp_path):
    from app.services.submission_intents import SubmissionIntentService, SubmissionUncertainError

    database = _database(tmp_path)
    _, application_id = _seed(database)
    side_effects: list[str] = []
    with database.session_scope() as session:
        service = SubmissionIntentService(session)
        intent = service.prepare_intent(
            application_id=application_id,
            attempt_id="crash-1",
            manifest={"role": "Summer Analyst"},
            destination_url="https://x.example.com/apply",
        )

        def loopback_submit() -> None:
            side_effects.append("POST")
            raise TimeoutError("page disappeared after request")

        with pytest.raises(SubmissionUncertainError):
            service.execute_click(intent, loopback_submit)
        assert side_effects == ["POST"]
        assert intent.status == "UNKNOWN"

    with database.session_scope() as session:
        stored = session.scalar(
            select(SubmissionIntent).where(SubmissionIntent.application_id == application_id)
        )
        assert stored is not None and stored.status == "UNKNOWN"
        state_row = session.get(Application, application_id)
        assert state_row is not None
        assert state_row.state == ApplicationState.SUBMISSION_UNKNOWN.value
        with pytest.raises(Exception):
            SubmissionIntentService(session).prepare_intent(
                application_id=application_id,
                attempt_id="crash-duplicate",
                manifest={"role": "Summer Analyst"},
                destination_url="https://x.example.com/apply",
            )


def test_two_sessions_cas_acquire_click_once_and_stale_session_has_no_external_effect(tmp_path):
    from app.services.submission_intents import IntentStateError, SubmissionIntentService

    database = _database(tmp_path)
    _, application_id = _seed(database)
    seed_session = database.SessionLocal()
    try:
        intent = SubmissionIntentService(seed_session).prepare_intent(
            application_id=application_id,
            attempt_id="cas-1",
            manifest={"role": "Summer Analyst"},
            destination_url="https://x.example.com/apply",
        )
        intent_id = intent.id
    finally:
        seed_session.close()

    session_a = database.SessionLocal()
    session_b = database.SessionLocal()
    effects: list[str] = []
    try:
        stale_b = session_b.get(SubmissionIntent, intent_id)
        current_a = session_a.get(SubmissionIntent, intent_id)
        assert stale_b is not None and current_a is not None
        SubmissionIntentService(session_a).execute_click(
            current_a, lambda: effects.append("A")
        )
        with pytest.raises(IntentStateError):
            SubmissionIntentService(session_b).execute_click(
                stale_b, lambda: effects.append("B")
            )
        assert effects == ["A"]
        assert stale_b.status == "CLICKED"
    finally:
        session_a.close()
        session_b.close()


def test_two_threads_racing_click_cas_produce_one_external_effect(tmp_path):
    from app.services.submission_intents import IntentStateError, SubmissionIntentService

    database = _database(tmp_path)
    _, application_id = _seed(database)
    with database.session_scope() as session:
        prepared = SubmissionIntentService(session).prepare_intent(
            application_id=application_id,
            attempt_id="thread-race",
            manifest={"role": "Summer Analyst"},
            destination_url="https://x.example.com/apply",
        )
        intent_id = prepared.id

    sessions = [database.SessionLocal(), database.SessionLocal()]
    intents = [session.get(SubmissionIntent, intent_id) for session in sessions]
    barrier = threading.Barrier(2)
    effects: list[str] = []
    errors: list[Exception] = []
    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            SubmissionIntentService(sessions[index]).execute_click(
                intents[index],
                lambda: effects.append("A"),
            )
        except IntentStateError as exc:
            errors.append(exc)
        finally:
            sessions[index].close()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert effects == ["A"]
    assert len(errors) == 1


def test_two_spawned_processes_racing_click_have_one_external_effect(tmp_path):
    from app.services.submission_intents import SubmissionIntentService

    database = _database(tmp_path)
    _, application_id = _seed(database)
    with database.session_scope() as session:
        prepared = SubmissionIntentService(session).prepare_intent(
            application_id=application_id,
            attempt_id="process-race",
            manifest={"role": "Summer Analyst"},
            destination_url="https://x.example.com/apply",
        )
        intent_id = prepared.id
    database.engine.dispose()

    context = multiprocessing.get_context("spawn")
    gate = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_click_worker,
            args=(str(tmp_path / "d"), intent_id, gate, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert all(process.exitcode == 0 for process in processes)

    outcomes = [results.get(timeout=5) for _ in processes]
    assert [effect for _status, effects in outcomes for effect in effects] == ["A"]
    assert {status for status, _effects in outcomes} == {"winner", "loser"}


def test_stale_prepared_object_cannot_overwrite_unknown_or_confirmed(tmp_path):
    from app.services.submission_intents import IntentStateError, SubmissionIntentService

    database = _database(tmp_path)
    _, application_id = _seed(database)
    with database.session_scope() as session:
        prepared = SubmissionIntentService(session).prepare_intent(
            application_id=application_id,
            attempt_id="stale-1",
            manifest={"role": "Summer Analyst"},
            destination_url="https://x.example.com/apply",
        )
        intent_id = prepared.id

    current_session = database.SessionLocal()
    stale_session = database.SessionLocal()
    try:
        current = current_session.get(SubmissionIntent, intent_id)
        stale = stale_session.get(SubmissionIntent, intent_id)
        assert current is not None and stale is not None
        current_service = SubmissionIntentService(current_session)
        stale_service = SubmissionIntentService(stale_session)
        current_service.mark_clicked(current)
        current_service.mark_unknown(current)
        with pytest.raises(IntentStateError):
            stale_service.mark_clicked(stale)
        assert stale.status == "UNKNOWN"
    finally:
        current_session.close()
        stale_session.close()


def test_stale_object_cannot_overwrite_confirmed_after_receipt_cas(tmp_path):
    from app.automation.receipts import receipt_is_correlated
    from app.services.submission_intents import IntentStateError, SubmissionIntentService
    from tests.unit.test_receipts import _evidence

    database = _database(tmp_path)
    _, application_id = _seed(database)
    with database.session_scope() as session:
        prepared = SubmissionIntentService(session).prepare_intent(
            application_id=application_id,
            attempt_id="stale-confirmed",
            manifest={"role": "Summer Analyst"},
            destination_url="https://jobs.example.test/api/apply",
        )
        intent_id = prepared.id

    current_session = database.SessionLocal()
    stale_session = database.SessionLocal()
    try:
        current = current_session.get(SubmissionIntent, intent_id)
        stale = stale_session.get(SubmissionIntent, intent_id)
        assert current is not None and stale is not None
        current_service = SubmissionIntentService(current_session)
        current_service.mark_clicked(current)
        before = _evidence(
            request_id="",
            response_request_id="",
            bound_intent_id=current.id,
            bound_intent_nonce=current.nonce,
        )
        after = _evidence(
            request_id="fresh-confirm",
            response_request_id="fresh-confirm",
            reference="ARG-5678",
            confirmation_text="Application submitted",
            form_action="https://jobs.example.test/api/apply",
            request_url="https://jobs.example.test/api/apply",
            response_url="https://jobs.example.test/api/apply",
            bound_intent_id=current.id,
            bound_intent_nonce=current.nonce,
        )
        assert receipt_is_correlated(
            before,
            after,
            bound_target={
                "target_fingerprint": "target-1",
                "control_fingerprint": "control-1",
                "page_id": "page-1",
                "frame_url": "https://jobs.example.test/apply",
                "root_selector": "#application",
                "control_selector": "#submit",
                "form_action": "https://jobs.example.test/api/apply",
                "method": "POST",
                "destination": "https://jobs.example.test/confirmation",
                "provider": "example",
            },
            bound_intent=current,
        )
        current_service.confirm_with_receipt(
            current,
            before,
            after,
            bound_target={
                "target_fingerprint": "target-1",
                "control_fingerprint": "control-1",
                "page_id": "page-1",
                "frame_url": "https://jobs.example.test/apply",
                "root_selector": "#application",
                "control_selector": "#submit",
                "form_action": "https://jobs.example.test/api/apply",
                "method": "POST",
                "destination": "https://jobs.example.test/confirmation",
                "provider": "example",
            },
        )
        with pytest.raises(IntentStateError):
            SubmissionIntentService(stale_session).mark_clicked(stale)
        assert stale.status == "CONFIRMED"
    finally:
        current_session.close()
        stale_session.close()


def test_confirmation_bypass_and_non_durable_shortcuts_are_denied(tmp_path):
    from app.services.submission_intents import IntentStateError, SubmissionIntentService

    database = _database(tmp_path)
    _, application_id = _seed(database)
    with database.session_scope() as session:
        service = SubmissionIntentService(session)
        intent = service.prepare_intent(
            application_id=application_id,
            attempt_id="strict-1",
            manifest={"role": "Summer Analyst"},
            destination_url="https://x.example.com/apply",
        )
        with pytest.raises(IntentStateError):
            service.mark_confirmed(intent, correlated=True)
        with pytest.raises(IntentStateError):
            service.mark_clicked(intent, durable=False)
        with pytest.raises(IntentStateError):
            service.mark_unknown(intent, durable=False)


def test_confirm_with_receipt_refuses_incomplete_target(tmp_path):
    from app.automation.receipts import ReceiptEvidence
    from app.services.submission_intents import IntentStateError, SubmissionIntentService

    database = _database(tmp_path)
    _, application_id = _seed(database)
    with database.session_scope() as session:
        service = SubmissionIntentService(session)
        intent = service.prepare_intent(
            application_id=application_id,
            attempt_id="incomplete-target",
            manifest={"role": "Summer Analyst"},
            destination_url="https://x.example.com/apply",
        )
        service.mark_clicked(intent)
        evidence = ReceiptEvidence(
            url_before_click="https://x.example.com/apply",
            baseline_captured=True,
        )
        with pytest.raises(IntentStateError):
            service.confirm_with_receipt(intent, evidence, evidence)


def test_confirm_requires_correlated_receipt_and_failed_local_is_only_pre_click(tmp_path):
    from app.automation.receipts import ReceiptEvidence
    from app.services.submission_intents import IntentStateError, SubmissionIntentService

    database = _database(tmp_path)
    _, application_id = _seed(database)
    with database.session_scope() as session:
        service = SubmissionIntentService(session)
        intent = service.prepare_intent(
            application_id=application_id,
            attempt_id="confirm-1",
            manifest={"role": "Summer Analyst"},
            destination_url="https://x.example.com/apply",
        )
        service.mark_failed_local(intent)
        assert intent.status == "FAILED_LOCAL"

    with database.session_scope() as session:
        service = SubmissionIntentService(session)
        intent = service.prepare_intent(
            application_id=application_id,
            attempt_id="confirm-2",
            manifest={"role": "Summer Analyst"},
            destination_url="https://x.example.com/apply",
        )
        service.mark_clicked(intent)
        bad = ReceiptEvidence(
            url_before_click="https://x.example.com/apply",
            dom_had_reference=True,
            dom_confirmation_text_present=True,
            final_url="https://x.example.com/apply",
        )
        with pytest.raises(IntentStateError):
            service.confirm_with_receipt(intent, bad, bad, bound_target="target", bound_intent=intent)
        assert intent.status == "CLICKED"
        with pytest.raises(IntentStateError):
            service.mark_failed_local(intent)
