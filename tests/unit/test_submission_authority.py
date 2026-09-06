from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading

import pytest
from sqlalchemy import inspect, text

from app.config import Settings
from app.db import Database
from app.models import (
    Application,
    Opportunity,
    SubmissionAuthority,
    SubmissionReviewBinding,
)
from app.services.submission_authority import (
    SubmissionAuthorityError,
    SubmissionAuthorityService,
    SubmissionReviewBindingService,
    manifest_fingerprint,
    authority_manifest_projection,
)


def database(tmp_path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_API_TOKEN": "test"})
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    return db


def seed(db, application_id="app-1"):
    with db.session_scope() as session:
        opportunity = Opportunity(
            id=f"opp-{application_id}", employer="Loopback", role_title="Analyst",
            cycle="2027", url="http://127.0.0.1:8765/source", source="test",
        )
        session.add(opportunity)
        session.add(Application(id=application_id, opportunity=opportunity, state="READY_TO_SUBMIT"))


def manifest(application_id="app-1", target="target-1", document="sha256:doc-1"):
    destination = "http://127.0.0.1:8765"
    return {
        "application_id": application_id,
        "employer": "Loopback",
        "role": "Analyst",
        "requisition": "req-1",
        "provider": "greenhouse",
        "application_url": f"{destination}/apply",
        "destination": destination,
        "form_action": f"{destination}/submit",
        "expected_final_url": f"{destination}/submit",
        "method": "POST",
        "form_identity": "greenhouse-form-v1",
        "root_selector": "#application",
        "control_selector": "button[type=submit]",
        "control_fingerprint": target,
        "frame_url": f"{destination}/apply",
        "target_fingerprint": target,
        "documents": [{
            "id": "cv-1",
            "kind": "document.cv",
            "sha256": document,
            "approved": True,
        }],
        "answers": [{
            "id": "answer-1",
            "canonical_key": "answer.motivation",
            "sha256": "a" * 64,
            "approved": True,
            "sensitive": False,
        }],
    }


def test_issue_and_consume_once(tmp_path):
    db = database(tmp_path); seed(db)
    with db.session_scope() as session:
        service = SubmissionAuthorityService(session)
        authority = service.issue(application_id="app-1", session_id="sess-1", manifest=manifest(), destination_origin="http://127.0.0.1:8765")
        authority_id = authority.id
    with db.session_scope() as session:
        consumed = SubmissionAuthorityService(session).consume(authority_id, application_id="app-1", session_id="sess-1", manifest=manifest(), destination_origin="http://127.0.0.1:8765")
        assert consumed.consumed_at is not None
    with db.session_scope() as session:
        with pytest.raises(SubmissionAuthorityError):
            SubmissionAuthorityService(session).consume(authority_id, application_id="app-1", session_id="sess-1", manifest=manifest(), destination_origin="http://127.0.0.1:8765")


def test_displayed_review_binding_is_durable_and_rejects_same_origin_control_change(tmp_path):
    db = database(tmp_path); seed(db)
    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    with db.session_scope() as session:
        recorded = SubmissionReviewBindingService(session).record(
            application_id="app-1",
            session_id="sess-review",
            manifest=manifest(),
            destination_origin="http://127.0.0.1:8765",
            expires_at=expiry,
        )
        binding_id = recorded.id

    db.engine.dispose()
    restarted = Database(db.settings); restarted.create_schema()
    with restarted.session_scope() as session:
        validated = SubmissionReviewBindingService(session).validate(
            application_id="app-1",
            session_id="sess-review",
            manifest=manifest(),
            destination_origin="http://127.0.0.1:8765",
        )
        assert validated.id == binding_id
        changed = manifest() | {
            "control_selector": "form#other button[type=submit]",
            "control_fingerprint": "same-origin-other-control",
        }
        with pytest.raises(SubmissionAuthorityError, match="displayed review binding"):
            SubmissionReviewBindingService(session).validate(
                application_id="app-1",
                session_id="sess-review",
                manifest=changed,
                destination_origin="http://127.0.0.1:8765",
            )
    assert "submission_review_bindings" in inspect(restarted.engine).get_table_names()


@pytest.mark.parametrize("change", ["application", "session", "manifest", "origin"])
def test_binding_mismatch_fails_closed(tmp_path, change):
    db = database(tmp_path); seed(db)
    with db.session_scope() as session:
        authority = SubmissionAuthorityService(session).issue(application_id="app-1", session_id="sess-1", manifest=manifest(), destination_origin="http://127.0.0.1:8765")
        values = {"application_id": "app-1", "session_id": "sess-1", "manifest": manifest(), "destination_origin": "http://127.0.0.1:8765"}
        if change == "application": values["application_id"] = "app-2"
        if change == "session": values["session_id"] = "sess-2"
        if change == "manifest": values["manifest"] = manifest(target="target-2")
        if change == "origin": values["destination_origin"] = "http://127.0.0.1:9999"
        with pytest.raises(SubmissionAuthorityError):
            SubmissionAuthorityService(session).consume(authority.id, **values)


def test_document_hash_and_target_fingerprint_are_bound(tmp_path):
    db = database(tmp_path); seed(db)
    with db.session_scope() as session:
        authority = SubmissionAuthorityService(session).issue(application_id="app-1", session_id="sess-1", manifest=manifest(), destination_origin="http://127.0.0.1:8765")
        for altered in (manifest(document="sha256:other"), manifest(target="other")):
            with pytest.raises(SubmissionAuthorityError):
                SubmissionAuthorityService(session).consume(authority.id, application_id="app-1", session_id="sess-1", manifest=altered, destination_origin="http://127.0.0.1:8765")


def test_approved_answer_identity_and_hash_are_bound(tmp_path):
    db = database(tmp_path); seed(db)
    with db.session_scope() as session:
        authority = SubmissionAuthorityService(session).issue(
            application_id="app-1",
            session_id="sess-1",
            manifest=manifest(),
            destination_origin="http://127.0.0.1:8765",
        )
        changed = manifest()
        changed["answers"] = [dict(changed["answers"][0], sha256="b" * 64)]
        with pytest.raises(SubmissionAuthorityError):
            SubmissionAuthorityService(session).consume(
                authority.id,
                application_id="app-1",
                session_id="sess-1",
                manifest=changed,
                destination_origin="http://127.0.0.1:8765",
            )


@pytest.mark.parametrize(
    "answers",
    [
        [{"id": "answer-1", "canonical_key": "answer.x", "sha256": "a" * 64, "approved": False, "sensitive": False}],
        [{"id": "answer-1", "canonical_key": "answer.x", "sha256": "short", "approved": True, "sensitive": False}],
        [{"id": "answer-1", "canonical_key": "answer.x", "sha256": "a" * 64, "approved": True, "sensitive": False, "plaintext": "must not persist"}],
        [
            {"id": "answer-1", "canonical_key": "answer.x", "sha256": "a" * 64, "approved": True, "sensitive": False},
            {"id": "answer-1", "canonical_key": "answer.y", "sha256": "b" * 64, "approved": True, "sensitive": False},
        ],
    ],
)
def test_invalid_or_plaintext_answer_evidence_is_rejected(answers):
    with pytest.raises(SubmissionAuthorityError):
        authority_manifest_projection(manifest() | {"answers": answers})


def test_answer_order_is_canonical():
    first = manifest()
    first["answers"] = [
        {"id": "answer-2", "canonical_key": "answer.z", "sha256": "b" * 64, "approved": True, "sensitive": True},
        *first["answers"],
    ]
    second = dict(first, answers=list(reversed(first["answers"])))
    assert manifest_fingerprint(first) == manifest_fingerprint(second)


def test_expiry_is_fixed_and_not_extended(tmp_path):
    db = database(tmp_path); seed(db)
    clock = [datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)]
    with db.session_scope() as session:
        service = SubmissionAuthorityService(session, now=lambda: clock[0])
        authority = service.issue(application_id="app-1", session_id="sess-1", manifest=manifest(), destination_origin="http://127.0.0.1:8765", expires_in_seconds=300)
        expiry = authority.expires_at
    clock[0] += timedelta(seconds=301)
    with db.session_scope() as session:
        with pytest.raises(SubmissionAuthorityError):
            SubmissionAuthorityService(session, now=lambda: clock[0]).consume(authority.id, application_id="app-1", session_id="sess-1", manifest=manifest(), destination_origin="http://127.0.0.1:8765")
    assert expiry == datetime(2026, 8, 25, 12, 5, tzinfo=timezone.utc)


