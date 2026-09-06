# TDD: R9 — audit-chain concurrency.
# Two sessions appending concurrently must NEVER fork the chain. The old
# append_audit read the head outside any serialization and flushed without
# holding a lock through commit, forking at event 4819 in production.
from __future__ import annotations

import threading
from multiprocessing import get_context
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import Database
from app.models import Application, AuditEvent, AuditOutbox, Opportunity
from app.security import audit as audit_module
from app.security.audit import (
    AuditInput,
    AuditDrainError,
    append_audit,
    drain_audit_outbox,
    verify_audit_chain,
)


def _database(tmp_path) -> Database:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path / "data"),
            "ARGUS_API_TOKEN": "test-token",
        }
    )
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return database


def test_sequential_appends_form_valid_chain(tmp_path):
    database = _database(tmp_path)
    with database.session_scope() as session:
        for index in range(10):
            append_audit(
                session,
                AuditInput("t", "event", "entity", str(index), {"n": index}),
            )
    with database.SessionLocal() as session:
        verification = verify_audit_chain(session)
    assert verification.valid, verification


def test_two_sessions_appending_concurrently_never_fork(tmp_path):
    """100 repeated collision attempts: both sessions read the same head,
    then append + commit. Every finished run must verify valid."""
    for attempt in range(100):
        database = _database(tmp_path)
        # Seed one event so there is a non-empty head to collide on.
        with database.session_scope() as session:
            append_audit(session, AuditInput("seed", "seed", "entity", "0", {}))

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                barrier.wait(timeout=5)
                with database.session_scope() as session:
                    append_audit(
                        session,
                        AuditInput("racer", f"race-{n}", "entity", str(n), {"n": n}),
                    )
            except Exception as exc:  # noqa: BLE001 - collected below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert not errors, (attempt, errors)
        with database.SessionLocal() as session:
            verification = verify_audit_chain(session)
        assert verification.valid, (
            f"attempt {attempt}: forked at event {verification.broken_event_id}"
        )


def test_many_threads_still_linear(tmp_path):
    """8 concurrent writers, 5 rounds: chain stays linear and valid."""
    import random

    database = _database(tmp_path)
    failures = []
    for round_number in range(5):
        barrier = threading.Barrier(8)

        def worker(n: int) -> None:
            try:
                barrier.wait(timeout=10)
                with database.session_scope() as session:
                    append_audit(
                        session,
                        AuditInput(
                            "multi",
                            f"round-{round_number}",
                            "entity",
                            str(n),
                            {},
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert not failures, failures[:3]
    with database.SessionLocal() as session:
        verification = verify_audit_chain(session)
    assert verification.valid


def test_two_threads_can_queue_before_forced_commit_and_preserve_both_intents(tmp_path):
    """Queueing must not hold a process/global lock through a caller commit.

    Both workers reach the barrier before either transaction is committed.  A
    flush-held Python lock would strand the second worker before the barrier;
    the durable outbox lets SQLite serialize only the short commits/drain.
    """
    database = _database(tmp_path)
    application_ids: list[str] = []
    with database.SessionLocal() as session:
        for n in range(2):
            opportunity = Opportunity(
                employer=f"Thread Employer {n}",
                role_title="Thread Internship",
                cycle="2027",
                url=f"https://thread.example/{n}",
            )
            session.add(opportunity)
            session.flush()
            application = Application(
                opportunity_id=opportunity.id,
                state="DISCOVERED",
            )
            session.add(application)
            session.flush()
            application_ids.append(application.id)
        session.commit()

    ready = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        session = database.SessionLocal()
        try:
            application = session.get(Application, application_ids[n])
            assert application is not None
            application.next_action = f"thread business mutation {n}"
            append_audit(
                session,
                AuditInput(
                    "thread",
                    f"thread.{n}",
                    "application",
                    application.id,
                    {"n": n},
                ),
            )
            ready.wait(timeout=5)
            session.commit()
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)
            try:
                session.rollback()
            except Exception:
                pass
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert not errors, errors
    with database.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AuditOutbox)) == 2
        assert [
            session.get(Application, application_id).next_action
            for application_id in application_ids
        ] == ["thread business mutation 0", "thread business mutation 1"]
        verification = verify_audit_chain(session)
    assert verification.valid, verification


