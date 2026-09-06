# TDD: R6 — batch-wide confirmation must not exist.
# A single boolean (request flag + env flag) must never confirm submissions
# for many applications. Every submission requires exact per-application
# action-time confirmation bound to that application's identity.
from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.models import Application, Opportunity


def _seed_nonempty_autopilot_candidate(live_server) -> str:
    """Seed one loopback candidate so the no-batch assertion is non-vacuous."""

    settings = Settings.load({"ARGUS_DATA_DIR": str(live_server.data_dir)})
    database = Database(settings)
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="ARGUS Loopback Candidate",
            role_title="Summer Analyst",
            programme_group="summer",
            cycle="2027",
            url=f"{live_server.base_url}/source",
            application_url=f"{live_server.base_url}/lab/ats/standard",
            target_status=TargetKind.APPLICATION_ENTRY.value,
            resolved_ats_type="greenhouse",
            resolution_evidence_json=json.dumps(
                {
                    "identity_verified": True,
                    "evidence": {"synthetic_lab": True},
                }
            ),
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.ELIGIBILITY_CHECKED.value,
        )
        session.add(application)
        session.flush()
        return application.id


def test_autopilot_endpoint_has_no_confirm_submissions_parameter(live_server):
    """The OpenAPI schema must not expose a batch confirmation switch."""
    schema = httpx.get(f"{live_server.base_url}/openapi.json", timeout=10).json()
    post = schema["paths"].get("/api/scout/autopilot", {}).get("post", {})
    params = {p.get("name") for p in post.get("parameters", [])}
    assert "confirm_submissions" not in params
    assert "max_runs" in params  # sanity: endpoint itself still exists


def test_env_flag_cannot_enable_batch_submit(live_server, monkeypatch):
    """ARGUS_AUTOPILOT_SUBMIT=true must be inert: no code path reads it."""
    import app.routers.scout as scout_module

    assert hasattr(scout_module, "_batch_submit_env_enabled")
    monkeypatch.setenv("ARGUS_AUTOPILOT_SUBMIT", "true")
    assert scout_module._batch_submit_env_enabled() is False


def test_autopilot_never_runs_a_second_submit_pass(live_server):
    """A non-empty autopilot run still exposes no batch submit phase."""
    application_id = _seed_nonempty_autopilot_candidate(live_server)
    outcome = httpx.post(
        f"{live_server.base_url}/api/scout/autopilot",
        params={"max_runs": 1},
        timeout=30,
    )
    outcome.raise_for_status()
    payload = outcome.json()
    assert "batch_submit" not in payload
    assert payload["processed"] == 1, payload
    assert len(payload["details"]) == 1
    assert payload["details"][0]["application_id"] == application_id
    assert payload["needs_user"] == 1
    assert payload["submitted"] == 0