def test_authority_survives_service_restart_and_schema_is_additive(tmp_path):
    db = database(tmp_path); seed(db)
    with db.session_scope() as session:
        authority = SubmissionAuthorityService(session).issue(application_id="app-1", session_id="sess-1", manifest=manifest(), destination_origin="http://127.0.0.1:8765")
        authority_id = authority.id
    db.engine.dispose()
    restarted = Database(db.settings); restarted.create_schema()
    with restarted.session_scope() as session:
        stored = SubmissionAuthorityService(session).consume(authority_id, application_id="app-1", session_id="sess-1", manifest=manifest(), destination_origin="http://127.0.0.1:8765")
        assert stored.id == authority_id
    assert "submission_authorities" in inspect(restarted.engine).get_table_names()


def test_concurrent_consume_has_one_winner(tmp_path):
    db = database(tmp_path); seed(db)
    with db.session_scope() as session:
        authority = SubmissionAuthorityService(session).issue(application_id="app-1", session_id="sess-1", manifest=manifest(), destination_origin="http://127.0.0.1:8765")
        authority_id = authority.id
    barrier = threading.Barrier(2); results = []
    def worker():
        local = Database(db.settings)
        try:
            with local.SessionLocal() as session:
                barrier.wait()
                try:
                    SubmissionAuthorityService(session).consume(authority_id, application_id="app-1", session_id="sess-1", manifest=manifest(), destination_origin="http://127.0.0.1:8765")
                    session.commit(); results.append("winner")
                except SubmissionAuthorityError:
                    results.append("loser")
        finally:
            local.engine.dispose()
    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]; [t.join(timeout=10) for t in threads]
    assert sorted(results) == ["loser", "winner"]


