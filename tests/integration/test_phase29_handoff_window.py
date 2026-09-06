"""Phase 29 Needs-You copy and service ownership integration checks."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, Opportunity


def test_needs_you_names_role_captcha_blank_reasons_and_candidate_submit_boundary(
    tmp_path: Path,
) -> None:
    app = create_app(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
    app.state.ui_v2_enabled = True
    with TestClient(app) as client:
        with client.app.state.db.session_scope() as session:
            opportunity = Opportunity(
                employer="Aquatic Capital Management",
                role_title="Investment Analyst",
                programme_group="summer",
                cycle="2027",
                location="London",
                url="https://example.test/aquatic-source",
                application_url="https://example.test/aquatic-application",
                target_status=TargetKind.HUMAN_CHALLENGE.value,
                source="phase29-test",
            )
            session.add(opportunity)
            session.flush()
            application = Application(
                opportunity_id=opportunity.id,
                state=ApplicationState.NEEDS_USER.value,
                priority=90,
                risk_level=3,
                next_action=(
                    "CAPTCHA needs solving. Fields left blank: Work authorisation "
                    "— no exact stored-profile match; Academic classification — "
                    "plausibility check rejected the stored value."
                ),
            )
            session.add(application)
            session.flush()

        response = client.get("/needs-you?group=captcha")

    assert response.status_code == 200
    assert "Aquatic Capital Management" in response.text
    assert "Investment Analyst" in response.text
    assert "CAPTCHA needs solving" in response.text
    assert "Work authorisation" in response.text
    assert "no exact stored-profile match" in response.text
    assert "Academic classification" in response.text
    assert "plausibility check rejected" in response.text
    assert "Press Submit yourself" in response.text

