from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import LiveServer, persist_verified_lab_target
from tests.document_helpers import cv_docx_bytes


def configure_candidate(base_url: str) -> None:
    response = httpx.put(
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
    response.raise_for_status()
    document = httpx.post(
        f"{base_url}/api/documents",
        data={"kind": "cv", "approved": "true", "tags": "london,summer-cv"},
        files={
            "file": (
                "Synthetic E2E CV.docx",
                cv_docx_bytes(2028, "Application flows"),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        timeout=10,
    )
    document.raise_for_status()


def prepare(server: LiveServer, scenario: str) -> str:
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
            "source": "e2e",
            "ats_type": "greenhouse",
            "sponsorship_supported": True,
            "cv_required": True,
        },
        timeout=10,
    )
    opportunity.raise_for_status()
    persist_verified_lab_target(
        server, opportunity.json()["id"], target_url, "greenhouse"
    )
    evaluated = httpx.post(
        f"{base_url}/api/opportunities/{opportunity.json()['id']}/evaluate", timeout=10
    )
    evaluated.raise_for_status()
    application_id = evaluated.json()["application_id"]
    queued = httpx.post(f"{base_url}/api/applications/{application_id}/queue", timeout=10)
    queued.raise_for_status()
    prepared = httpx.post(f"{base_url}/api/applications/{application_id}/prepare", timeout=10)
    prepared.raise_for_status()
    assert prepared.json()["ready"] is True
    return application_id


def run(base_url: str, application_id: str) -> dict:
    review = httpx.post(
        f"{base_url}/api/applications/{application_id}/run",
        params={"mode": "prefill", "headed": "true"},
        timeout=30,
    )
    review.raise_for_status()
    review_payload = review.json()
    if review_payload.get("state") in {
        "BLOCKED",
        "NEEDS_OA",
        "NEEDS_USER",
        "SUBMISSION_UNKNOWN",
    }:
        return review_payload
    session_id = review_payload.get("handoff_session_id")
    assert session_id, review_payload
    manifest = httpx.post(
        f"{base_url}/api/handoff/sessions/{session_id}/final-manifest",
        json={"application_id": application_id},
        timeout=30,
    )
    manifest.raise_for_status()
    confirmed = httpx.post(
        f"{base_url}/api/handoff/sessions/{session_id}/confirm",
        json={"application_id": application_id},
        timeout=30,
    )
    confirmed.raise_for_status()
    authority = confirmed.json().get("authority")
    assert authority, confirmed.json()
    submitted = httpx.post(
        f"{base_url}/api/applications/{application_id}/run",
        params={"mode": "submit"},
        json={
            "application_id": application_id,
            "session_id": session_id,
            "authority_id": authority["authority_id"],
        },
        timeout=30,
    )
    submitted.raise_for_status()
    return submitted.json()


def test_standard_mock_ats_submits_and_captures_receipt(live_server) -> None:
    configure_candidate(live_server.base_url)
    application_id = prepare(live_server, "standard")

    outcome = run(live_server.base_url, application_id)

    assert outcome["state"] == "CONFIRMATION_VERIFIED"
    assert outcome["risk_level"] == 0
    assert outcome["adapter"] == "greenhouse"
    assert outcome["receipt"]["reference"].startswith("ARG-")
    assert outcome["trace_path"].endswith(".zip")
    assert outcome["screenshot_path"].endswith(".png")
    submissions = httpx.get(f"{live_server.base_url}/api/lab/submissions").json()
    assert len(submissions) == 1
    assert submissions[0]["scenario"] == "standard"


@pytest.mark.parametrize(
    ("scenario", "expected_state", "blocking_code"),
    [
        ("sensitive", "NEEDS_USER", "sensitive_demographic"),
        ("assessment", "NEEDS_OA", "assessment_handoff"),
        ("mismatch", "BLOCKED", "destination_employer_mismatch"),
    ],
)
def test_guarded_scenarios_stop_without_submission(
    live_server, scenario: str, expected_state: str, blocking_code: str
) -> None:
    configure_candidate(live_server.base_url)
    application_id = prepare(live_server, scenario)

    outcome = run(live_server.base_url, application_id)

    assert outcome["state"] == expected_state
    assert blocking_code in outcome["blocked_reasons"]
    assert httpx.get(f"{live_server.base_url}/api/lab/submissions").json() == []
