"""Tests for POST /api/scout/autopilot after batch submission was removed.

Forensic root cause 7: a single boolean (request flag + env flag) once
confirmed every ready application at once. That behaviour is retired — the
endpoint must not expose it, honour it, or run a second submit pass.

These tests use a fake runner factory (monkeypatched over
``_runner_factory``) so no browser automation runs.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, ConflictRule, Opportunity


ARMED_SETTINGS = {"ARGUS_AUTOMATION_MODE": "ARMED"}


def _seed_candidate(session, *, employer: str = "Batch Bank") -> Application:
    application_url = f"https://boards.greenhouse.io/{employer.casefold()}/1"
    opportunity = Opportunity(
        employer=employer,
        role_title="Summer Analyst",
        programme_group="summer",
        cycle="2027",
        url=application_url,
        application_url=application_url,
        target_status=TargetKind.APPLICATION_ENTRY.value,
        resolved_ats_type="greenhouse",
        resolution_evidence_json='{"identity_verified": true, "source": "test_fixture"}',
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
    return application


def _client(tmp_path: Path) -> TestClient:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path), **ARMED_SETTINGS})
    return TestClient(create_app(settings))


def _install_fake_runner(monkeypatch, db, modes: list[str]) -> None:
    """Replace the endpoint's runner with a risk-0 fake that persists a
    verified submission on SUBMIT (mirrors AutomationRunner's own session)."""

    def fake_runner_factory(request):
        def run(application_id: str, mode, headed: bool) -> dict[str, object]:
            modes.append(mode.value)
            if mode.value == "submit":
                with db.session_scope() as runner_session:
                    persisted = runner_session.get(Application, application_id)
                    persisted.state = ApplicationState.CONFIRMATION_VERIFIED.value
                    persisted.submission_reference = f"BATCH-{application_id[:8]}"
                return {
                    "state": ApplicationState.CONFIRMATION_VERIFIED.value,
                    "risk_level": 0,
                    "adapter": "greenhouse",
                    "receipt": {"reference": "IGNORED-RETURN"},
                }
            return {
                "state": ApplicationState.READY_TO_SUBMIT.value,
                "risk_level": 0,
                "adapter": "greenhouse",
            }

        return run

    monkeypatch.setattr("app.routers.scout._runner_factory", fake_runner_factory)


def test_confirm_submissions_param_is_not_exposed(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    schema = client.get("/openapi.json").json()
    post = schema["paths"]["/api/scout/autopilot"]["post"]
    params = {p["name"] for p in post.get("parameters", [])}
    assert "confirm_submissions" not in params


def test_confirm_submissions_true_cannot_authorize_submission(
    tmp_path: Path, monkeypatch
) -> None:
    modes: list[str] = []
    with _client(tmp_path) as client:
        db = client.app.state.db
        with db.session_scope() as session:
            session.add(
                ConflictRule(
                    employer_pattern="Batch Bank", cycle="2027", max_applications=2
                )
            )
            _seed_candidate(session)
        _install_fake_runner(monkeypatch, db, modes)

        response = client.post(
            "/api/scout/autopilot", params={"confirm_submissions": True}
        )

    assert response.status_code == 200  # FastAPI ignores unknown query params
    payload = response.json()
    # The flag is inert: no batch phase, nothing submitted.
    assert "batch_submit" not in payload
    assert payload["submitted"] == 0


def test_env_flag_alone_never_enables_batch_submit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ARGUS_AUTOPILOT_SUBMIT", "true")
    modes: list[str] = []
    with _client(tmp_path) as client:
        db = client.app.state.db
        with db.session_scope() as session:
            session.add(
                ConflictRule(
                    employer_pattern="Batch Bank", cycle="2027", max_applications=2
                )
            )
            application = _seed_candidate(session)
            application_id = application.id
        _install_fake_runner(monkeypatch, db, modes)

        response = client.post("/api/scout/autopilot")

    assert response.status_code == 200
    payload = response.json()
    assert "batch_submit" not in payload
    assert payload["submitted"] == 0
    assert payload["details"][0]["result"] == "awaiting_action_time_confirmation"
    assert modes == ["review"]

    # Nothing was submitted: assert the durable row, not only the response.
    with db.session_scope() as check:
        persisted = check.get(Application, application_id)
        assert persisted is not None
        assert persisted.state == ApplicationState.PACKAGE_PREPARED.value
        assert persisted.submission_reference in (None, "")


def test_ready_apps_stop_at_confirmation_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    modes: list[str] = []
    with _client(tmp_path) as client:
        db = client.app.state.db
        with db.session_scope() as session:
            session.add(
                ConflictRule(
                    employer_pattern="Batch Bank", cycle="2027", max_applications=2
                )
            )
            application = _seed_candidate(session)
            application_id = application.id
        _install_fake_runner(monkeypatch, db, modes)

        response = client.post("/api/scout/autopilot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["submitted"] == 0
    entry = next(
        e for e in payload["details"] if e["application_id"] == application_id
    )
    assert entry["result"] == "awaiting_action_time_confirmation"
    assert entry.get("confirmation_required") is True
    assert set(modes) == {"review"}
