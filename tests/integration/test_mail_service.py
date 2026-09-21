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
    message["To"] = "demo.candidate@example.test"
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
