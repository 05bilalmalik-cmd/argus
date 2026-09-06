"""Phase S4 tests for the operator-supplied application URL (fix-link) affordance.

Every browser-like target in this module is a loopback lab URL.  No test
contacts a public host or invokes submission authority.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.automation.host_policy import origin_for_url
from app.config import Settings
from app.domain.states import ApplicationState, UserApplicationStatus
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, AuditEvent, Opportunity

LAB_APPLICATION_URL = "https://boards.greenhouse.io/greenhouse/jobs/1234567"
LAB_INVALID_URL = "not-a-url"
LAB_INSECURE_URL = "http://boards.greenhouse.io/jobs/1234567"


def _client(tmp_path: Path) -> TestClient:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    app = create_app(settings)
    app.state.ui_v2_enabled = True
    app.state.manual_target_lab_enabled = False
    return TestClient(app)


def _seed_with_application(
    client: TestClient,
    *,
    employer: str = "S4 Test Employer",
    role: str = "S4 Summer Analyst",
    target_status: TargetKind = TargetKind.MISSING_EMPLOYER_LINK,
    application_url: str | None = None,
    application_url_provenance: str = "",
) -> Application:
    with client.app.state.db.session_scope() as session:
        opportunity = Opportunity(
            employer=employer,
            role_title=role,
            programme_group="summer",
            cycle="2027",
            location="London",
            url="https://boards.greenhouse.io/s4",
            source="s4_test",
            ats_type="greenhouse",
            user_status=UserApplicationStatus.NOT_APPLIED.value,
            target_status=target_status.value,
            application_url=application_url,
        )
        opportunity.application_url_provenance = application_url_provenance
        opportunity.application_window_status = "OPEN"
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.DISCOVERED.value,
            priority=70,
            risk_level=3,
            eligibility_json=json.dumps({"eligible": True, "reason_codes": []}),
            conflict_json=json.dumps({"blocked": False, "reason_codes": []}),
        )
        session.add(application)
        session.flush()
        return application


def _audit_events(client: TestClient, event_type: str) -> list[AuditEvent]:
    with client.app.state.db.session_scope() as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == event_type)
                .order_by(AuditEvent.id.desc())
            ).all()
        )


def test_operator_supplied_url_stored_with_provenance(tmp_path: Path) -> None:
    """(a) An operator-supplied URL is stored with OPERATOR_SUPPLIED provenance."""
    with _client(tmp_path) as client:
        application = _seed_with_application(client)

        response = client.post(
            f"/api/applications/{application.id}/operator-supplied-url",
            json={"application_url": LAB_APPLICATION_URL},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["application_url"] == LAB_APPLICATION_URL
        assert payload["application_url_provenance"] == "OPERATOR_SUPPLIED"
        # An operator-supplied URL is stored, NOT trusted: it must still pass
        # the resolver's verification before anything is filled into it.
        assert payload["target_status"] == TargetKind.UNRESOLVED.value
        assert payload["verified"] is False
        assert payload["pending_verification"] is True

        # Verify the DB column is set
        with client.app.state.db.session_scope() as session:
            opportunity = session.get(Opportunity, application.opportunity_id)
            assert opportunity is not None
            assert opportunity.application_url == LAB_APPLICATION_URL
            assert opportunity.application_url_provenance == "OPERATOR_SUPPLIED"


def test_operator_supplied_url_audit_recorded(tmp_path: Path) -> None:
    """Verify the audit chain records the operator-supplied correction."""
    with _client(tmp_path) as client:
        application = _seed_with_application(client)

        client.post(
            f"/api/applications/{application.id}/operator-supplied-url",
            json={"application_url": LAB_APPLICATION_URL},
        )

        events = _audit_events(client, "opportunity.operator_supplied_url")
        assert len(events) == 1
        details = json.loads(events[0].details_json or "{}")
        assert details.get("input_source") == "operator_supplied_application_url"
        assert details.get("typed_url") == LAB_APPLICATION_URL
        assert details.get("promoted") is False
        assert details.get("submitted") is False
        verification = details.get("verification_outcome", {})
        # The audit chain must not claim a verification that never ran.
        assert verification.get("status") == "stored_pending_verification"
        assert verification.get("target_kind") == TargetKind.UNRESOLVED.value


def test_invalid_url_rejected_and_does_not_advance(tmp_path: Path) -> None:
    """(b+c) A URL that fails verification is rejected and does NOT advance
    the row out of its blocked state."""
    with _client(tmp_path) as client:
        application = _seed_with_application(client)

        response = client.post(
            f"/api/applications/{application.id}/operator-supplied-url",
            json={"application_url": LAB_INVALID_URL},
        )

        assert response.status_code == 422
        payload = response.json()
        assert payload.get("code") == "operator_supplied_url_invalid"
        assert payload.get("application_url_supplied") is False

        # Verify target_status was NOT advanced
        with client.app.state.db.session_scope() as session:
            opportunity = session.get(Opportunity, application.opportunity_id)
            assert opportunity is not None
            # The application_url should NOT have been updated
            assert opportunity.application_url is None
            # The provenance should not have been set
            assert opportunity.application_url_provenance == ""
            # The target_status should remain unchanged
            assert opportunity.target_status == TargetKind.MISSING_EMPLOYER_LINK.value


def test_insecure_http_url_rejected(tmp_path: Path) -> None:
    """Non-HTTPS URLs are rejected by validation (lab mode off)."""
    with _client(tmp_path) as client:
        application = _seed_with_application(client)

        response = client.post(
            f"/api/applications/{application.id}/operator-supplied-url",
            json={"application_url": LAB_INSECURE_URL},
        )

        assert response.status_code == 422
        payload = response.json()
        assert payload.get("code") == "operator_supplied_url_invalid"


def test_all_target_states_accept_operator_url(tmp_path: Path) -> None:
    """Unlike the blocker-resolution path, the operator-supplied endpoint
    works regardless of target status (no BLOCKED requirement)."""
    for target_state in [
        TargetKind.UNRESOLVED,
        TargetKind.MISSING_EMPLOYER_LINK,
        TargetKind.APPLICATION_FORM,
        TargetKind.BLOCKED,
    ]:
        with _client(tmp_path) as client:
            application = _seed_with_application(
                client,
                employer=f"S4 {target_state.value}",
                target_status=target_state,
            )

            response = client.post(
                f"/api/applications/{application.id}/operator-supplied-url",
                json={"application_url": LAB_APPLICATION_URL},
            )

            assert response.status_code == 200, (
                f"Failed for target_state={target_state}: "
                f"{response.json()}"
            )
            payload = response.json()
            assert payload["application_url_provenance"] == "OPERATOR_SUPPLIED"
            assert payload["verified"] is False


def test_operator_provenance_distinct_from_resolver(tmp_path: Path) -> None:
    """OPERATOR_SUPPLIED provenance is distinct — verify it is not RESOLVER_SUPPLIED."""
    with _client(tmp_path) as client:
        application = _seed_with_application(client)

        response = client.post(
            f"/api/applications/{application.id}/operator-supplied-url",
            json={"application_url": LAB_APPLICATION_URL},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["application_url_provenance"] == "OPERATOR_SUPPLIED"
        assert payload["application_url_provenance"] != "RESOLVER_SUPPLIED"


def test_missing_application_returns_404(tmp_path: Path) -> None:
    """Non-existent application ID returns 404."""
    with _client(tmp_path) as client:
        response = client.post(
            "/api/applications/nonexistent-id/operator-supplied-url",
            json={"application_url": LAB_APPLICATION_URL},
        )
        assert response.status_code == 404