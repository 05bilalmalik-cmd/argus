from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.domain.states import ApplicationState
from app.main import create_app
from app.models import Application, AuditEvent, AutomationRun, Opportunity


def _client(tmp_path: Path) -> TestClient:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_API_TOKEN": "phase25-api-test",
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
            "ARGUS_ENABLE_NOTIFICATIONS": "false",
        }
    )
    return TestClient(create_app(settings))


def _seed(client: TestClient) -> tuple[str, str]:
    with client.app.state.db.session_scope() as session:
        opportunity = Opportunity(
            id="phase25-api-opportunity",
            employer="Example Capital",
            role_title="Summer Analyst",
            programme_group="summer",
            cycle="2027",
            url="https://example.test/jobs/phase25-api",
            application_url="https://example.test/apply/phase25-api",
            target_status="APPLICATION_FORM",
            resolved_ats_type="fixture",
            application_window_status="OPEN",
            cv_required=False,
        )
        application = Application(
            id="phase25-api-application",
            opportunity=opportunity,
            state=ApplicationState.PACKAGE_PREPARED.value,
            eligibility_json='{"eligible":true}',
            conflict_json='{"blocked":false}',
        )
        session.add(application)
        return opportunity.id, application.id


def test_user_status_rest_round_trip_is_audited_idempotent_and_same_origin_guarded(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        opportunity_id, application_id = _seed(client)

        initial = client.get(f"/api/opportunities/{opportunity_id}/user-status")
        assert initial.status_code == 200
        assert initial.json()["status"] == "NOT_APPLIED"
        assert initial.json()["actor"] == "default"
        assert initial.json()["automation_eligible"] is True

        hostile = client.put(
            f"/api/opportunities/{opportunity_id}/user-status",
            json={"status": "HIREVUE"},
            headers={"Origin": "https://attacker.example"},
        )
        assert hostile.status_code == 403

        changed = client.put(
            f"/api/opportunities/{opportunity_id}/user-status",
            json={"status": "HIREVUE"},
        )
        assert changed.status_code == 200
        assert changed.json()["status"] == "HIREVUE"
        assert changed.json()["actor"] == "user"
        assert changed.json()["changed"] is True
        assert changed.json()["automation_eligible"] is False
        assert "HIREVUE" in changed.json()["automation_exclusion_reason"]

        repeated = client.put(
            f"/api/opportunities/{opportunity_id}/user-status",
            json={"status": "HIREVUE"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["changed"] is False

        assert client.put(
            f"/api/opportunities/{opportunity_id}/user-status",
            json={"status": "HIREVUE", "actor": "import"},
        ).status_code == 422
        assert client.put(
            f"/api/opportunities/{opportunity_id}/user-status",
            json={"status": "MADE_UP"},
        ).status_code == 422

        opportunities = client.get("/api/opportunities").json()
        applications = client.get("/api/applications").json()
        assert opportunities[0]["user_status"] == "HIREVUE"
        assert applications[0]["user_status"] == "HIREVUE"
        assert applications[0]["automation_exclusion_reason"]

        with client.app.state.db.session_scope() as session:
            application = session.get(Application, application_id)
            assert application.state == ApplicationState.PACKAGE_PREPARED.value
            assert session.query(AutomationRun).count() == 0
            events = session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type == "opportunity.user_status_changed"
                )
            ).all()
            assert len(events) == 1
            assert json.loads(events[0].details_json)["new_status"] == "HIREVUE"


@pytest.mark.parametrize(
    "excluded_status",
    ("NOT_INTERESTED", "APPLICATION_SUBMITTED"),
)
def test_excluded_status_is_rejected_before_api_resolution_or_runner_creation(
    tmp_path: Path,
    monkeypatch,
    excluded_status: str,
) -> None:
    with _client(tmp_path) as client:
        opportunity_id, application_id = _seed(client)
        assert client.put(
            f"/api/opportunities/{opportunity_id}/user-status",
            json={"status": excluded_status},
        ).status_code == 200

        calls: list[str] = []

        class ForbiddenRunner:
            def __init__(self, *_args, **_kwargs):
                calls.append("runner-created")

        monkeypatch.setattr("app.routers.api.AutomationRunner", ForbiddenRunner)
        monkeypatch.setattr(
            client.app.state.navigator,
            "start",
            lambda *_args, **_kwargs: calls.append("navigator-started"),
        )
        run = client.post(f"/api/applications/{application_id}/run")
        handoff = client.post(
            f"/api/handoff/applications/{application_id}/start"
        )
        resolve = client.post(
            f"/api/opportunities/{opportunity_id}/resolve-target",
            json={"opportunity_id": opportunity_id, "confirmed": True},
        )

        assert run.status_code == 409
        assert handoff.status_code == 409
        assert resolve.status_code == 409
        assert excluded_status in run.text
        assert excluded_status in handoff.text
        assert excluded_status in resolve.text
        assert calls == []
        with client.app.state.db.session_scope() as session:
            opportunity = session.get(Opportunity, opportunity_id)
            assert opportunity.resolution_attempted_at is None
            assert session.query(AutomationRun).count() == 0
