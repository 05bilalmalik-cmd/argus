"""Reviewer probes for the durable audit-chain safety contract."""
from __future__ import annotations

from multiprocessing import get_context

from sqlalchemy import func, select, update

import pytest

from app.db import Database
from app.models import Application, AuditChainState, AuditEvent, AuditOutbox, Opportunity
from app.config import Settings
from app.security import audit as audit_module
from app.security.audit import AuditDrainError, AuditInput, append_audit, drain_audit_outbox, verify_audit_chain
from tests.integration.test_audit_concurrency import _database


def _seed_application(database):
    with database.SessionLocal() as session:
        opportunity = Opportunity(
            employer="Review Employer",
            role_title="Review Internship",
            cycle="2027",
            url="https://review.example/role",
        )
        session.add(opportunity)
        session.flush()
        application = Application(opportunity_id=opportunity.id, state="DISCOVERED")
        session.add(application)
        session.commit()
        return application.id


def _queue_without_auto_drain(database, monkeypatch, audit_input):
    monkeypatch.setattr(audit_module, "drain_audit_outbox", lambda *_args, **_kwargs: None)
    with database.SessionLocal() as session:
        append_audit(session, audit_input)
        session.commit()


def test_deleting_current_head_refuses_without_normalizing_durable_state(tmp_path):
    database = _database(tmp_path)
    with database.SessionLocal() as session:
        append_audit(session, AuditInput("review", "seed", "fixture", "1", {}))
        session.commit()

    with database.SessionLocal() as session:
        event_id = session.scalar(select(AuditEvent.id).order_by(AuditEvent.id.desc()))
        state_before = session.get(AuditChainState, 1)
        assert state_before is not None
        head_before = state_before.head_hash
        session.execute(
            update(AuditEvent).where(AuditEvent.id == event_id).values(details_json="{}")
        )
        # Deleting the tail is the decisive case: its remaining rows can still
        # look internally linear, but the durable state proves evidence was lost.
        session.execute(
            AuditEvent.__table__.delete().where(AuditEvent.id == event_id)
        )
        session.commit()

    # A normal business commit does not hide the damage, and the next drain
    # refuses to append to a state head whose surviving tail no longer agrees.
    with pytest.raises(AuditDrainError, match="chain state"):
        drain_audit_outbox(database.engine)

    with database.SessionLocal() as session:
        state_after = session.get(AuditChainState, 1)
        assert state_after is not None
        assert state_after.head_hash == head_before
        verification = verify_audit_chain(session)
        assert not verification.valid
        assert not verification.state_consistent


def test_nested_commit_does_not_drain_before_outer_rollback(tmp_path, monkeypatch):
    database = _database(tmp_path)
    application_id = _seed_application(database)
    calls: list[object] = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(audit_module, "drain_audit_outbox", spy)
    session = database.SessionLocal()
    try:
        outer = session.begin()
        application = session.get(Application, application_id)
        assert application is not None
        application.next_action = "rolled back business mutation"
        nested = session.begin_nested()
        append_audit(
            session,
            AuditInput("review", "application.changed", "application", application_id, {}),
        )
        nested.commit()
        assert calls == []
        outer.rollback()
    finally:
        session.close()

    with database.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AuditOutbox)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0
        persisted = session.get(Application, application_id)
        assert persisted is not None
        assert persisted.next_action == ""


def test_empty_root_rollback_after_nested_commit_discards_all_writes(tmp_path):
    """SAVEPOINT must never become SQLite's top-level transaction."""
    database = _database(tmp_path)
    session = database.SessionLocal()
    try:
        outer = session.begin()
        nested = session.begin_nested()
        opportunity = Opportunity(
            employer="Nested Root Employer",
            role_title="Nested Root Internship",
            cycle="2027",
            url="https://nested-root.example/role",
        )
        session.add(opportunity)
        append_audit(
            session,
            AuditInput("review", "nested.root", "opportunity", "nested", {}),
        )
        nested.commit()
        with pytest.raises(RuntimeError, match="outer rollback"):
            raise RuntimeError("outer rollback")
    finally:
        # The production scenario raises out of the root transaction after the
        # savepoint is released; make the rollback boundary explicit here.
        outer.rollback()
        session.close()

    with database.SessionLocal() as fresh:
        nested_counts = (
            fresh.scalar(select(func.count()).select_from(Opportunity)),
            fresh.scalar(select(func.count()).select_from(AuditOutbox)),
            fresh.scalar(select(func.count()).select_from(AuditEvent)),
        )
        assert nested_counts == (0, 0, 0), f"NESTED_COUNTS {nested_counts}"


