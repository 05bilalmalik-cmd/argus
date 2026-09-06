import json
import os
import stat
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.automation.runner import AutomationRunner, SubmissionBlocked
from app.automation.types import RunMode
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, AutomationRun, CandidateProfile, Opportunity
from app.scouting import programmes
from app.security.crypto import CryptoBox
from app.services.answers import AnswerService
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


def test_profile_service_encrypts_sensitive_fields_and_returns_snapshot(tmp_path: Path) -> None:
    _, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        service = ProfileService(session, crypto)
        service.update(
            ProfileUpdate(
                first_name="Alex",
                last_name="Sample",
                email="alex@example.test",
                university="Example University",
                degree="BSc Finance with Year in Industry",
                graduation_year=2029,
                preferred_locations=("London", "Manchester"),
                work_authorisation="Exact user-approved wording",
                requires_sponsorship=False,
                work_authorisation_approved=True,
            )
        )
        stored = session.get(CandidateProfile, 1)
        snapshot = service.get_snapshot()
        automation = service.get_automation_data()

        assert "Exact user-approved wording" not in stored.work_authorisation_ciphertext
        assert snapshot.expected_graduation_year == 2029
        assert snapshot.requires_sponsorship is False
        assert snapshot.work_authorisation_approved is True
        assert automation["identity.first_name"] == "Alex"
        assert automation["legal.work_authorisation"] == "Exact user-approved wording"


def test_unapproved_work_authorisation_is_not_exposed_to_automation(tmp_path: Path) -> None:
    _, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        service = ProfileService(session, crypto)
        service.update(
            ProfileUpdate(
                first_name="Alex",
                work_authorisation="Not approved",
                requires_sponsorship=True,
                work_authorisation_approved=False,
            )
        )

        automation = service.get_automation_data()

        assert "legal.work_authorisation" not in automation
        assert "legal.sponsorship" not in automation


def test_summer_runner_inputs_project_2028_without_mutating_stored_profile(
    tmp_path: Path,
) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        ProfileService(session, crypto).update(ProfileUpdate(graduation_year=2029))
        cv = DocumentService(session, settings.documents_dir).store_bytes(
            filename="summer-cv.docx",
            content=cv_docx_bytes(2028, "Summer runner"),
            kind="cv",
            tags=("summer-cv",),
            approved=True,
        )
        opportunity = Opportunity(
            employer="Example Employer",
            role_title="Summer Analyst",
            programme_group="summer",
            cycle="2027",
            url="https://example.test/summer",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.PACKAGE_PREPARED.value,
            selected_cv_id=cv.id,
        )
        session.add(application)
        session.flush()
        resolver = getattr(programmes, "resolve_programme_framing", None)
        assert resolver is not None, "programme framing resolver is missing"
        framing = resolver(opportunity.programme_group)
        assert framing is not None

        profile_values, _, documents, _, _ = AutomationRunner(
            db, settings, crypto
        )._approved_inputs(session, application, framing)

        assert "education.graduation_year" not in profile_values
        assert profile_values["guard.programme_graduation_conflict"] is True
        assert documents["document.cv"].value == cv.path
        assert "summer-cv" in json.loads(cv.tags_json)
        assert session.get(CandidateProfile, 1).graduation_year == 2029


@pytest.mark.parametrize(
    ("selected_tag", "programme_group"),
    [
        pytest.param("yii-cv", "summer", id="yii-cv-with-summer-framing"),
        pytest.param("summer-cv", "year_in_industry", id="summer-cv-with-yii-framing"),
    ],
)
def test_runner_refuses_mixed_cv_and_graduation_framing(
    tmp_path: Path,
    selected_tag: str,
    programme_group: str,
) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        ProfileService(session, crypto).update(ProfileUpdate(graduation_year=2029))
        cv = DocumentService(session, settings.documents_dir).store_bytes(
            filename=f"{selected_tag}.docx",
            content=cv_docx_bytes(
                2029 if selected_tag == "yii-cv" else 2028,
                f"Mixed framing {selected_tag}",
            ),
            kind="cv",
            tags=(selected_tag,),
            approved=True,
        )
        opportunity = Opportunity(
            employer="Example Employer",
            role_title="Analyst",
            programme_group=programme_group,
            cycle="2027",
            url=f"https://example.test/{programme_group}",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.PACKAGE_PREPARED.value,
            selected_cv_id=cv.id,
        )
        session.add(application)
        session.flush()
        resolver = getattr(programmes, "resolve_programme_framing", None)
        assert resolver is not None, "programme framing resolver is missing"
        framing = resolver(programme_group)
        assert framing is not None

        with pytest.raises(SubmissionBlocked) as exc:
            AutomationRunner(db, settings, crypto)._approved_inputs(
                session, application, framing
            )

        assert exc.value.code == "programme_framing_mismatch"


