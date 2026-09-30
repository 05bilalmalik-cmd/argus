"""Legal-bearing twins must stop even when stored profile answers exist."""
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.automation.types import SessionState
from tests.e2e.test_lab_adapter_variants import (
    _configure_candidate, _prepare, _observe_journey_prepare, _lab_settings,
)


@pytest.mark.parametrize("provider", ["greenhouse", "lever", "workday"])
def test_static_legal_twin_does_not_answer_or_submit(live_server, monkeypatch, provider):
    _configure_candidate(live_server.base_url)
    application_id = _prepare(live_server, provider, provider)
    observations = _observe_journey_prepare(monkeypatch)
    with TestClient(create_app(_lab_settings(live_server))) as client:
        response = client.post(f"/api/applications/{application_id}/run",
                               params={"mode": "prefill", "headed": "true"})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["state"] == "NEEDS_USER", payload
        session_id = payload["handoff_session_id"]
        assert session_id
        navigator = client.app.state.navigator
        assert navigator.get(session_id).state is SessionState.HUMAN_REQUIRED
        result = navigator.journey_result(session_id)
        assert result["risk_level"] == 3
        assert "approved_legal_answer_missing" in result["blocked_reasons"]
        assert len(observations) == 1
        dom = observations[0]["dom"]
        assert "sponsor" not in dom["fields"]
        assert dom["clicks"] == {"next": 0, "submit": 0}
        assert observations[0]["mutations"] == []
        assert httpx.get(f"{live_server.base_url}/api/lab/submissions", timeout=10).json() == []
