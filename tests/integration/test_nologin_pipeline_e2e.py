"""No-login ATS pipeline end-to-end (mocked runner only).

Proves a no-login ATS application form (greenhouse-style URL, resolved
target kind APPLICATION_FORM) travels from discovery to "ready for human
confirmation" without ever attempting a real submission.

No network, no real browser, no database outside the tmp dir: the runner
is a fake that records the RunMode it was asked for.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.automation.types import RunMode
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.models import Application, Opportunity
from app.scouting.service import ScoutService
from app.security.crypto import CryptoBox

# No-login ATS form: a public greenhouse job board posting. No auth wall,
# no login step; the resolved target is the form itself.
NO_LOGIN_ATS_URL = "https://boards.greenhouse.io/acmecapital/jobs/12345678"
DISCOVERY_URL = "https://example.test/listings/acme-summer-analyst"


def _runtime(tmp_path: Path, *, mode: str):
    """Established fixture pattern: temp SQLite session + Settings + crypto."""
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": mode,
        }
    )
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    crypto = CryptoBox.from_path(settings.secret_key_path)
    return settings, database, crypto


def _seed_nologin_form(session) -> tuple[Application, Opportunity]:
    """Seed a DISCOVERED opportunity resolved to a no-login ATS form."""
    opportunity = Opportunity(
        employer="Acme Capital",
        role_title="Summer Analyst",
        programme_group="summer",
        location="London",
        cycle="2027",
        url=DISCOVERY_URL,
        application_url=NO_LOGIN_ATS_URL,
        target_status=TargetKind.APPLICATION_FORM.value,
        resolved_ats_type="greenhouse",
        resolved_at=datetime.now(timezone.utc),
        application_window_status="OPEN",
        cv_required=False,
        cover_letter_required=False,
        source="test",
    )
    session.add(opportunity)
    session.flush()
    application = Application(
        opportunity_id=opportunity.id,
        state=ApplicationState.DISCOVERED.value,
        priority=50,
    )
    session.add(application)
    session.flush()
    return application, opportunity


class _FakeRunner:
    """Fake runner_factory: records RunModes, never touches network/browser."""

    def __init__(self) -> None:
        self.modes: list[RunMode] = []
        self.application_ids: list[str] = []

    def __call__(self, application_id, mode, headed):  # noqa: ANN001
        self.modes.append(mode)
        self.application_ids.append(application_id)
        # Shape matches exactly what ScoutService.run_autopilot reads:
        # outcome.get("risk_level"), .get("adapter"), .get("state"),
        # and (for handoff runs) .get("blocked_reasons").
        return {
            "state": "READY_TO_SUBMIT",
            "risk_level": 0,
            "adapter": "greenhouse",
        }


def test_nologin_form_reaches_review_without_submit(tmp_path: Path) -> None:
    """Scheduled path (no confirmed ids): discovery -> ready, never submit."""
    settings, database, crypto = _runtime(tmp_path, mode="REVIEW_ONLY")
    with database.session_scope() as session:
        application, opportunity = _seed_nologin_form(session)
        application_id = application.id
        assert opportunity.target_status == TargetKind.APPLICATION_FORM.value
        assert opportunity.application_url == NO_LOGIN_ATS_URL

        scout = ScoutService(session, settings, crypto)
        fake = _FakeRunner()
        # Scheduled callers intentionally omit confirmed_application_ids,
        # so the run must stop at the confirmation boundary.
        result = scout.run_autopilot(fake)

        # Never attempted a real submission.
        assert fake.modes, "fake runner should have been invoked for review"
        assert all(mode is RunMode.REVIEW for mode in fake.modes)
        assert RunMode.SUBMIT not in fake.modes
        assert "submit" not in {str(mode).lower() for mode in fake.modes}
        assert result["submitted"] == 0

        # Pipeline carried discovery -> review/ready for human confirmation.
        details = result["details"]
        assert result["processed"] == 1
        assert details[0]["application_id"] == application_id
        assert details[0]["result"] == "review_only"
        assert details[0]["state"] == "READY_TO_SUBMIT"

        fresh = session.get(Application, application_id)
        assert fresh is not None
        assert fresh.state == ApplicationState.PACKAGE_PREPARED.value


def test_confirmation_alone_cannot_arm_submission(tmp_path: Path) -> None:
    """Confirmed id + NOT-armed settings: the armed gate still blocks submit."""
    settings, database, crypto = _runtime(tmp_path, mode="REVIEW_ONLY")
    assert settings.submission_armed is False
    with database.session_scope() as session:
        application, opportunity = _seed_nologin_form(session)
        application_id = application.id
        assert opportunity.target_status == TargetKind.APPLICATION_FORM.value

        scout = ScoutService(session, settings, crypto)
        fake = _FakeRunner()
        result = scout.run_autopilot(
            fake,
            confirmed_application_ids={application_id},
        )

        assert fake.modes, "fake runner should have been invoked for review"
        assert all(mode is RunMode.REVIEW for mode in fake.modes)
        assert RunMode.SUBMIT not in fake.modes
        assert "submit" not in {str(mode).lower() for mode in fake.modes}
        assert result["submitted"] == 0
        assert result["submission_armed"] is False
        assert result["details"][0]["result"] == "review_only"
