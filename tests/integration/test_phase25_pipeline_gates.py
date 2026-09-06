from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.automation.targets import TargetResolution
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.models import Application, Opportunity, SubmissionAuthority
from app.scouting.service import ScoutService
from app.security.crypto import CryptoBox


def _runtime(tmp_path: Path, *, mode: str = "OFF") -> tuple[Settings, Database, CryptoBox]:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_API_TOKEN": "phase25-gate-test",
            "ARGUS_AUTOMATION_MODE": mode,
            "ARGUS_ENABLE_NOTIFICATIONS": "false",
        }
    )
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return settings, database, CryptoBox.from_path(settings.secret_key_path)


def _opportunity(identifier: str, *, status: str, employer: str = "Example Capital") -> Opportunity:
    return Opportunity(
        id=identifier,
        employer=employer,
        role_title="Summer Analyst",
        programme_group="summer",
        location="London",
        cycle="2027",
        url=f"https://example.test/jobs/{identifier}",
        application_url=f"https://example.test/apply/{identifier}",
        target_status=TargetKind.APPLICATION_FORM.value,
        resolved_ats_type="fixture",
        resolved_at=datetime.now(timezone.utc),
        application_window_status="OPEN",
        cv_required=False,
        user_status=status,
        user_status_actor="user",
    )


@pytest.mark.parametrize(
    "excluded_status",
    ("NOT_INTERESTED", "APPLICATION_SUBMITTED"),
)
def test_queue_and_prepare_refuse_excluded_status_without_mutating_machine_state(
    tmp_path: Path,
    excluded_status: str,
) -> None:
    from app.services.applications import ApplicationBlockedError, ApplicationService

    settings, database, crypto = _runtime(tmp_path)
    with database.session_scope() as session:
        opportunity = _opportunity("application-gate", status=excluded_status)
        application = Application(
            id="application-gate",
            opportunity=opportunity,
            state=ApplicationState.ELIGIBILITY_CHECKED.value,
            eligibility_json='{"eligible":true}',
            conflict_json='{"blocked":false}',
            next_action="preserve this",
        )
        session.add(application)
        session.flush()
        service = ApplicationService(session, settings, crypto)

        with pytest.raises(ApplicationBlockedError, match=excluded_status):
            service.queue(application.id)
        assert application.state == ApplicationState.ELIGIBILITY_CHECKED.value
        assert application.next_action == "preserve this"

        opportunity.user_status = "INTERESTED"
        queued = service.queue(application.id)
        assert queued.state == ApplicationState.QUEUED.value
        queued.next_action = "preserve queued state"
        opportunity.user_status = excluded_status

        package = service.prepare(application.id)
        assert package.ready is False
        assert package.reason_codes == ("user_status_excludes_automation",)
        assert application.state == ApplicationState.QUEUED.value
        assert application.next_action == "preserve queued state"
        assert opportunity.is_archived is False


def test_target_resolution_refuses_excluded_status_before_browser_or_persistence(
    tmp_path: Path,
) -> None:
    from app.services.target_resolution import (
        TargetResolutionService,
        UserStatusAutomationExcludedError,
    )

    _settings, database, _crypto = _runtime(tmp_path)
    calls: list[str] = []
    with database.session_scope() as session:
        opportunity = _opportunity("resolution-gate", status="APPLICATION_SUBMITTED")
        opportunity.application_url = None
        opportunity.target_status = TargetKind.UNRESOLVED.value
        opportunity.resolved_at = None
        session.add(opportunity)
        session.flush()
        service = TargetResolutionService(session)

        def resolver(_context):  # noqa: ANN001
            calls.append("browser")
            raise AssertionError("excluded status reached resolver")

        with pytest.raises(UserStatusAutomationExcludedError, match="APPLICATION_SUBMITTED"):
            service.resolve(opportunity.id, resolver=resolver)
        with pytest.raises(UserStatusAutomationExcludedError):
            service.record(
                opportunity.id,
                TargetResolution(
                    source_url=opportunity.url,
                    final_url=opportunity.url,
                    kind=TargetKind.JOB_DETAIL,
                ),
            )
        with pytest.raises(UserStatusAutomationExcludedError):
            service.record_failure(opportunity.id, reason_code="should_not_write")

        assert calls == []
        assert opportunity.resolution_attempted_at is None
        assert opportunity.target_status == TargetKind.UNRESOLVED.value


def test_batch_and_autopilot_select_only_eligible_statuses_interested_first(
    tmp_path: Path,
) -> None:
    from app.services.batch_target_resolution import (
        BatchResolveOptions,
        BatchTargetResolutionDriver,
    )

    settings, database, crypto = _runtime(tmp_path, mode="REVIEW_ONLY")
    with database.session_scope() as session:
        fixtures = (
            ("not-applied", "NOT_APPLIED", 1),
            ("interested", "INTERESTED", 90),
            ("not-interested", "NOT_INTERESTED", 0),
            ("submitted", "APPLICATION_SUBMITTED", 0),
        )
        for identifier, status, priority in fixtures:
            opportunity = _opportunity(identifier, status=status, employer=identifier)
            opportunity.application_url = None
            opportunity.target_status = TargetKind.UNRESOLVED.value
            opportunity.resolved_at = None
            opportunity.deadline = date.today() + timedelta(days=30)
            session.add(opportunity)
            session.flush()
            session.add(
                Application(
                    id=f"app-{identifier}",
                    opportunity_id=opportunity.id,
                    state=ApplicationState.DISCOVERED.value,
                    priority=priority,
                )
            )

    selected = BatchTargetResolutionDriver(database, settings)._select(
        BatchResolveOptions(only_unattempted=True)
    )
    assert [item.opportunity_id for item in selected] == ["interested", "not-applied"]

    with database.session_scope() as session:
        for opportunity in session.query(Opportunity).all():
            opportunity.application_url = opportunity.url
            opportunity.target_status = TargetKind.APPLICATION_FORM.value
            opportunity.resolved_at = datetime.now(timezone.utc)
        session.flush()
        scout = ScoutService(session, settings, crypto)
        candidates = scout.autopilot_candidates()
        assert [opportunity.id for _application, opportunity in candidates] == [
            "interested",
            "not-applied",
        ]


