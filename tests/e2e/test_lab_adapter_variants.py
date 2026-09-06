from __future__ import annotations

import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app.automation.runner import _OwnerThreadJourney
from app.automation.types import SessionState
from app.config import Settings
from app.main import create_app

from tests.e2e.conftest import LiveServer, persist_verified_lab_target
from tests.document_helpers import cv_docx_bytes


def _configure_candidate(base_url: str) -> None:
    profile = httpx.put(
        f"{base_url}/api/profile",
        json={
            "first_name": "Demo",
            "last_name": "Candidate",
            "email": "demo@example.test",
            "university": "Example University",
            "degree": "BSc Finance",
            "graduation_year": 2028,
            "work_authorisation": "Approved local laboratory wording",
            "requires_sponsorship": False,
            "work_authorisation_approved": True,
        },
        timeout=10,
    )
    profile.raise_for_status()
    document = httpx.post(
        f"{base_url}/api/documents",
        data={"kind": "cv", "approved": "true", "tags": "london,summer-cv"},
        files={
            "file": (
                "Synthetic Adapter CV.docx",
                cv_docx_bytes(2028, "Adapter variants"),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        timeout=10,
    )
    document.raise_for_status()


def _prepare(server: LiveServer, scenario: str, adapter: str) -> str:
    base_url = server.base_url
    target_url = f"{base_url}/lab/ats/{scenario}"
    opportunity = httpx.post(
        f"{base_url}/api/opportunities",
        json={
            "employer": "ARGUS Test Capital",
            "role_title": "Summer Analyst",
            "division": "Investment Banking",
            "programme_group": "summer",
            "location": "London",
            "cycle": "2027",
            "url": target_url,
            "source": "lab",
            "ats_type": adapter,
            "sponsorship_supported": True,
            "cv_required": True,
        },
        timeout=10,
    )
    opportunity.raise_for_status()
    persist_verified_lab_target(server, opportunity.json()["id"], target_url, adapter)
    evaluated = httpx.post(
        f"{base_url}/api/opportunities/{opportunity.json()['id']}/evaluate", timeout=10
    )
    evaluated.raise_for_status()
    application_id = evaluated.json()["application_id"]
    httpx.post(
        f"{base_url}/api/applications/{application_id}/queue", timeout=10
    ).raise_for_status()
    prepared = httpx.post(
        f"{base_url}/api/applications/{application_id}/prepare", timeout=10
    )
    prepared.raise_for_status()
    assert prepared.json()["ready"] is True
    return application_id


def _run_with_authority(base_url: str, application_id: str) -> httpx.Response:
    review = httpx.post(
        f"{base_url}/api/applications/{application_id}/run",
        params={"mode": "prefill", "headed": "true"},
        timeout=30,
    )
    if review.status_code != 200:
        return review
    review_payload = review.json()
    if review_payload.get("state") in {
        "BLOCKED",
        "NEEDS_OA",
        "NEEDS_USER",
        "SUBMISSION_UNKNOWN",
    }:
        return review
    session_id = review_payload.get("handoff_session_id")
    if not session_id:
        raise AssertionError(
            f"risk-zero final review did not publish a handoff session: {review_payload}"
        )
    manifest = httpx.post(
        f"{base_url}/api/handoff/sessions/{session_id}/final-manifest",
        json={"application_id": application_id},
        timeout=30,
    )
    if manifest.status_code != 200:
        return manifest
    confirmed = httpx.post(
        f"{base_url}/api/handoff/sessions/{session_id}/confirm",
        json={"application_id": application_id},
        timeout=30,
    )
    if confirmed.status_code != 200:
        return confirmed
    authority = confirmed.json().get("authority")
    if not authority:
        return confirmed
    return httpx.post(
        f"{base_url}/api/applications/{application_id}/run",
        params={"mode": "submit"},
        json={
            "application_id": application_id,
            "session_id": session_id,
            "authority_id": authority["authority_id"],
        },
        timeout=30,
    )


def _lab_settings(server: LiveServer) -> Settings:
    return Settings.load(
        {
            "ARGUS_DATA_DIR": str(server.data_dir),
            "ARGUS_API_TOKEN": "e2e-token",
            "ARGUS_PORT": server.base_url.rsplit(":", 1)[1],
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_BROWSER_HEADLESS": "true",
        }
    )


def _observe_journey_prepare(monkeypatch):
    """Observe real owner-thread execution; never replace a guard or result."""
    observations = []
    prepare = _OwnerThreadJourney.prepare

    def observed_prepare(journey, page):
        mutations = []
        page.on(
            "request",
            lambda request: mutations.append((request.method, request.url))
            if request.method not in {"GET", "HEAD", "OPTIONS"} else None,
        )
        page.evaluate("""() => {
            window.__testJourneyClicks = {next: 0, submit: 0};
            document.addEventListener('click', event => {
                const control = event.target.closest('button, input');
                if (control?.matches('[data-automation-id="submitNextButton"]'))
                    window.__testJourneyClicks.next += 1;
                if (control?.matches('[type="submit"]'))
                    window.__testJourneyClicks.submit += 1;
            }, true);
        }""")
        result = prepare(journey, page)
        dom = page.evaluate("""() => ({
            clicks: window.__testJourneyClicks,
            workday: window.__argusWorkdayState || null,
            provider: window.__argusProviderJourney || null,
            fields: Object.fromEntries([...document.querySelectorAll('input[name], select[name]')]
                .filter(el => el.type !== 'radio' || el.checked)
                .map(el => [el.name, el.type === 'file'
                    ? [...el.files].map(file => ({name: file.name, size: file.size})) : el.value])),
            captcha: !!document.querySelector('[data-captcha="true"]'),
            final_submit: !!document.querySelector('[data-automation-id="submitButton"]')
        })""")
        observations.append({
            "dom": dom, "mutations": mutations, "result": result,
            "owner_thread_id": threading.get_ident(),
        })
        return result

    monkeypatch.setattr(_OwnerThreadJourney, "prepare", observed_prepare)
    return observations


def _assert_step_one_prefill(server, monkeypatch, scenario: str, adapter: str):
    _configure_candidate(server.base_url)
    application_id = _prepare(server, scenario, adapter)
    observations = _observe_journey_prepare(monkeypatch)
    with TestClient(create_app(_lab_settings(server))) as client:
        response = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "prefill", "headed": "true"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["state"] == "NEEDS_USER", payload
        assert payload["adapter"] == adapter
        session_id = payload["handoff_session_id"]
        assert session_id, payload
        navigator = client.app.state.navigator
        snapshot = navigator.get(session_id)
        assert snapshot.application_id == application_id
        assert snapshot.state is SessionState.HUMAN_REQUIRED
        assert snapshot.worker_alive is True
        result = navigator.journey_result(session_id)
        assert result["state"] == "NEEDS_USER"
        assert result["risk_level"] == 0
        assert not result["blocked_reasons"]
        assert result["reason"] == (
            "Prefill complete without advancing — human review is required for the remaining steps"
        )
        # step_index counts completed Next transitions, not fill/reinspection passes.
        assert result["step_index"] == 0
        assert len(observations) == 1
        observed = observations[0]
        assert observed["owner_thread_id"] == navigator.diagnostics(session_id).owner_thread_id
        assert observed["mutations"] == []
        dom = observed["dom"]
        assert dom["clicks"] == {"next": 0, "submit": 0}
        assert dom["final_submit"] is False
        assert dom["captcha"] is False
        assert dom["fields"]["first_name"] == "Demo"
        assert dom["fields"]["last_name"] == "Candidate"
        assert httpx.get(f"{server.base_url}/api/lab/submissions", timeout=10).json() == []
        return dom


@pytest.mark.parametrize(
    ("scenario", "adapter"),
    [("smartrecruiters-journey", "smartrecruiters"), ("workable-journey", "workable")],
)
def test_multistep_prefill_hands_off_on_first_step_without_next_or_post(
    live_server, monkeypatch, scenario: str, adapter: str
) -> None:
    dom = _assert_step_one_prefill(live_server, monkeypatch, scenario, adapter)
    assert dom["provider"] == {"step": 0, "nextClicks": 0, "distractionClicks": 0}
    assert dom["fields"]["email"] == "demo@example.test"
    assert dom["fields"]["graduation_year"] == "2028"
    assert dom["fields"]["sponsor"] == "no"
    assert len(dom["fields"]["cv"]) == 1
    assert dom["fields"]["cv"][0]["size"] > 0


@pytest.mark.parametrize(
    ("scenario", "adapter"),
    [("greenhouse", "greenhouse"), ("lever", "lever"), ("workday", "workday")],
)
def test_ats_variants_detect_fill_and_submit_once(
    live_server, scenario: str, adapter: str
) -> None:
    _configure_candidate(live_server.base_url)
    application_id = _prepare(live_server, scenario, adapter)

    outcome = _run_with_authority(live_server.base_url, application_id)
    outcome.raise_for_status()
    payload = outcome.json()
    assert payload["state"] == "CONFIRMATION_VERIFIED"
    assert payload["adapter"] == adapter
    assert payload["receipt"]["reference"].startswith("ARG-")

    submissions = httpx.get(f"{live_server.base_url}/api/lab/submissions", timeout=10).json()
    assert len(submissions) == 1
    assert submissions[0]["scenario"] == scenario
    assert submissions[0]["payload"]["first_name"] == "Demo"
    assert submissions[0]["payload"]["last_name"] == "Candidate"
    assert submissions[0]["payload"]["email"] == "demo@example.test"
    assert submissions[0]["payload"]["cv"]


@pytest.mark.parametrize(
    ("scenario", "adapter", "expected_error"),
    [
        ("duplicate-controls", "greenhouse", ""),
        ("ambiguous-submit", "greenhouse", "Ambiguous"),
        ("absent-submit", "greenhouse", "No visible submission"),
    ],
)
def test_submit_controls_fail_closed_without_duplicate_submission(
    live_server, scenario: str, adapter: str, expected_error: str
) -> None:
    _configure_candidate(live_server.base_url)
    application_id = _prepare(live_server, scenario, adapter)

    outcome = _run_with_authority(live_server.base_url, application_id)
    outcome.raise_for_status()
    payload = outcome.json()
    submissions = httpx.get(f"{live_server.base_url}/api/lab/submissions", timeout=10).json()

    if scenario == "duplicate-controls":
        assert payload["state"] == "CONFIRMATION_VERIFIED"
        assert len(submissions) == 1
    else:
        assert payload["state"] == "NEEDS_USER"
        assert submissions == []
        run = httpx.get(f"{live_server.base_url}/api/runs", timeout=10).json()[0]
        assert expected_error in run["error"]
