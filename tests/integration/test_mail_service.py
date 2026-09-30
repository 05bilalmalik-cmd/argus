from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, Opportunity
from app.security.crypto import CryptoBox
from app.services.email import MailService


def oa_email() -> bytes:
    message = EmailMessage()
    message["Message-ID"] = "<oa-arg-55@example.test>"
    message["From"] = "earlycareers@argustestcapital.example"
    message["To"] = "alex@example.test"
    message["Subject"] = "ARGUS Test Capital — online assessment invitation"
    message["Date"] = "Sat, 22 Aug 2026 09:30:00 +0100"
    message.set_content(
        "Application reference ARG-55. Please complete your numerical reasoning assessment by 17:00 on 25 August 2026."
    )
    return message.as_bytes()


def test_mail_service_links_assessment_updates_state_and_deduplicates(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox.from_path(settings.secret_key_path)
    with db.session_scope() as session:
        opportunity = Opportunity(
            employer="ARGUS Test Capital",
            role_title="Summer Analyst",
            cycle="2027",
            url="https://jobs.example.test/argus",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.CONFIRMATION_VERIFIED.value,
            submission_reference="ARG-55",
        )
        session.add(application)
        session.flush()

        service = MailService(session)
        first = service.ingest(oa_email())
        second = service.ingest(oa_email())

        assert first.id == second.id
        assert first.classification == "assessment"
        assert first.application_id == application.id
        assert application.state == ApplicationState.OA_PENDING.value
        assert application.next_action == "Complete online assessment"
        assert application.next_action_deadline is not None
        assert application.next_action_deadline.date().isoformat() == "2026-08-25"

def unmatched_oa_email() -> bytes:
    from email.message import EmailMessage as _EmailMessage

    message = _EmailMessage()
    message["Message-ID"] = "<oa-nomatch-7@example.test>"
    message["From"] = "assessments@unknown-sender.example"
    message["To"] = "alex@example.test"
    message["Subject"] = "Online assessment invitation"
    message["Date"] = "Sat, 22 Aug 2026 09:30:00 +0100"
    message.set_content(
        "Please complete your numerical reasoning assessment by 17:00 on 25 August 2026."
    )
    return message.as_bytes()


def confirmation_email() -> bytes:
    from email.message import EmailMessage as _EmailMessage

    message = _EmailMessage()
    message["Message-ID"] = "<confirm-nomatch-9@example.test>"
    message["From"] = "noreply@unknown-sender.example"
    message["To"] = "alex@example.test"
    message["Subject"] = "Thank you for applying"
    message["Date"] = "Sat, 22 Aug 2026 09:30:00 +0100"
    message.set_content("We received your application and will review it shortly.")
    return message.as_bytes()


def _seed_app(session, *, state, employer="Match Bank"):
    opportunity = Opportunity(
        employer=employer,
        role_title="Summer Analyst",
        cycle="2027",
        url=f"https://jobs.example.test/{employer.casefold().replace(chr(32), chr(45))}",
    )
    session.add(opportunity)
    session.flush()
    application = Application(opportunity_id=opportunity.id, state=state)
    session.add(application)
    session.flush()
    return application


def test_override_binds_unmatched_and_applies_assessment(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    db.create_schema()
    with db.session_scope() as session:
        application = _seed_app(session, state=ApplicationState.CONFIRMATION_VERIFIED.value)
        service = MailService(session)
        record = service.ingest(unmatched_oa_email())
        assert record.application_id is None

        bound = service.override_match(record.id, application.id)
        assert bound.application_id == application.id
        assert application.state == ApplicationState.OA_PENDING.value
        assert application.next_action_deadline is not None


def test_override_unbinds_without_rewriting_state(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    db.create_schema()
    with db.session_scope() as session:
        application = _seed_app(session, state=ApplicationState.CONFIRMATION_VERIFIED.value)
        service = MailService(session)
        record = service.override_match(
            service.ingest(unmatched_oa_email()).id, application.id
        )
        assert application.state == ApplicationState.OA_PENDING.value

        cleared = service.override_match(record.id, None)
        assert cleared.application_id is None
        assert application.state == ApplicationState.OA_PENDING.value


def test_override_rejects_unknown_ids(tmp_path: Path) -> None:
    import pytest as _pytest

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    db.create_schema()
    with db.session_scope() as session:
        application = _seed_app(session, state=ApplicationState.CONFIRMATION_VERIFIED.value)
        service = MailService(session)
        record = service.ingest(unmatched_oa_email())
        with _pytest.raises(KeyError):
            service.override_match("missing-email", application.id)
        with _pytest.raises(KeyError):
            service.override_match(record.id, "missing-application")
        with _pytest.raises(KeyError):
            service.match_candidates("missing-email")


def test_override_invalid_transition_binds_without_state_change(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    db.create_schema()
    with db.session_scope() as session:
        application = _seed_app(session, state=ApplicationState.QUEUED.value)
        service = MailService(session)
        record = service.ingest(confirmation_email())
        assert record.application_id is None

        bound = service.override_match(record.id, application.id)
        assert bound.application_id == application.id
        assert application.state == ApplicationState.QUEUED.value


def test_match_endpoints_bind_and_list_candidates(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient as _TestClient

    from app.main import create_app as _create_app

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    app = _create_app(settings)
    with _TestClient(app) as api:
        with app.state.db.session_scope() as session:
            application = _seed_app(session, state=ApplicationState.CONFIRMATION_VERIFIED.value)
            record = MailService(session).ingest(unmatched_oa_email())
            email_id, application_id = record.id, application.id

        candidates = api.get(f"/api/mail/{email_id}/candidates")
        assert candidates.status_code == 200, candidates.text
        assert candidates.json()["email_id"] == email_id

        bound = api.post(f"/api/mail/{email_id}/match", json={"application_id": application_id})
        assert bound.status_code == 200, bound.text
        assert bound.json()["application_id"] == application_id

        assert api.post("/api/mail/missing/match", json={"application_id": application_id}).status_code == 404
        assert api.get("/api/mail/missing/candidates").status_code == 404