def test_prepare_unknown_programme_refuses_before_document_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        opportunity = Opportunity(
            employer="Example Employer",
            role_title="Analyst",
            programme_group="other",
            cycle="2027",
            url="https://example.test/unknown-programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.QUEUED.value,
        )
        session.add(application)
        session.flush()
        monkeypatch.setattr(
            "app.services.applications.DocumentService",
            lambda *_args, **_kwargs: pytest.fail(
                "document service must not be constructed for an unmapped programme"
            ),
        )

        package = ApplicationService(session, settings, crypto).prepare(application.id)

        assert package.ready is False
        assert package.reason_codes == ("programme_framing_required",)
        assert package.application.state == ApplicationState.NEEDS_USER.value
        assert package.application.selected_cv_id is None
        assert package.application.selected_cover_letter_id is None


def test_prepare_never_selects_a_firm_match_with_the_wrong_cv_variant(
    tmp_path: Path,
) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        documents = DocumentService(session, settings.documents_dir)
        documents.store_bytes(
            filename="older-yii-cv.docx",
            content=cv_docx_bytes(2029, "Wrong YII variant"),
            kind="cv",
            tags=("example-employer", "finance", "yii-cv"),
            approved=True,
        )
        summer_cv = documents.store_bytes(
            filename="summer-cv.docx",
            content=cv_docx_bytes(2028, "Correct summer variant"),
            kind="cv",
            tags=("finance", "summer-cv"),
            approved=True,
        )
        opportunity = Opportunity(
            employer="Example Employer",
            role_title="Summer Analyst",
            division="Finance",
            programme_group="summer",
            cycle="2027",
            url="https://example.test/preparation-cv-framing",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.QUEUED.value,
        )
        session.add(application)
        session.flush()

        package = ApplicationService(session, settings, crypto).prepare(application.id)

        assert package.ready is True
        assert package.cv_id == summer_cv.id


@pytest.mark.parametrize("mode", [RunMode.REVIEW, RunMode.SUBMIT])
def test_runner_unknown_programme_persists_handoff_before_run_inputs_or_navigator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: RunMode
) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        opportunity = Opportunity(
            employer="Example Employer",
            role_title="Analyst",
            programme_group="invalid",
            cycle="2027",
            url="https://example.test/runner-unknown-programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.PACKAGE_PREPARED.value,
        )
        session.add(application)
        session.flush()
        application_id = application.id
        runner = AutomationRunner(db, settings, crypto)

        def forbidden(*_args, **_kwargs):
            pytest.fail("unmapped framing must stop before run inputs or Navigator")

        monkeypatch.setattr(runner, "_resolution_from_opportunity", forbidden)
        monkeypatch.setattr(runner, "_approved_inputs", forbidden)
        monkeypatch.setattr(runner, "_navigator_for_run", forbidden)
        monkeypatch.setattr("app.automation.runner.AutomationRun", forbidden)

        outcome = runner._run_claimed_owner(
            session,
            application,
            application.id,
            mode,
            headed=False,
        )

        assert outcome.state == ApplicationState.NEEDS_USER.value
        assert outcome.blocked_reasons == ("programme_framing_required",)
        assert outcome.run_id == ""
        assert application.state == ApplicationState.NEEDS_USER.value
        assert application.next_action == "Resolve programme framing"

    with db.session_scope() as session:
        persisted = session.get(Application, application_id)
        assert persisted is not None
        assert persisted.state == ApplicationState.NEEDS_USER.value
        assert persisted.next_action == "Resolve programme framing"
        assert session.scalar(select(func.count()).select_from(AutomationRun)) == 0


