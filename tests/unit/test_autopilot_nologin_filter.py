"""Tests for the autopilot login-wall skip filter.

Candidates whose resolved target kind is AUTH_WALL or HUMAN_CHALLENGE are
skipped (counted separately) while the ARGUS_AUTOPILOT_SKIP_LOGIN_REQUIRED
setting is enabled (default True).
"""
from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.models import Application, Opportunity
from app.scouting.programmes import ProgrammeType
from app.scouting.service import ScoutService


def CryptoBox_from_path(key_path):  # local alias keeps imports at top tidy
    from app.security.crypto import CryptoBox

    return CryptoBox.from_path(key_path)


def _setup(tmp_path: Path, **env_overrides):
    env = {"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY"}
    env.update(env_overrides)
    settings = Settings.load(env)
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox_from_path(settings.secret_key_path)
    return settings, db, crypto


def _prepared_candidate(session, *, employer: str = "Prepared Bank"):
    opportunity = Opportunity(
        employer=employer,
        role_title="Summer Analyst",
        programme_group=ProgrammeType.SUMMER.value,
        cycle="2027",
        url=f"https://boards.greenhouse.io/{employer.casefold().replace(' ', '-')}/1",
        source="test",
        cv_required=False,
    )
    session.add(opportunity)
    session.flush()
    application = Application(
        opportunity_id=opportunity.id,
        state=ApplicationState.PACKAGE_PREPARED.value,
        priority=50,
    )
    session.add(application)
    session.flush()
    return application, opportunity


def _review_runner(calls: list):
    def runner_factory(application_id, mode, headed):
        calls.append(mode.value)
        return {"state": "PACKAGE_PREPARED", "risk_level": 0, "adapter": "greenhouse"}

    return runner_factory


def test_auth_wall_candidate_is_skipped(tmp_path: Path, monkeypatch) -> None:
    settings, db, crypto = _setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        application, opportunity = _prepared_candidate(session)
        opportunity.target_status = TargetKind.AUTH_WALL.value
        opportunity.application_url = opportunity.url
        session.flush()
        monkeypatch.setattr(
            scout, "autopilot_candidates", lambda: [(application, opportunity)]
        )
        calls: list = []

        result = scout.run_autopilot(_review_runner(calls))

        assert result["skipped_login_required"] == 1
        assert result["processed"] == 0
        assert result["blocked"] == 0
        assert result["failed"] == 0
        assert calls == []
        assert result["details"][0]["result"] == "skipped:login_required"
        assert "reason" in result["details"][0]


def test_human_challenge_candidate_is_skipped(tmp_path: Path, monkeypatch) -> None:
    settings, db, crypto = _setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        application, opportunity = _prepared_candidate(session)
        opportunity.target_status = TargetKind.HUMAN_CHALLENGE.value
        opportunity.application_url = opportunity.url
        session.flush()
        monkeypatch.setattr(
            scout, "autopilot_candidates", lambda: [(application, opportunity)]
        )
        calls: list = []

        result = scout.run_autopilot(_review_runner(calls))

        assert result["skipped_login_required"] == 1
        assert result["processed"] == 0
        assert calls == []
        assert result["details"][0]["result"] == "skipped:login_required"


def test_application_form_candidate_is_processed(tmp_path: Path, monkeypatch) -> None:
    settings, db, crypto = _setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        application, opportunity = _prepared_candidate(session)
        opportunity.target_status = TargetKind.APPLICATION_FORM.value
        opportunity.application_url = opportunity.url
        session.flush()
        monkeypatch.setattr(
            scout, "autopilot_candidates", lambda: [(application, opportunity)]
        )
        calls: list = []

        result = scout.run_autopilot(_review_runner(calls))

        assert result["skipped_login_required"] == 0
        assert result["processed"] == 1
        assert calls == ["review"]
        assert result["details"][0]["result"] == "review_only"


def test_auth_wall_not_skipped_when_setting_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    settings, db, crypto = _setup(
        tmp_path, ARGUS_AUTOPILOT_SKIP_LOGIN_REQUIRED="false"
    )
    assert settings.autopilot_skip_login_required is False
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        application, opportunity = _prepared_candidate(session)
        opportunity.target_status = TargetKind.AUTH_WALL.value
        opportunity.application_url = opportunity.url
        session.flush()
        monkeypatch.setattr(
            scout, "autopilot_candidates", lambda: [(application, opportunity)]
        )
        calls: list = []

        result = scout.run_autopilot(_review_runner(calls))

        assert result["skipped_login_required"] == 0
        assert result["processed"] == 1
        assert calls == ["review"]
        assert result["details"][0]["result"] != "skipped:login_required"