@pytest.mark.parametrize(
    "excluded_status",
    ("NOT_INTERESTED", "APPLICATION_SUBMITTED"),
)
def test_autopilot_rechecks_status_before_runner_call(
    tmp_path: Path,
    monkeypatch,
    excluded_status: str,
) -> None:
    settings, database, crypto = _runtime(tmp_path, mode="REVIEW_ONLY")
    with database.session_scope() as session:
        opportunity = _opportunity("action-time", status="INTERESTED")
        application = Application(
            id="app-action-time",
            opportunity=opportunity,
            state=ApplicationState.PACKAGE_PREPARED.value,
        )
        session.add(application)
        session.flush()
        scout = ScoutService(session, settings, crypto)
        monkeypatch.setattr(scout, "autopilot_candidates", lambda: [(application, opportunity)])
        opportunity.user_status = excluded_status
        runner_calls: list[str] = []

        result = scout.run_autopilot(
            lambda *_args: runner_calls.append("runner"),
            max_runs=1,
        )

        assert runner_calls == []
        assert result["processed"] == 1
        assert result["blocked"] == 1
        assert result["details"][0]["result"] == "blocked:user_status"
        assert application.state == ApplicationState.PACKAGE_PREPARED.value


def _manifest(application_id: str) -> dict[str, object]:
    origin = "https://example.test"
    return {
        "application_id": application_id,
        "employer": "Example Capital",
        "role": "Summer Analyst",
        "requisition": "phase25-req",
        "provider": "fixture",
        "application_url": f"{origin}/apply",
        "destination": f"{origin}/submit",
        "form_action": f"{origin}/submit",
        "expected_final_url": f"{origin}/receipt",
        "method": "POST",
        "form_identity": "phase25-form",
        "root_selector": "#application",
        "control_selector": "button[type=submit]",
        "control_fingerprint": "phase25-control",
        "frame_url": f"{origin}/apply",
        "documents": [],
        "answers": [],
    }


@pytest.mark.parametrize(
    "excluded_status",
    ("NOT_INTERESTED", "APPLICATION_SUBMITTED"),
)
def test_submission_authority_rechecks_status_in_atomic_consume(
    tmp_path: Path,
    excluded_status: str,
) -> None:
    from app.services.submission_authority import (
        SubmissionAuthorityError,
        SubmissionAuthorityService,
    )

    _settings, database, _crypto = _runtime(tmp_path)
    with database.session_scope() as session:
        opportunity = _opportunity("authority-gate", status="INTERESTED")
        application = Application(
            id="app-authority-gate",
            opportunity=opportunity,
            state=ApplicationState.READY_TO_SUBMIT.value,
        )
        session.add(application)
        session.flush()
        authority = SubmissionAuthorityService(session).issue(
            application_id=application.id,
            session_id="session-authority-gate",
            manifest=_manifest(application.id),
            destination_origin="https://example.test",
        )
        authority_id = authority.id

    with database.session_scope() as session:
        session.get(Opportunity, "authority-gate").user_status = excluded_status

    with database.session_scope() as session:
        with pytest.raises(SubmissionAuthorityError, match="(?i)user status"):
            SubmissionAuthorityService(session).consume(
                authority_id,
                application_id="app-authority-gate",
                session_id="session-authority-gate",
                manifest=_manifest("app-authority-gate"),
                destination_origin="https://example.test",
            )

    with database.session_scope() as session:
        assert session.get(SubmissionAuthority, authority_id).consumed_at is None
        session.get(Opportunity, "authority-gate").user_status = "INTERESTED"

    with database.session_scope() as session:
        consumed = SubmissionAuthorityService(session).consume(
            authority_id,
            application_id="app-authority-gate",
            session_id="session-authority-gate",
            manifest=_manifest("app-authority-gate"),
            destination_origin="https://example.test",
        )
        assert consumed.consumed_at is not None


@pytest.mark.parametrize(
    "excluded_status",
    ("NOT_INTERESTED", "APPLICATION_SUBMITTED"),
)
def test_cli_apply_refuses_excluded_status_before_runner_creation(
    tmp_path: Path,
    monkeypatch,
    capsys,
    excluded_status: str,
) -> None:
    from app import cli

    settings, database, crypto = _runtime(tmp_path)
    with database.session_scope() as session:
        opportunity = _opportunity("cli-gate", status=excluded_status)
        session.add(
            Application(
                id="app-cli-gate",
                opportunity=opportunity,
                state=ApplicationState.PACKAGE_PREPARED.value,
            )
        )

    calls: list[str] = []

    class ForbiddenRunner:
        def __init__(self, *_args, **_kwargs):
            calls.append("runner-created")

    monkeypatch.setattr(cli, "_runtime", lambda: (settings, database, crypto))
    monkeypatch.setattr(cli, "AutomationRunner", ForbiddenRunner)
    result = cli._command_apply(
        SimpleNamespace(
            application_id="app-cli-gate",
            mode="dry-run",
            confirm_submit="",
            headed=False,
        )
    )

    assert result == 2
    assert calls == []
    assert excluded_status in capsys.readouterr().err