def test_canonical_fingerprint_is_order_independent():
    first = manifest()
    second = {key: first[key] for key in reversed(list(first))}
    assert manifest_fingerprint(first) == manifest_fingerprint(second)


def test_canonical_fingerprint_ignores_volatile_display_timestamp_but_binds_form_identity():
    base = manifest() | {"expires_at": "2026-08-25T12:00:00+00:00"}
    changed_time = dict(base, expires_at="2026-08-25T12:05:00+00:00")
    changed_form = dict(base, form_identity="greenhouse-form-v2")
    assert manifest_fingerprint(base) == manifest_fingerprint(changed_time)
    assert manifest_fingerprint(base) != manifest_fingerprint(changed_form)
    assert "expires_at" not in authority_manifest_projection(base)


def test_raw_page_id_and_raw_target_fingerprint_are_volatile():
    first = manifest() | {"page_id": "page-review", "target_fingerprint": "raw-review"}
    second = manifest() | {"page_id": "page-execution", "target_fingerprint": "raw-execution"}
    assert manifest_fingerprint(first) == manifest_fingerprint(second)


def test_incomplete_manifest_is_rejected_fail_closed(tmp_path):
    db = database(tmp_path); seed(db)
    with db.session_scope() as session:
        with pytest.raises(SubmissionAuthorityError, match="manifest is incomplete"):
            SubmissionAuthorityService(session).issue(
                application_id="app-1",
                session_id="sess-1",
                manifest={"application_id": "app-1"},
                destination_origin="http://127.0.0.1:8765",
            )
        missing_answers = manifest()
        del missing_answers["answers"]
        with pytest.raises(SubmissionAuthorityError, match="answers"):
            SubmissionAuthorityService(session).issue(
                application_id="app-1",
                session_id="sess-2",
                manifest=missing_answers,
                destination_origin="http://127.0.0.1:8765",
            )


