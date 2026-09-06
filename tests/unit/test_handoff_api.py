"""HTTP contract tests for the queue-only application navigator."""
from __future__ import annotations

import json
from pathlib import Path
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.automation.types import SessionState
from app.config import Settings
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, Opportunity


def _client(tmp_path: Path) -> TestClient:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
        }
    )
    app = create_app(settings)
    # Starts resolve only from the persisted verified application target.
    app.state.navigator.url_resolver = None
    # Keep these HTTP-contract tests deterministic without opening a real
    # external browser.  The target is still the persisted, verified loopback
    # target above; this runtime only supplies a no-op local page lifecycle.
    class _Page:
        url = "about:blank"
        frames = ()

        def on(self, *_args):
            return None

        def goto(self, url, **_kwargs):
            self.url = url

        def evaluate(self, *_args):
            return {"captcha": False, "reason": ""}

        def wait_for_timeout(self, _milliseconds):
            return None

        def close(self):
            return None

    class _Context:
        def __init__(self):
            self.page = _Page()

        def on(self, *_args):
            return None

        def route(self, *_args):
            return None

        def new_page(self):
            return self.page

        def close(self):
            return None

    class _Browser:
        def __init__(self):
            self.context = _Context()

        def new_context(self, **_kwargs):
            return self.context

        def close(self):
            return None

    class _Runtime:
        def __init__(self):
            self.chromium = self

        def launch(self, **_kwargs):
            return _Browser()

        def stop(self):
            return None

    class _Factory:
        def start(self):
            return _Runtime()

    app.state.navigator.playwright_factory = lambda: _Factory()
    app.state.handoff_manager = app.state.navigator
    return TestClient(app)


def _seed_application(
    client: TestClient,
    application_id: str,
    *,
    state: ApplicationState = ApplicationState.NEEDS_USER,
    target_status: str = TargetKind.APPLICATION_ENTRY.value,
) -> None:
    with client.app.state.db.session_scope() as session:
        opportunity = Opportunity(
            id=f"opp-{application_id}",
            employer="Loopback Employer",
            role_title="Loopback Role",
            cycle="2027",
            url="http://127.0.0.1:9/source",
            source_fingerprint=f"fingerprint-{application_id}",
            application_url="http://127.0.0.1:9/application",
            target_status=target_status,
            resolved_ats_type="greenhouse" if target_status == TargetKind.APPLICATION_ENTRY.value else "",
            resolved_at=datetime.now(timezone.utc),
            resolution_evidence_json=json.dumps(
                {
                    "source_url": "http://127.0.0.1:9/source",
                    "final_url": "http://127.0.0.1:9/application",
                    "kind": target_status,
                    "provider": "greenhouse" if target_status == TargetKind.APPLICATION_ENTRY.value else "",
                    "identity_verified": True,
                    "form_verified": False,
                    "reason_codes": ["test_verified_target"],
                    "evidence": {
                        "synthetic_lab": True,
                        "provider": "greenhouse",
                        "application_origin": "http://127.0.0.1:9",
                        "employer": "Loopback Employer",
                        "role": "Loopback Role",
                        "requisition": "/application",
                        "form_identity": "application",
                    },
                }
            ),
        )
        application = Application(
            id=application_id,
            opportunity=opportunity,
            state=state.value,
        )
        session.add(application)


def _wait(client: TestClient, session_id: str, expected: str, timeout: float = 2) -> dict:
    deadline = time.monotonic() + timeout
    latest = {}
    while time.monotonic() < deadline:
        latest = client.get(f"/api/handoff/sessions/{session_id}").json()
        if latest.get("status") == expected:
            return latest
        time.sleep(0.01)
    return latest


def test_start_is_202_and_returns_session_id_without_generic_complete(tmp_path):
    with _client(tmp_path) as client:
        _seed_application(client, "app-1")
        response = client.post("/api/handoff/applications/app-1/start", params={"mode": "review"})
        assert response.status_code == 202
        payload = response.json()
        assert payload["session_id"]
        assert payload["application_id"] == "app-1"
        assert payload["status"] in {"OPENING", "ACTIVE"}
        assert payload["state"] == payload["status"]
        assert "session_id" in client.get(
            f"/api/handoff/sessions/{payload['session_id']}"
        ).json()