def test_nested_commit_then_outer_commit_emits_one_audit_event(tmp_path):
    database = _database(tmp_path)
    with database.SessionLocal() as session:
        outer = session.begin()
        nested = session.begin_nested()
        opportunity = Opportunity(
            employer="Nested Commit Employer",
            role_title="Nested Commit Internship",
            cycle="2027",
            url="https://nested-commit.example/role",
        )
        session.add(opportunity)
        append_audit(
            session,
            AuditInput("review", "nested.commit", "opportunity", "nested", {}),
        )
        nested.commit()
        outer.commit()

    with database.SessionLocal() as fresh:
        assert fresh.scalar(select(func.count()).select_from(Opportunity)) == 1
        assert fresh.scalar(select(func.count()).select_from(AuditOutbox)) == 1
        assert fresh.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert verify_audit_chain(fresh).valid


def test_nested_rollback_removes_only_savepoint_audit_intent(tmp_path, monkeypatch):
    database = _database(tmp_path)
    application_id = _seed_application(database)
    calls: list[object] = []
    monkeypatch.setattr(audit_module, "drain_audit_outbox", lambda *args, **kwargs: calls.append(args))

    with database.SessionLocal() as session:
        outer = session.begin()
        application = session.get(Application, application_id)
        assert application is not None
        nested = session.begin_nested()
        application.next_action = "savepoint-only mutation"
        append_audit(
            session,
            AuditInput("review", "application.changed", "application", application_id, {}),
        )
        nested.rollback()
        outer.commit()

    assert calls == []
    with database.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AuditOutbox)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0
        persisted = session.get(Application, application_id)
        assert persisted is not None
        assert persisted.next_action == ""


@pytest.mark.parametrize("column, value", [("intent_fingerprint", "not-json"), ("details_json", "not-json")])
def test_corrupt_pending_intent_stays_pending_and_emits_no_event(tmp_path, monkeypatch, column, value):
    database = _database(tmp_path)
    _queue_without_auto_drain(
        database,
        monkeypatch,
        AuditInput("review", "corrupt", "fixture", "1", {"safe": True}),
    )
    with database.engine.begin() as connection:
        connection.execute(
            update(AuditOutbox).where(AuditOutbox.status == AuditOutbox.PENDING).values(**{column: value})
        )

    with pytest.raises(AuditDrainError, match="intent"):
        drain_audit_outbox(database.engine)

    with database.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0
        assert session.scalar(
            select(func.count()).select_from(AuditOutbox).where(AuditOutbox.status == AuditOutbox.PENDING)
        ) == 1


def test_fresh_verifier_exposes_pending_intent_as_incomplete(tmp_path, monkeypatch):
    database = _database(tmp_path)
    _queue_without_auto_drain(
        database,
        monkeypatch,
        AuditInput("review", "pending", "fixture", "1", {}),
    )

    # A fresh session has no compatibility objects in its in-memory pending
    # list, so the durable outbox is the only evidence visible to verification.
    with database.SessionLocal() as session:
        verification = verify_audit_chain(session)
        assert verification.pending_count == 1
        assert verification.incomplete
        assert not verification.valid
        assert not verification.complete


def _verify_pending_in_process(data_dir: str, result_queue) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": data_dir, "ARGUS_API_TOKEN": "test-token"})
    database = Database(settings)
    database.create_schema()
    with database.SessionLocal() as session:
        verification = verify_audit_chain(session)
        result_queue.put(
            (
                verification.pending_count,
                verification.incomplete,
                verification.valid,
                verification.complete,
            )
        )


def test_fresh_process_verifier_reports_durable_pending_intent(tmp_path, monkeypatch):
    database = _database(tmp_path)
    _queue_without_auto_drain(
        database,
        monkeypatch,
        AuditInput("review", "pending.process", "fixture", "1", {}),
    )

    context = get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_verify_pending_in_process,
        args=(str(tmp_path / "data"), result_queue),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 0
    assert result_queue.get(timeout=5) == (1, True, False, False)