def test_business_commit_and_audit_intent_are_atomic(tmp_path):
    database = _database(tmp_path)
    with database.SessionLocal() as session:
        opportunity = Opportunity(
            employer="Atomic Employer",
            role_title="Atomic Internship",
            cycle="2027",
            url="https://atomic.example/role",
        )
        session.add(opportunity)
        session.flush()
        application = Application(opportunity_id=opportunity.id, state="DISCOVERED")
        session.add(application)
        session.flush()
        application.next_action = "business mutation committed"
        append_audit(
            session,
            AuditInput("thread", "application.changed", "application", application.id, {}),
        )
        session.commit()

    with database.SessionLocal() as session:
        persisted = session.get(Application, application.id)
        assert persisted is not None
        assert persisted.next_action == "business mutation committed"
        assert session.scalar(select(func.count()).select_from(AuditOutbox)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_business_rollback_also_rolls_back_audit_intent(tmp_path):
    database = _database(tmp_path)
    with database.SessionLocal() as session:
        append_audit(
            session,
            AuditInput("rollback", "fixture.discarded", "fixture", "1", {}),
        )
        session.rollback()

    with database.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AuditOutbox)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_crash_after_business_commit_leaves_pending_intent_for_recovery(
    tmp_path, monkeypatch
):
    database = _database(tmp_path)
    original_drain = audit_module.drain_audit_outbox

    def crash(*args, **kwargs):
        raise AuditDrainError("injected crash after business commit")

    monkeypatch.setattr(audit_module, "drain_audit_outbox", crash)
    with pytest.raises(AuditDrainError, match="injected crash"):
        with database.SessionLocal() as session:
            append_audit(
                session,
                AuditInput("crash", "fixture.committed", "fixture", "1", {}),
            )
            session.commit()

    monkeypatch.setattr(audit_module, "drain_audit_outbox", original_drain)
    with database.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AuditOutbox)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0

    drained = drain_audit_outbox(database.engine)
    assert drained.emitted_events == 1
    with database.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AuditOutbox)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert verify_audit_chain(session).valid


def test_crash_inside_drain_rolls_back_emission_and_recovery_replays_all(tmp_path):
    database = _database(tmp_path)
    original_drain = audit_module.drain_audit_outbox
    monkeypatch_target = {"drain": original_drain}

    # Commit the business transaction while temporarily suppressing the
    # automatic post-commit drain, leaving two durable pending intents.
    audit_module.drain_audit_outbox = lambda *_args, **_kwargs: None
    try:
        with database.SessionLocal() as session:
            append_audit(session, AuditInput("crash", "fixture.one", "fixture", "1", {}))
            append_audit(session, AuditInput("crash", "fixture.two", "fixture", "2", {}))
            session.commit()
    finally:
        audit_module.drain_audit_outbox = monkeypatch_target["drain"]

    with pytest.raises(AuditDrainError, match="injected audit drain crash"):
        drain_audit_outbox(database.engine, fail_after=1)
    with database.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0
        assert session.scalar(
            select(func.count()).select_from(AuditOutbox).where(
                AuditOutbox.status == AuditOutbox.PENDING
            )
        ) == 2

    result = drain_audit_outbox(database.engine)
    assert result.emitted_events == 2
    with database.SessionLocal() as session:
        assert verify_audit_chain(session).valid


def _process_append_audit(data_dir: str, event_number: int, ready, release, result) -> None:
    """Spawn-safe worker used by the deterministic cross-process test."""
    settings = Settings.load({"ARGUS_DATA_DIR": data_dir, "ARGUS_API_TOKEN": "test-token"})
    database = Database(settings)
    database.create_schema()
    try:
        with database.SessionLocal() as session:
            append_audit(
                session,
                AuditInput(
                    "process",
                    f"process.{event_number}",
                    "fixture",
                    str(event_number),
                    {"n": event_number},
                ),
            )
            ready.set()
            if not release.wait(20):
                raise RuntimeError("commit release timed out")
            session.commit()
        result.put((event_number, "ok"))
    except BaseException as exc:  # noqa: BLE001 - sent to parent
        result.put((event_number, f"{type(exc).__name__}: {exc}"))


def test_two_processes_commit_in_forced_order_without_a_fork(tmp_path):
    """Separate processes have no shared Python lock; SQLite must serialize."""
    database = _database(tmp_path)
    with database.SessionLocal() as session:
        append_audit(session, AuditInput("seed", "seed", "fixture", "0", {}))
        session.commit()

    context = get_context("spawn")
    ready = [context.Event(), context.Event()]
    release = [context.Event(), context.Event()]
    result = context.Queue()
    processes = [
        context.Process(
            target=_process_append_audit,
            args=(str(tmp_path / "data"), n, ready[n], release[n], result),
        )
        for n in range(2)
    ]
    for process in processes:
        process.start()
    try:
        assert ready[0].wait(20)
        assert ready[1].wait(20)
        release[0].set()
        processes[0].join(timeout=20)
        assert not processes[0].is_alive(), "first audit worker did not exit"
        release[1].set()
        processes[1].join(timeout=20)
        assert not processes[1].is_alive(), "second audit worker did not exit"
        outcomes = [result.get(timeout=10) for _ in processes]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    assert all(outcome[1] == "ok" for outcome in outcomes), outcomes
    with database.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 3
        verification = verify_audit_chain(session)
    assert verification.valid, verification