@pytest.mark.parametrize("mode", [RunMode.REVIEW, RunMode.PREFILL])
def test_runner_unknown_programme_resumes_retryable_failure_to_durable_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: RunMode
) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        opportunity = Opportunity(
            employer="Example Employer",
            role_title="Analyst",
            programme_group="invalid",
            cycle="2027",
            url="https://example.test/retryable-unknown-programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.FAILED_RETRYABLE.value,
        )
        session.add(application)
        session.flush()
        application_id = application.id
        runner = AutomationRunner(db, settings, crypto)

        def forbidden(*_args, **_kwargs):
            pytest.fail("framing handoff must precede target/input/run/Navigator work")

        monkeypatch.setattr(runner, "_resolution_from_opportunity", forbidden)
        monkeypatch.setattr(runner, "_approved_inputs", forbidden)
        monkeypatch.setattr(runner, "_navigator_for_run", forbidden)
        monkeypatch.setattr("app.automation.runner.AutomationRun", forbidden)

        outcome = runner._run_claimed_owner(
            session,
            application,
            application.id,
            mode,
            headed=True,
        )

        assert outcome.state == ApplicationState.NEEDS_USER.value
        assert application.state == ApplicationState.NEEDS_USER.value
        assert application.next_action == "Resolve programme framing"

    with db.session_scope() as session:
        persisted = session.get(Application, application_id)
        assert persisted is not None
        assert persisted.state == ApplicationState.NEEDS_USER.value
        assert persisted.next_action == "Resolve programme framing"
        assert session.scalar(select(func.count()).select_from(AutomationRun)) == 0


@pytest.mark.parametrize(
    ("state", "mode", "headed"),
    [
        (ApplicationState.BLOCKED, RunMode.REVIEW, True),
        (ApplicationState.NEEDS_OA, RunMode.REVIEW, True),
        (ApplicationState.FAILED_RETRYABLE, RunMode.REVIEW, False),
        (ApplicationState.FAILED_RETRYABLE, RunMode.SUBMIT, True),
        (ApplicationState.NEEDS_USER, RunMode.REVIEW, False),
        (ApplicationState.QUEUED, RunMode.REVIEW, True),
    ],
)
def test_runner_unknown_programme_preserves_non_runnable_state_refusal(
    tmp_path: Path,
    state: ApplicationState,
    mode: RunMode,
    headed: bool,
) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        opportunity = Opportunity(
            employer="Example Employer",
            role_title="Analyst",
            programme_group="invalid",
            cycle="2027",
            url="https://example.test/non-runnable-unknown-programme",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=state.value,
        )
        session.add(application)
        session.flush()

        with pytest.raises(SubmissionBlocked) as exc:
            AutomationRunner(db, settings, crypto)._run_claimed_owner(
                session,
                application,
                application.id,
                mode,
                headed=headed,
            )

        assert exc.value.code == "resume_not_allowed"
        assert application.state == state.value
        assert session.scalar(select(func.count()).select_from(AutomationRun)) == 0


def test_answer_service_only_resolves_approved_answers(tmp_path: Path) -> None:
    _, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        answers = AnswerService(session, crypto)
        answers.upsert(
            canonical_key="answer.why_private_credit",
            prompt="Why private credit?",
            answer="I am drawn to downside protection and cash-flow durability.",
            approved=True,
            sensitive=False,
            evidence="Monolith Research and finance studies",
        )
        answers.upsert(
            canonical_key="answer.failure",
            prompt="Tell us about a failure",
            answer="Draft only",
            approved=False,
            sensitive=False,
        )

        approved = answers.resolve("answer.why_private_credit", "Why this division?")

        assert approved.value.startswith("I am drawn")
        assert approved.source == "answer_bank"
        assert answers.resolve("answer.failure", "Failure") is None


def test_document_service_sanitises_filename_hashes_and_verifies_content(tmp_path: Path) -> None:
    settings, db, _ = setup(tmp_path)
    with db.session_scope() as session:
        documents = DocumentService(session, settings.documents_dir)
        stored = documents.store_bytes(
            filename="../../Alex CV 2027.pdf",
            content=b"approved cv bytes",
            kind="cv",
            tags=("private-credit", "london"),
            approved=True,
        )

        assert Path(stored.path).parent == settings.documents_dir
        assert Path(stored.path).name.startswith("Alex_CV_2027")
        assert ".." not in Path(stored.path).name
        assert len(stored.sha256) == 64
        assert documents.verify(stored) is True
        if os.name != "nt":
            assert stat.S_IMODE(Path(stored.path).stat().st_mode) == 0o600

        Path(stored.path).write_bytes(b"tampered")
        assert documents.verify(stored) is False
