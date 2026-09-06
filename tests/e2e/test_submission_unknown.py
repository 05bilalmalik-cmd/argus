"""Acceptance: R7 — the durable-intent contract under a post-click failure.

Strategy: exercise the runner in-process (so we CAN inject a persistence
failure) against the subprocess live server's lab origin.  The lab records
the submission; the injected failure then prevents ARGUS from persisting
that evidence.  The application must land SUBMISSION_UNKNOWN and a second
submit must never produce a second employer-side POST.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.states import ApplicationState
from app.main import create_app
from app.models import Application

from tests.e2e.test_lab_adapter_variants import _configure_candidate, _prepare


def test_post_click_failure_marks_unknown_and_blocks_retry(
    tmp_path, monkeypatch, live_server
):
    # ---- 1. drive the real flow against the live server's lab ----------
    base_url = live_server.base_url
    _configure_candidate(base_url)
    application_id = _prepare(live_server, "greenhouse", "greenhouse")

    # ---- 2. run the submit pass IN-PROCESS with an injected commit failure
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(live_server.data_dir),
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
            "ARGUS_API_TOKEN": "e2e-token",
            "ARGUS_PORT": str(live_server.base_url.rsplit(":", 1)[1]),
        }
    )
    app = create_app(settings)

    from app.automation import runner as runner_module

    original_transition = runner_module.AutomationRunner._transition

    def failing_transition(*args, **kwargs):
        call_args = tuple(args[1:]) if len(args) == 4 else args
        if len(call_args) == 3 and call_args[2].value == "SUBMITTED":
            raise RuntimeError("simulated disk failure at submission commit")
        return original_transition(*call_args, **kwargs)

    monkeypatch.setattr(
        runner_module.AutomationRunner, "_transition", failing_transition
    )

    with TestClient(app) as client:
        review = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "prefill", "headed": "true"},
        )
        assert review.status_code == 200, review.text
        session_id = review.json()["handoff_session_id"]
        assert session_id, review.json()
        manifest_response = client.post(
            f"/api/handoff/sessions/{session_id}/final-manifest",
            json={"application_id": application_id},
        )
        assert manifest_response.status_code == 200, manifest_response.text
        confirmed = client.post(
            f"/api/handoff/sessions/{session_id}/confirm",
            json={"application_id": application_id},
        )
        assert confirmed.status_code == 200, confirmed.text
        authority = confirmed.json()["authority"]["authority_id"]
        outcome = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "submit"},
            json={"application_id": application_id, "session_id": session_id, "authority_id": authority},
        )
        payload = outcome.json()
        assert outcome.status_code == 200, outcome.text

        submissions = httpx.get(f"{base_url}/api/lab/submissions", timeout=10).json()
    assert len(submissions) == 1  # exactly one click ever went out

    # The run must NOT claim clean verified success.
    runs = httpx.get(f"{base_url}/api/runs", timeout=10).json()
    final_run = runs[0]
    assert not (
        payload.get("state") == "CONFIRMATION_VERIFIED"
        and payload.get("receipt")
        and final_run["error"] == ""
    ), "run claimed clean success although evidence persistence failed"

    # The intent must exist and reflect uncertainty (never silently CONFIRMED).
    from sqlalchemy import select

    from app.models import SubmissionAuthority, SubmissionIntent

    engine_settings = Settings.load(
        {"ARGUS_DATA_DIR": str(live_server.data_dir), "ARGUS_API_TOKEN": "e2e-token"}
    )
    from app.db import Database

    probe = Database(engine_settings)
    with probe.session_scope() as session:
        intent = session.scalar(
            select(SubmissionIntent).where(
                SubmissionIntent.application_id == application_id
            )
        )
        assert intent is not None, "no durable intent persisted pre-click"
        assert intent.status in {
            "UNKNOWN",
            "CLICKED",
        }, f"intent status {intent.status} hides the uncertainty"

        state_row = session.get(Application, application_id)
        assert state_row is not None
        assert state_row.state == ApplicationState.SUBMISSION_UNKNOWN.value
        authority_row = session.get(SubmissionAuthority, authority)
        assert authority_row is not None
        assert authority_row.consumed_at is not None

    # ---- 3. no duplicate submission: a retry must be refused -------------
    second = httpx.post(
        f"{base_url}/api/applications/{application_id}/run",
        params={"mode": "submit"},
        json={
            "application_id": application_id,
            "session_id": session_id,
            "authority_id": authority,
        },
        timeout=60,
    )
    submissions_after = httpx.get(
        f"{base_url}/api/lab/submissions", timeout=10
    ).json()
    assert len(submissions_after) == 1, (
        f"duplicate employer-side submission! ({len(submissions_after)})"
    )

    # A second action must remain refused at the boundary; its response and
    # durable state cannot silently turn an UNKNOWN outcome green.
    assert second.status_code in {409, 422}
    with probe.session_scope() as session:
        final_intent = session.scalar(
            select(SubmissionIntent).where(
                SubmissionIntent.application_id == application_id
            )
        )
        final_state = session.get(Application, application_id)
        assert final_intent is not None
        assert final_intent.status in {"UNKNOWN", "CLICKED"}
        assert final_state is not None
        assert final_state.state == ApplicationState.SUBMISSION_UNKNOWN.value