def test_duplicate_refusal_and_truthful_close_commands(tmp_path):
    with _client(tmp_path) as client:
        _seed_application(client, "app-2")
        first = client.post("/api/handoff/applications/app-2/start").json()
        duplicate = client.post("/api/handoff/applications/app-2/start")
        assert duplicate.status_code == 409

        cancelled = client.post(
            f"/api/handoff/sessions/{first['session_id']}/cancel",
            json={"application_id": "app-2"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"

        repeated = client.post(
            f"/api/handoff/sessions/{first['session_id']}/close",
            json={"application_id": "app-2"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["status"] == "CANCELLED"
        assert repeated.json().get("outcome", "") != "submitted"


def test_continue_endpoint_is_narrow_and_complete_is_not_submission(tmp_path):
    with _client(tmp_path) as client:
        _seed_application(client, "app-3")
        session = client.post("/api/handoff/applications/app-3/start").json()
        continued = client.post(
            f"/api/handoff/sessions/{session['session_id']}/continue",
            json={"application_id": "app-3"},
        )
        assert continued.status_code == 200
        assert continued.json()["status"] in {"OPENING", "ACTIVE", "HUMAN_REQUIRED"}

        deprecated = client.post(
            f"/api/handoff/sessions/{session['session_id']}/complete",
            json={"application_id": "app-3"},
        )
        assert deprecated.status_code == 200
        assert deprecated.json()["status"] == "CANCELLED"


@pytest.mark.parametrize(
    "state",
    [
        ApplicationState.NEEDS_OA,
        ApplicationState.SUBMISSION_UNKNOWN,
        ApplicationState.BLOCKED,
        ApplicationState.SUBMITTED,
        ApplicationState.CONFIRMATION_VERIFIED,
        ApplicationState.OA_PENDING,
        ApplicationState.INTERVIEW,
        ApplicationState.REJECTED,
        ApplicationState.OFFER,
    ],
)
def test_start_state_gate_refuses_no_retry_states_without_creating_worker(tmp_path, state):
    with _client(tmp_path) as client:
        _seed_application(client, "gated-app", state=state)
        for path in (
            "/api/handoff/applications/gated-app/start",
            "/api/handoff/start/gated-app",
            "/api/handoff/sessions",
        ):
            response = client.post(path, json={"application_id": "gated-app", "mode": "review"})
            assert response.status_code == 409, (state, path, response.text)
        assert client.app.state.navigator.all_sessions() == []


def test_start_requires_review_mode_and_known_application_state(tmp_path):
    with _client(tmp_path) as client:
        _seed_application(client, "mode-app")
        submit = client.post(
            "/api/handoff/applications/mode-app/start",
            params={"mode": "submit"},
        )
        assert submit.status_code == 409
        assert client.app.state.navigator.all_sessions() == []

        missing = client.post("/api/handoff/applications/missing-app/start")
        assert missing.status_code == 404
        assert client.app.state.navigator.all_sessions() == []


def test_unresolved_target_is_a_conflict_and_does_not_start_worker(tmp_path):
    with _client(tmp_path) as client:
        _seed_application(client, "unresolved-app", target_status="UNRESOLVED")
        def unresolved(_application_id: str):
            raise ValueError("application target is unresolved")

        client.app.state.navigator.url_resolver = unresolved
        response = client.post("/api/handoff/applications/unresolved-app/start")
        assert response.status_code == 409
        assert client.app.state.navigator.all_sessions() == []


def test_confirmation_requires_exact_json_application_id_before_navigator_confirm(tmp_path):
    with _client(tmp_path) as client:
        _seed_application(client, "confirm-app")
        started = client.post("/api/handoff/applications/confirm-app/start").json()
        session_id = started["session_id"]
        before = client.get(f"/api/handoff/sessions/{session_id}").json()

        missing = client.post(f"/api/handoff/sessions/{session_id}/confirm")
        assert missing.status_code == 422
        assert client.get(f"/api/handoff/sessions/{session_id}").json()["status"] == before["status"]

        mismatch = client.post(
            f"/api/handoff/sessions/{session_id}/confirm",
            json={"application_id": "other-app"},
        )
        assert mismatch.status_code == 409
        after = client.get(f"/api/handoff/sessions/{session_id}").json()
        assert after["application_id"] == "confirm-app"
        assert after["status"] == before["status"]


def test_every_mutating_handoff_command_requires_exact_application_binding(tmp_path):
    with _client(tmp_path) as client:
        _seed_application(client, "bound-app")
        started = client.post("/api/handoff/applications/bound-app/start").json()
        session_id = started["session_id"]

        missing = client.post(f"/api/handoff/sessions/{session_id}/continue")
        assert missing.status_code == 422
        mismatch = client.post(
            f"/api/handoff/sessions/{session_id}/cancel",
            json={"application_id": "other-app"},
        )
        assert mismatch.status_code == 409
        manifest_missing = client.post(
            f"/api/handoff/sessions/{session_id}/final-manifest"
        )
        assert manifest_missing.status_code == 422
        unchanged = client.get(f"/api/handoff/sessions/{session_id}").json()
        assert unchanged["application_id"] == "bound-app"
        assert unchanged["status"] in {"OPENING", "ACTIVE"}


def test_expired_handoff_is_resumable_on_same_service_owned_page(tmp_path):
    with _client(tmp_path) as client:
        navigator = client.app.state.navigator
        navigator.ttl_seconds = 0.05
        _seed_application(client, "resumable-app")
        started = client.post(
            "/api/handoff/applications/resumable-app/start"
        ).json()
        session_id = started["session_id"]
        page_token = navigator.diagnostics(session_id).page_token
        expired = navigator.wait_for_state(
            session_id,
            SessionState.EXPIRED,
            timeout=2,
        )

        assert expired.state is SessionState.EXPIRED
        status = client.get(f"/api/handoff/sessions/{session_id}").json()
        assert status["resumable"] is True
        assert status["worker_alive"] is True
        assert status["cleanup_complete"] is False

        resumed = client.post(
            f"/api/handoff/sessions/{session_id}/resume",
            json={"application_id": "resumable-app"},
        )

        assert resumed.status_code == 200
        assert resumed.json()["status"] == "ACTIVE"
        assert resumed.json()["expires_at"] > status["expires_at"]
        assert navigator.diagnostics(session_id).page_token == page_token