def test_repeated_issue_is_one_durable_row(tmp_path):
    db = database(tmp_path); seed(db)
    with db.session_scope() as session:
        first = SubmissionAuthorityService(session).issue(
            application_id="app-1",
            session_id="sess-1",
            manifest=manifest(),
            destination_origin="http://127.0.0.1:8765",
        )
        second = SubmissionAuthorityService(session).issue(
            application_id="app-1",
            session_id="sess-1",
            manifest=manifest(),
            destination_origin="http://127.0.0.1:8765",
        )
        assert second.id == first.id
        assert session.query(type(first)).filter_by(
            application_id="app-1", session_id="sess-1"
        ).count() == 1


def test_concurrent_issue_returns_one_authority_id(tmp_path):
    db = database(tmp_path); seed(db)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        local = Database(db.settings)
        try:
            with local.SessionLocal() as session:
                barrier.wait()
                authority = SubmissionAuthorityService(session).issue(
                    application_id="app-1",
                    session_id="sess-concurrent",
                    manifest=manifest(),
                    destination_origin="http://127.0.0.1:8765",
                )
                session.commit()
                results.append(authority.id)
        except Exception as exc:  # pragma: no cover - assertion below reports it
            results.append(type(exc).__name__)
        finally:
            local.engine.dispose()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    [thread.start() for thread in threads]
    [thread.join(timeout=35) for thread in threads]
    assert len(results) == 2
    assert all(isinstance(value, str) and value not in {"IntegrityError", "OperationalError"} for value in results)
    assert results[0] == results[1]
    with db.session_scope() as session:
        assert session.query(SubmissionAuthority).filter_by(
            application_id="app-1", session_id="sess-concurrent"
        ).count() == 1


def test_populated_legacy_authority_table_gets_additive_unique_migration(tmp_path):
    db = database(tmp_path); seed(db)
    with db.session_scope() as session:
        authority = SubmissionAuthorityService(session).issue(
            application_id="app-1",
            session_id="sess-legacy",
            manifest=manifest(),
            destination_origin="http://127.0.0.1:8765",
        )
        row = {
            "id": authority.id,
            "application_id": authority.application_id,
            "session_id": authority.session_id,
            "manifest_fingerprint": authority.manifest_fingerprint,
            "manifest_json": authority.manifest_json,
            "destination_origin": authority.destination_origin,
            "issued_at": authority.issued_at.isoformat(),
            "expires_at": authority.expires_at.isoformat(),
        }

    # Simulate a populated pre-uniqueness table: schema creation must preserve
    # the authority row and add the invariant without requiring a data reset.
    with db.engine.begin() as connection:
        connection.execute(text("DROP TABLE submission_authorities"))
        connection.execute(text(
            """
            CREATE TABLE submission_authorities (
              id VARCHAR(96) NOT NULL PRIMARY KEY,
              application_id VARCHAR(36) NOT NULL,
              session_id VARCHAR(120) NOT NULL,
              manifest_fingerprint VARCHAR(64) NOT NULL,
              manifest_json TEXT NOT NULL,
              destination_origin VARCHAR(500) NOT NULL,
              issued_at DATETIME NOT NULL,
              expires_at DATETIME NOT NULL,
              consumed_at DATETIME
            )
            """
        ))
        connection.execute(text(
            "INSERT INTO submission_authorities "
            "(id, application_id, session_id, manifest_fingerprint, manifest_json, "
            "destination_origin, issued_at, expires_at, consumed_at) "
            "VALUES (:id, :application_id, :session_id, :manifest_fingerprint, "
            ":manifest_json, :destination_origin, :issued_at, :expires_at, NULL)"
        ), row)

    restarted = Database(db.settings)
    restarted.create_schema()
    indexes = inspect(restarted.engine).get_indexes("submission_authorities")
    assert any(
        bool(index.get("unique"))
        and tuple(index.get("column_names") or ()) == ("application_id", "session_id")
        for index in indexes
    )
    with restarted.session_scope() as session:
        stored = session.get(SubmissionAuthority, row["id"])
        assert stored is not None
        assert stored.session_id == "sess-legacy"
    with restarted.engine.connect() as connection:
        assert connection.execute(text("PRAGMA user_version")).scalar_one() >= 6


