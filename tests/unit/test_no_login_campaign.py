"""No-login campaign tests (DB-backed, no monkeypatching of candidates).

Covers the demonstrated defects without touching the network, browser, or
target trust:
- correctly classified AUTH_WALL / HUMAN_CHALLENGE rows stay unrunnable yet
  are visible in sweep skip counts/details;
- a parked (non-runnable) candidate sorting ahead of a genuine no-login form
  does not consume the runnable budget (max_runs=1 still reviews the form);
- two default scheduled passes never submit while a runnable form proceeds;
- the explicit opt-out flag never weakens target verification end-to-end.
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
from app.scouting.programmes import ProgrammeType
from app.scouting.service import ScoutService


def _crypto(key_path):  # local alias keeps imports at top tidy
    from app.security.crypto import CryptoBox

    return CryptoBox.from_path(key_path)


def _setup(tmp_path: Path, **env_overrides):
    env = {"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY"}
    env.update(env_overrides)
    settings = Settings.load(env)
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    return settings, db, _crypto(settings.secret_key_path)


def _opportunity(
    session,
    *,
    employer: str,
    slug: str,
    target_status: str,
    application_url: str | None = None,
    with_verified_stamp: bool = False,
) -> Opportunity:
    url = f"https://boards.greenhouse.io/{slug}/jobs/1"
    opportunity = Opportunity(
        employer=employer,
        role_title="Summer Analyst",
        programme_group=ProgrammeType.SUMMER.value,
        cycle="2027",
        url=url,
        source="test",
        cv_required=False,
        application_window_status="OPEN",
        target_status=target_status,
        application_url=application_url if application_url is not None else url,
    )
    if with_verified_stamp:
        opportunity.resolved_at = datetime.now(timezone.utc)
        opportunity.resolved_ats_type = "greenhouse"
    session.add(opportunity)
    session.flush()
    return opportunity


def _application(session, opportunity: Opportunity, *, state: str, priority: int) -> Application:
    application = Application(
        opportunity_id=opportunity.id,
        state=state,
        priority=priority,
    )
    session.add(application)
    session.flush()
    return application


class _ReviewRunner:
    """Fake runner: records modes/ids, never touches network or browser."""

    def __init__(self) -> None:
        self.modes: list[RunMode] = []
        self.application_ids: list[str] = []

    def __call__(self, application_id, mode, headed):  # noqa: ANN001
        self.modes.append(mode)
        self.application_ids.append(application_id)
        return {"state": "PACKAGE_PREPARED", "risk_level": 0, "adapter": "greenhouse"}


def test_known_walls_visible_skipped_never_runnable(tmp_path: Path) -> None:
    settings, db, crypto = _setup(tmp_path)
    assert settings.autopilot_skip_login_required is True
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        wall_auth = _opportunity(
            session, employer="Wall Bank", slug="wall-bank",
            target_status=TargetKind.AUTH_WALL.value,
        )
        wall_captcha = _opportunity(
            session, employer="Captcha Bank", slug="captcha-bank",
            target_status=TargetKind.HUMAN_CHALLENGE.value,
        )
        genuine = _opportunity(
            session, employer="Open Bank", slug="open-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
            with_verified_stamp=True,
        )
        _application(session, wall_auth, state=ApplicationState.PACKAGE_PREPARED.value, priority=5)
        _application(session, wall_captcha, state=ApplicationState.PACKAGE_PREPARED.value, priority=6)
        genuine_app = _application(
            session, genuine, state=ApplicationState.PACKAGE_PREPARED.value, priority=50
        )

        # Walls must never enter the runnable candidate set.
        runnable_ids = {opportunity.id for _, opportunity in scout.autopilot_candidates()}
        assert genuine.id in runnable_ids
        assert wall_auth.id not in runnable_ids
        assert wall_captcha.id not in runnable_ids

        runner = _ReviewRunner()
        result = scout.run_autopilot(runner)

        # ... yet both walls are visible in the sweep skip accounting ...
        assert result["skipped_login_required"] == 2
        skipped = [d for d in result["details"] if d["result"] == "skipped:login_required"]
        assert {d["employer"] for d in skipped} == {"Wall Bank", "Captcha Bank"}
        # ... while the genuine form still receives its REVIEW and nothing submits.
        assert runner.application_ids == [genuine_app.id]
        assert all(mode is RunMode.REVIEW for mode in runner.modes)
        assert result["submitted"] == 0
        assert result["processed"] == 1


def test_parked_row_does_not_starve_genuine_with_single_slot(tmp_path: Path) -> None:
    settings, db, crypto = _setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        parked = _opportunity(
            session, employer="Parked Bank", slug="parked-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
            with_verified_stamp=True,
        )
        genuine = _opportunity(
            session, employer="Open Bank", slug="open-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
            with_verified_stamp=True,
        )
        # READY_TO_SUBMIT sorts first (priority 10) but can never run: the
        # pipeline parks it without invoking the runner.
        _application(session, parked, state=ApplicationState.READY_TO_SUBMIT.value, priority=10)
        genuine_app = _application(
            session, genuine, state=ApplicationState.PACKAGE_PREPARED.value, priority=50
        )

        runner = _ReviewRunner()
        result = scout.run_autopilot(runner, max_runs=1)

        # The single runnable slot reaches the genuine form's REVIEW ...
        assert runner.application_ids == [genuine_app.id]
        assert all(mode is RunMode.REVIEW for mode in runner.modes)
        assert result["submitted"] == 0
        # ... while the parked row is still counted honestly, not silently dropped.
        assert result["processed"] == 2
        assert result["blocked"] == 1
        parked_entries = [
            d for d in result["details"] if d["employer"] == "Parked Bank"
        ]
        assert len(parked_entries) == 1
        assert parked_entries[0]["result"] == "stopped:READY_TO_SUBMIT"


def test_two_scheduled_passes_never_submit_genuine_proceeds(tmp_path: Path) -> None:
    settings, db, crypto = _setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        genuine = _opportunity(
            session, employer="Open Bank", slug="open-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
            with_verified_stamp=True,
        )
        genuine_app = _application(
            session, genuine, state=ApplicationState.DISCOVERED.value, priority=50
        )
        runner = _ReviewRunner()

        first = scout.run_autopilot(runner)
        second = scout.run_autopilot(runner)

        for result in (first, second):
            assert result["submitted"] == 0
            assert result["skipped_login_required"] == 0
        assert runner.application_ids == [genuine_app.id, genuine_app.id]
        assert all(mode is RunMode.REVIEW for mode in runner.modes)
        assert RunMode.SUBMIT not in runner.modes
        assert first["details"][0]["result"] == "review_only"
        assert second["details"][0]["result"] == "review_only"


def test_skip_disabled_never_weakens_target_verification(tmp_path: Path) -> None:
    settings, db, crypto = _setup(tmp_path, ARGUS_AUTOPILOT_SKIP_LOGIN_REQUIRED="false")
    assert settings.autopilot_skip_login_required is False
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        wall = _opportunity(
            session, employer="Wall Bank", slug="wall-bank",
            target_status=TargetKind.AUTH_WALL.value,
        )
        genuine = _opportunity(
            session, employer="Open Bank", slug="open-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
            with_verified_stamp=True,
        )
        _application(session, wall, state=ApplicationState.PACKAGE_PREPARED.value, priority=5)
        genuine_app = _application(
            session, genuine, state=ApplicationState.PACKAGE_PREPARED.value, priority=50
        )

        # Even with the flag off, a classified wall is not a runnable target:
        # Opportunity.automation_url is unchanged, so it never enters candidates.
        runnable_ids = {opportunity.id for _, opportunity in scout.autopilot_candidates()}
        assert wall.id not in runnable_ids

        runner = _ReviewRunner()
        result = scout.run_autopilot(runner)

        assert result["skipped_login_required"] == 0
        assert runner.application_ids == [genuine_app.id]
        assert result["processed"] == 1
        assert result["submitted"] == 0
