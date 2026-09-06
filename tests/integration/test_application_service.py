import json
from datetime import date, timedelta
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, ConflictRule, Opportunity
from app.security.crypto import CryptoBox
from app.services.applications import ApplicationService
from app.services.documents import DocumentService
from app.services.profile import ProfileService, ProfileUpdate
from tests.document_helpers import cv_docx_bytes


def setup(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox.from_path(settings.secret_key_path)
    return settings, db, crypto


def seed_profile(session, crypto):
    ProfileService(session, crypto).update(
        ProfileUpdate(
            first_name="Demo",
            last_name="Candidate",
            email="demo@example.test",
            graduation_year=2029,
            university="Example University",
            work_authorisation="Approved UK wording",
            requires_sponsorship=False,
            work_authorisation_approved=True,
        )
    )


def opportunity(**overrides):
    values = {
        "employer": "Ares Management",
        "role_title": "Summer Analyst",
        "division": "Private Credit",
        "programme_group": "summer",
        "location": "London",
        "cycle": "2027",
        "url": "https://jobs.example.test/ares",
        "deadline": date.today() + timedelta(days=20),
        "min_graduation_year": 2028,
        "max_graduation_year": 2029,
        "sponsorship_supported": False,
        "cv_required": True,
    }
    values.update(overrides)
    return Opportunity(**values)


def test_application_pipeline_evaluates_queues_and_prepares_approved_cv(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        seed_profile(session, crypto)
        cv = DocumentService(session, settings.documents_dir).store_bytes(
            filename="Synthetic Private Credit CV.docx",
            content=cv_docx_bytes(2028, "Private Credit"),
            kind="cv",
            tags=("credit", "london", "summer-cv"),
            approved=True,
        )
        role = opportunity()
        session.add(role)
        session.flush()
        service = ApplicationService(session, settings, crypto)

        decision = service.evaluate(role.id)
        queued = service.queue(decision.application.id)
        package = service.prepare(queued.id)

        assert decision.eligible is True
        assert decision.conflict_blocked is False
        assert queued.state == ApplicationState.PACKAGE_PREPARED.value
        assert package.ready is True
        assert package.cv_id == cv.id
        assert package.application.selected_cv_id == cv.id
        assert package.application.next_action == "Run application"


def test_prepare_pauses_when_required_cv_is_missing(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        seed_profile(session, crypto)
        role = opportunity(url="https://jobs.example.test/no-cv")
        session.add(role)
        session.flush()
        service = ApplicationService(session, settings, crypto)
        application = service.queue(service.evaluate(role.id).application.id)

        package = service.prepare(application.id)

        assert package.ready is False
        assert package.application.state == ApplicationState.NEEDS_USER.value
        assert package.application.next_action == "Upload and approve a CV"
        assert "required_cv_missing" in package.reason_codes


def test_employer_rule_blocks_conflicting_application(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        seed_profile(session, crypto)
        first_role = opportunity(url="https://jobs.example.test/first")
        second_role = opportunity(
            url="https://jobs.example.test/second",
            division="Real Assets",
            programme_group="summer",
        )
        session.add_all([first_role, second_role])
        session.flush()
        session.add(
            ConflictRule(
                employer_pattern="Ares*",
                cycle="2027",
                max_applications=1,
                exclusive_groups_json=json.dumps([]),
            )
        )
        session.add(
            Application(
                opportunity_id=first_role.id,
                state=ApplicationState.SUBMITTED.value,
            )
        )
        session.flush()

        decision = ApplicationService(session, settings, crypto).evaluate(second_role.id)

        assert decision.conflict_blocked is True
        assert decision.application.state == ApplicationState.BLOCKED.value
        assert "maximum_applications_reached" in decision.reason_codes


def test_unverified_work_authorisation_sets_review_risk(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        ProfileService(session, crypto).update(
            ProfileUpdate(first_name="Demo", graduation_year=2029)
        )
        role = opportunity(url="https://jobs.example.test/review")
        session.add(role)
        session.flush()

        decision = ApplicationService(session, settings, crypto).evaluate(role.id)

        assert decision.eligible is True
        assert decision.requires_review is True
        assert decision.application.risk_level == 3
        assert decision.application.next_action == "Review eligibility answers"


def test_reevaluation_unblocks_application_after_eligibility_is_resolved(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        ProfileService(session, crypto).update(
            ProfileUpdate(
                first_name="Demo",
                graduation_year=2030,
                work_authorisation="Approved UK wording",
                requires_sponsorship=False,
                work_authorisation_approved=True,
            )
        )
        role = opportunity(url="https://jobs.example.test/re-evaluate")
        session.add(role)
        session.flush()
        service = ApplicationService(session, settings, crypto)

        blocked = service.evaluate(role.id)
        ProfileService(session, crypto).update(ProfileUpdate(graduation_year=2029))
        resolved = service.evaluate(role.id)

        assert blocked.application.id == resolved.application.id
        assert resolved.eligible is True
        assert resolved.conflict_blocked is False
        assert resolved.application.state == ApplicationState.ELIGIBILITY_CHECKED.value
        assert resolved.application.next_action == "Queue application"


def test_reevaluation_never_reopens_a_confirmed_application(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        seed_profile(session, crypto)
        role = opportunity(url="https://jobs.example.test/already-submitted")
        session.add(role)
        session.flush()
        application = Application(
            opportunity_id=role.id,
            state=ApplicationState.CONFIRMATION_VERIFIED.value,
            risk_level=0,
            next_action="Monitor for assessment or interview",
            submission_reference="ARG-LOCKED",
        )
        session.add(application)
        session.flush()

        decision = ApplicationService(session, settings, crypto).evaluate(role.id)

        assert decision.application.state == ApplicationState.CONFIRMATION_VERIFIED.value
        assert decision.application.next_action == "Monitor for assessment or interview"
        assert decision.application.submission_reference == "ARG-LOCKED"


def test_retryable_failure_can_be_requeued_after_review(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        role = opportunity(url="https://jobs.example.test/retryable")
        session.add(role)
        session.flush()
        application = Application(
            opportunity_id=role.id,
            state=ApplicationState.FAILED_RETRYABLE.value,
            eligibility_json=json.dumps({"eligible": True}),
            conflict_json=json.dumps({"blocked": False}),
            next_action="Retry automation after reviewing the failure",
        )
        session.add(application)
        session.flush()

        queued = ApplicationService(session, settings, crypto).queue(application.id)

        assert queued.state == ApplicationState.QUEUED.value
        assert queued.next_action == "Prepare documents"
