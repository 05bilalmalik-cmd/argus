from pathlib import Path

from app.config import Settings
from app.db import Database
from app.security.audit import AuditInput, append_audit, verify_audit_chain


def database(tmp_path: Path) -> Database:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    db.create_schema()
    return db


def test_audit_chain_verifies_ordered_events(tmp_path: Path) -> None:
    db = database(tmp_path)
    with db.session_scope() as session:
        append_audit(
            session,
            AuditInput("system", "profile.created", "profile", "1", {"approved": False}),
        )
        append_audit(
            session,
            AuditInput("demo", "profile.approved", "profile", "1", {"approved": True}),
        )
        verification = verify_audit_chain(session)

        assert verification.valid is True
        assert verification.checked_events == 2
        assert verification.broken_event_id is None


def test_audit_chain_detects_mutated_history(tmp_path: Path) -> None:
    db = database(tmp_path)
    with db.session_scope() as session:
        first = append_audit(
            session,
            AuditInput("system", "application.queued", "application", "a1", {"priority": 90}),
        )
        append_audit(
            session,
            AuditInput("system", "application.prepared", "application", "a1", {"cv": "cv1"}),
        )
        first.details_json = '{"priority":1}'
        session.flush()

        verification = verify_audit_chain(session)

        assert verification.valid is False
        assert verification.broken_event_id == first.id