def test_populated_legacy_duplicate_authorities_fail_migration_without_deletion(tmp_path):
    db = database(tmp_path); seed(db)
    with db.engine.begin() as connection:
        connection.execute(text("DROP TABLE submission_authorities"))
        connection.execute(text(
            """
            CREATE TABLE submission_authorities (
              id VARCHAR(96) NOT NULL PRIMARY KEY,
              application_id VARCHAR(36) NOT NULL,
              session_id VARCHAR(120) NOT NULL,
              manifest_fingerprint VARCHAR(64) NOT NULL,
              manifest_json TEXT NOT NULL,
              destination_origin VARCHAR(500) NOT NULL,
              issued_at DATETIME NOT NULL,
              expires_at DATETIME NOT NULL,
              consumed_at DATETIME
            )
            """
        ))
        for authority_id in ("legacy-a", "legacy-b"):
            connection.execute(text(
                "INSERT INTO submission_authorities VALUES "
                "(:id, 'app-1', 'duplicate-session', 'f', '{}', "
                "'http://127.0.0.1:8765', '2026-08-25', '2026-08-26', NULL)"
            ), {"id": authority_id})

    restarted = Database(db.settings)
    with pytest.raises(RuntimeError, match="duplicate application/session rows"):
        restarted.create_schema()
    with restarted.engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM submission_authorities")
        ).scalar_one() == 2


def test_migration_preserves_consumed_and_expired_rows_with_other_unique_index(tmp_path):
    db = database(tmp_path); seed(db)
    with db.engine.begin() as connection:
        connection.execute(text("DROP TABLE submission_authorities"))
        connection.execute(text(
            """
            CREATE TABLE submission_authorities (
              id VARCHAR(96) NOT NULL PRIMARY KEY,
              application_id VARCHAR(36) NOT NULL,
              session_id VARCHAR(120) NOT NULL,
              manifest_fingerprint VARCHAR(64) NOT NULL,
              manifest_json TEXT NOT NULL,
              destination_origin VARCHAR(500) NOT NULL,
              issued_at DATETIME NOT NULL,
              expires_at DATETIME NOT NULL,
              consumed_at DATETIME
            )
            """
        ))
        connection.execute(text(
            "CREATE UNIQUE INDEX legacy_authority_id_session "
            "ON submission_authorities(id, session_id)"
        ))
        connection.execute(text(
            "INSERT INTO submission_authorities VALUES "
            "('consumed', 'app-1', 'old-consumed', 'f1', '{}', "
            "'http://127.0.0.1:8765', '2026-08-20', '2026-08-21', '2026-08-20')"
        ))
        connection.execute(text(
            "INSERT INTO submission_authorities VALUES "
            "('expired', 'app-1', 'old-expired', 'f2', '{}', "
            "'http://127.0.0.1:8765', '2026-08-20', '2026-08-21', NULL)"
        ))

    restarted = Database(db.settings); restarted.create_schema()
    indexes = inspect(restarted.engine).get_indexes("submission_authorities")
    assert any(
        bool(index.get("unique"))
        and tuple(index.get("column_names") or ()) == ("application_id", "session_id")
        for index in indexes
    )
    with restarted.session_scope() as session:
        consumed = session.get(SubmissionAuthority, "consumed")
        expired = session.get(SubmissionAuthority, "expired")
        assert consumed is not None and consumed.consumed_at is not None
        assert expired is not None and expired.consumed_at is None

def test_submit_json_is_exactly_three_server_binding_ids():
    from pydantic import ValidationError
    from app.routers.api import RunApplicationBody

    body = RunApplicationBody(application_id="app-1", session_id="sess-1", authority_id="auth-1")
    assert body.model_dump() == {
        "application_id": "app-1",
        "session_id": "sess-1",
        "authority_id": "auth-1",
    }
    with pytest.raises(ValidationError):
        RunApplicationBody(application_id="app-1", session_id="sess-1", authority_id="auth-1", url="https://evil.test")
    with pytest.raises(ValidationError):
        RunApplicationBody(application_id="app-1", session_id="sess-1")
