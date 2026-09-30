"""Confirmation-scope campaign: exact application-ID scoping for batch confirm.

Real DB-backed tests, mock runner only. No monkeypatching of
``autopilot_candidates`` at the service level; the handler test drives the
real ``POST /api/review-queue/confirm`` handler with only
``AutomationRunner`` faked.

Contract under test:
- ``application_scope=None``: ordinary autonomous sweep (all candidates).
- explicit ``application_scope`` (incl. empty): only those application IDs may
  be examined/run/reported; empty/unknown/selected-wall scope never falls
  back to all candidates.
- READY_TO_SUBMIT selections and any batch-scoped submit path never gain
  submit authority: batch IDs alone are not session_id+authority_id, so the
  service returns an explicit per-application action-time confirmation
  requirement pointing at POST /api/applications/{id}/run?mode=submit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.automation.types import RunMode
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, ConflictRule, Opportunity
from app.scouting.programmes import ProgrammeType
from app.scouting.service import ScoutService


def _crypto(key_path):
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
        application_url=url,
        resolved_at=datetime.now(timezone.utc),
        resolved_ats_type="greenhouse",
    )
    session.add(opportunity)
    session.flush()
    return opportunity


def _application(
    session, opportunity: Opportunity, *, state: str, priority: int, app_id: str | None = None
) -> Application:
    kwargs: dict[str, object] = {
        "opportunity_id": opportunity.id,
        "state": state,
        "priority": priority,
    }
    if app_id is not None:
        kwargs["id"] = app_id
    application = Application(**kwargs)  # type: ignore[arg-type]
    session.add(application)
    session.flush()
    return application


class _ReviewRunner:
    """Fake runner_factory: records ids/modes, never touches network/browser."""

    def __init__(self, *, state: str = "PACKAGE_PREPARED") -> None:
        self.application_ids: list[str] = []
        self.modes: list[str] = []
        self._state = state

    def __call__(self, application_id, mode, headed):  # noqa: ANN001
        self.application_ids.append(application_id)
        self.modes.append(mode.value if hasattr(mode, "value") else str(mode))
        return {"state": self._state, "risk_level": 0, "adapter": "greenhouse"}


# ------------------------------------------------------- service-level scope


def test_scoped_confirm_reviews_only_selected(tmp_path: Path) -> None:
    settings, db, crypto = _setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        unrelated = _opportunity(
            session, employer="Unrelated Bank", slug="unrelated-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
        )
        selected = _opportunity(
            session, employer="Selected Bank", slug="selected-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
        )
        unrelated_app = _application(
            session, unrelated, state=ApplicationState.PACKAGE_PREPARED.value, priority=5
        )
        selected_app = _application(
            session, selected, state=ApplicationState.PACKAGE_PREPARED.value, priority=50
        )

        runner = _ReviewRunner()
        result = scout.run_autopilot(
            runner,
            max_runs=1,
            application_scope=[selected_app.id],
            confirmed_application_ids=[selected_app.id],
        )

        assert runner.application_ids == [selected_app.id]
        assert [d["application_id"] for d in result["details"]] == [selected_app.id]
        assert result["details"][0]["result"] == "review_only"
        assert result["submitted"] == 0
        assert session.get(Application, unrelated_app.id).state == (
            ApplicationState.PACKAGE_PREPARED.value
        )


def test_empty_scope_runs_nothing_and_never_falls_back(tmp_path: Path) -> None:
    settings, db, crypto = _setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        genuine = _opportunity(
            session, employer="Open Bank", slug="open-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
        )
        _application(session, genuine, state=ApplicationState.PACKAGE_PREPARED.value, priority=5)

        runner = _ReviewRunner()
        result = scout.run_autopilot(runner, application_scope=[])

        assert runner.application_ids == []
        assert result["processed"] == 0
        assert result["attempted_runs"] == 0
        assert result["submitted"] == 0
        assert result["skipped_login_required"] == 0
        assert result["details"] == []


def test_unknown_scope_runs_nothing_and_reports_unknown(tmp_path: Path) -> None:
    settings, db, crypto = _setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        genuine = _opportunity(
            session, employer="Open Bank", slug="open-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
        )
        _application(session, genuine, state=ApplicationState.PACKAGE_PREPARED.value, priority=5)

        runner = _ReviewRunner()
        result = scout.run_autopilot(runner, application_scope=["no-such-application"])

        assert runner.application_ids == []
        assert result["processed"] == 0
        assert result["submitted"] == 0
        assert [d["application_id"] for d in result["details"]] == ["no-such-application"]
        assert result["details"][0]["result"] == "unknown_application_id"


def test_selected_wall_scope_skips_wall_without_fallback(tmp_path: Path) -> None:
    settings, db, crypto = _setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        wall = _opportunity(
            session, employer="Wall Bank", slug="wall-bank",
            target_status=TargetKind.AUTH_WALL.value,
        )
        genuine = _opportunity(
            session, employer="Open Bank", slug="open-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
        )
        wall_app = _application(
            session, wall, state=ApplicationState.PACKAGE_PREPARED.value, priority=5
        )
        genuine_app = _application(
            session, genuine, state=ApplicationState.PACKAGE_PREPARED.value, priority=50
        )

        runner = _ReviewRunner()
        result = scout.run_autopilot(runner, application_scope=[wall_app.id])

        assert runner.application_ids == []
        assert result["processed"] == 0
        assert result["submitted"] == 0
        assert result["skipped_login_required"] == 1
        assert [d["application_id"] for d in result["details"]] == [wall_app.id]
        assert result["details"][0]["result"] == "skipped:login_required"
        assert genuine_app.id not in [d["application_id"] for d in result["details"]]


def test_two_walls_before_form_with_single_slot(tmp_path: Path) -> None:
    settings, db, crypto = _setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        for employer, slug, kind, priority in [
            ("Wall Bank", "wall-bank", TargetKind.AUTH_WALL.value, 1),
            ("Captcha Bank", "captcha-bank", TargetKind.HUMAN_CHALLENGE.value, 2),
            ("Open Bank", "open-bank", TargetKind.APPLICATION_FORM.value, 50),
        ]:
            opportunity = _opportunity(
                session, employer=employer, slug=slug, target_status=kind
            )
            _application(
                session, opportunity,
                state=ApplicationState.PACKAGE_PREPARED.value, priority=priority,
            )
            if employer == "Open Bank":
                form_id = opportunity.application.id

        runner = _ReviewRunner()
        result = scout.run_autopilot(runner, max_runs=1)

        assert runner.application_ids == [form_id]
        assert result["attempted_runs"] == 1
        assert result["skipped_login_required"] == 2
        assert result["submitted"] == 0


def test_selected_ready_requires_action_time_confirmation(tmp_path: Path) -> None:
    settings, db, crypto = _setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        ready = _opportunity(
            session, employer="Ready Bank", slug="ready-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
        )
        ready_app = _application(
            session, ready, state=ApplicationState.READY_TO_SUBMIT.value, priority=5
        )

        runner = _ReviewRunner()
        result = scout.run_autopilot(
            runner,
            application_scope=[ready_app.id],
            confirmed_application_ids=[ready_app.id],
        )

        assert runner.application_ids == []
        assert result["submitted"] == 0
        assert result["needs_user"] == 1
        assert result["processed"] == 1
        entry = result["details"][0]
        assert entry["application_id"] == ready_app.id
        assert entry["result"] == "awaiting_action_time_confirmation"
        assert entry["confirmation_required"] is True
        assert f"/api/applications/{ready_app.id}/run" in str(entry.get("submit_route", ""))
        assert "session_id" in str(entry.get("reason", ""))
        # Queue item stays visible for the human; nothing was submitted.
        assert session.get(Application, ready_app.id).state == (
            ApplicationState.READY_TO_SUBMIT.value
        )


def test_batch_scope_never_submits_even_when_armed(tmp_path: Path) -> None:
    settings, db, crypto = _setup(tmp_path, ARGUS_AUTOMATION_MODE="ARMED")
    assert settings.submission_armed is True
    with db.session_scope() as session:
        session.add(
            ConflictRule(employer_pattern="Armed Bank", cycle="2027", max_applications=2)
        )
        session.flush()
        scout = ScoutService(session, settings, crypto)
        opportunity = _opportunity(
            session, employer="Armed Bank", slug="armed-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
        )
        application = _application(
            session, opportunity, state=ApplicationState.PACKAGE_PREPARED.value, priority=5
        )

        runner = _ReviewRunner(state="READY_TO_SUBMIT")
        result = scout.run_autopilot(
            runner,
            application_scope=[application.id],
            confirmed_application_ids=[application.id],
        )

        assert runner.modes == ["review"]
        assert result["submitted"] == 0
        assert result["details"][0]["result"] == "awaiting_action_time_confirmation"
        assert result["details"][0]["confirmation_required"] is True


def test_unscoped_sweep_semantics_preserved(tmp_path: Path) -> None:
    settings, db, crypto = _setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        genuine = _opportunity(
            session, employer="Open Bank", slug="open-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
        )
        parked = _opportunity(
            session, employer="Parked Bank", slug="parked-bank",
            target_status=TargetKind.APPLICATION_FORM.value,
        )
        genuine_app = _application(
            session, genuine, state=ApplicationState.PACKAGE_PREPARED.value, priority=50
        )
        _application(session, parked, state=ApplicationState.READY_TO_SUBMIT.value, priority=60)

        runner = _ReviewRunner()
        result = scout.run_autopilot(runner)

        assert runner.application_ids == [genuine_app.id]
        parked_entries = [d for d in result["details"] if d["employer"] == "Parked Bank"]
        assert len(parked_entries) == 1
        assert parked_entries[0]["result"] == "stopped:READY_TO_SUBMIT"


# ------------------------------------------------------- handler-level scope


class _FakeAutomationRunner:
    """Stand-in for AutomationRunner: records calls, returns review outcomes."""

    calls: list[tuple[str, object]] = []

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN001, ANN002
        pass

    def run(self, application_id: str, mode, headed: bool = False, **kwargs):  # noqa: ANN001, ANN002
        type(self).calls.append((application_id, mode))
        return {"state": "PACKAGE_PREPARED", "risk_level": 0, "adapter": "greenhouse"}


def _seed_handler_row(session, *, app_id: str, employer: str, slug: str, priority: int) -> str:
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
        target_status=TargetKind.APPLICATION_FORM.value,
        application_url=url,
        resolved_at=datetime.now(timezone.utc),
        resolved_ats_type="greenhouse",
    )
    session.add(opportunity)
    session.flush()
    application = Application(
        id=app_id,
        opportunity_id=opportunity.id,
        state=ApplicationState.PACKAGE_PREPARED.value,
        priority=priority,
    )
    session.add(application)
    session.flush()
    return application.id


def test_confirm_handler_reviews_only_selected_application(tmp_path: Path) -> None:
    settings = Settings.load(
        {"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY"}
    )
    _FakeAutomationRunner.calls = []
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session_scope() as session:
            _seed_handler_row(
                session, app_id="app-scope-a", employer="Unrelated Bank",
                slug="unrelated-bank", priority=5,
            )
            _seed_handler_row(
                session, app_id="app-scope-b", employer="Selected Bank",
                slug="selected-bank", priority=50,
            )
        with patch(
            "app.routers.review_queue.AutomationRunner", _FakeAutomationRunner
        ):
            response = client.post(
                "/api/review-queue/confirm", json={"application_ids": ["app-scope-b"]}
            )
        assert response.status_code == 200, response.text
        payload = response.json()
        reviewed = [call[0] for call in _FakeAutomationRunner.calls]
        assert reviewed == ["app-scope-b"]
        assert [d["application_id"] for d in payload["details"]] == ["app-scope-b"]
        assert payload["details"][0]["result"] == "review_only"
        assert payload["submitted"] == 0
        assert payload["submission_armed"] is False
        with client.app.state.db.session_scope() as session:
            assert session.get(Application, "app-scope-a").state == (
                ApplicationState.PACKAGE_PREPARED.value
            )


def test_confirm_handler_unknown_id_runs_nothing(tmp_path: Path) -> None:
    settings = Settings.load(
        {"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY"}
    )
    _FakeAutomationRunner.calls = []
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session_scope() as session:
            _seed_handler_row(
                session, app_id="app-scope-a", employer="Unrelated Bank",
                slug="unrelated-bank", priority=5,
            )
        with patch(
            "app.routers.review_queue.AutomationRunner", _FakeAutomationRunner
        ):
            response = client.post(
                "/api/review-queue/confirm", json={"application_ids": ["app-unknown"]}
            )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert _FakeAutomationRunner.calls == []
        assert payload["processed"] == 0
        assert payload["submitted"] == 0
        assert [d["application_id"] for d in payload["details"]] == ["app-unknown"]
        assert payload["details"][0]["result"] == "unknown_application_id"
