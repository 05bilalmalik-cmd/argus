from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from app.automation.runner import SubmissionBlocked, assert_submission_allowed
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState, InvalidTransition, validate_transition
from app.security.audit import AuditInput, append_audit, verify_audit_chain
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
                "Synthetic Adversarial CV.docx",
                cv_docx_bytes(2028, "Adversarial flows"),
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
            "source": "adversarial_e2e",
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
    httpx.post(f"{base_url}/api/applications/{application_id}/queue", timeout=10).raise_for_status()
    package = httpx.post(
        f"{base_url}/api/applications/{application_id}/prepare", timeout=10
    )
    package.raise_for_status()
    assert package.json()["ready"] is True
    return application_id


def run_submit(base_url: str, application_id: str) -> httpx.Response:
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
        # Confirmation re-runs the authoritative safety projection. Unsafe
        # fixtures return their blocker outcome without minting a capability.
        return confirmed
    authority_id = authority["authority_id"]
    return httpx.post(
        f"{base_url}/api/applications/{application_id}/run",
        params={"mode": "submit"},
        json={
            "application_id": application_id,
            "session_id": session_id,
            "authority_id": authority_id,
        },
        timeout=30,
    )


def test_malicious_document_filename_cannot_escape_document_vault(live_server) -> None:
    secret_before = (live_server.data_dir / "secret.key").read_bytes()

    response = httpx.post(
        f"{live_server.base_url}/api/documents",
        data={"kind": "cv", "approved": "false", "tags": "hostile"},
        files={"file": ("../../secret.key", b"attacker-controlled", "application/octet-stream")},
        timeout=10,
    )

    response.raise_for_status()
    assert response.json()["name"] == "secret.key"
    assert (live_server.data_dir / "secret.key").read_bytes() == secret_before
    stored = list((live_server.data_dir / "documents").glob("*"))
    assert len(stored) == 1
    assert stored[0].resolve().is_relative_to((live_server.data_dir / "documents").resolve())
    assert stored[0].read_bytes() == b"attacker-controlled"


def test_duplicate_submit_is_refused_without_second_post(live_server) -> None:
    configure_candidate(live_server.base_url)
    application_id = prepare(live_server, "standard")

    first = run_submit(live_server.base_url, application_id)
    second = run_submit(live_server.base_url, application_id)

    first.raise_for_status()
    assert first.json()["state"] == "CONFIRMATION_VERIFIED"
    assert second.status_code == 409
    assert "already been submitted" in second.json()["detail"]
    submissions = httpx.get(f"{live_server.base_url}/api/lab/submissions", timeout=10).json()
    assert len(submissions) == 1


def test_forged_same_page_confirmation_is_not_accepted(live_server) -> None:
    configure_candidate(live_server.base_url)
    application_id = prepare(live_server, "forged")

    response = run_submit(live_server.base_url, application_id)

    response.raise_for_status()
    outcome = response.json()
    # Contract updated 2026-08-24: a click whose effect cannot be verified
    # (forged wording, no receipt) now lands SUBMISSION_UNKNOWN — the honest
    # answer is "unknown", not a clean retryable NEEDS_USER.  Either way the
    # forged confirmation must NOT be accepted as success.
    assert outcome["state"] in {"NEEDS_USER", "SUBMISSION_UNKNOWN"}
    assert outcome["receipt"] is None
    assert httpx.get(f"{live_server.base_url}/api/lab/submissions", timeout=10).json() == []


@pytest.mark.parametrize(
    ("scenario", "expected_state", "reason"),
    [
        ("unknown", "NEEDS_USER", "unknown_required_field"),
        ("mismatch", "BLOCKED", "destination_employer_mismatch"),
        ("exfiltration", "NEEDS_USER", "submission_target_guard"),
        ("script-exfiltration", "NEEDS_USER", "submission_network_guard"),
    ],
)
def test_unknown_and_mismatched_forms_fail_closed(
    live_server, scenario: str, expected_state: str, reason: str
) -> None:
    configure_candidate(live_server.base_url)
    application_id = prepare(live_server, scenario)

    response = run_submit(live_server.base_url, application_id)

    response.raise_for_status()
    payload = response.json()
    assert payload["state"] == expected_state
    assert reason in payload["blocked_reasons"]
    assert "authority" not in payload
    assert httpx.get(f"{live_server.base_url}/api/lab/submissions", timeout=10).json() == []


def test_stale_opportunity_cannot_be_queued_or_run(live_server) -> None:
    configure_candidate(live_server.base_url)
    expired = (date.today() - timedelta(days=1)).isoformat()
    opportunity = httpx.post(
        f"{live_server.base_url}/api/opportunities",
        json={
            "employer": "Expired Capital",
            "role_title": "Closed Internship",
            "cycle": "2027",
            "url": f"{live_server.base_url}/lab/ats/standard?expired=1",
            "deadline": expired,
            "sponsorship_supported": True,
        },
        timeout=10,
    )
    opportunity.raise_for_status()

    evaluated = httpx.post(
        f"{live_server.base_url}/api/opportunities/{opportunity.json()['id']}/evaluate",
        timeout=10,
    )
    evaluated.raise_for_status()
    payload = evaluated.json()
    assert payload["eligible"] is False
    assert payload["state"] == "BLOCKED"
    queued = httpx.post(
        f"{live_server.base_url}/api/applications/{payload['application_id']}/queue",
        timeout=10,
    )
    assert queued.status_code == 409


def test_invalid_transition_and_prohibited_domain_are_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidTransition):
        validate_transition(ApplicationState.CONFIRMATION_VERIFIED, ApplicationState.FILLING)

    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_LIVE_SUBMIT": "true",
            "ARGUS_LIVE_DOMAIN_ALLOWLIST": "jobs.allowed.example",
        }
    )
    with pytest.raises(SubmissionBlocked, match="not allowlisted"):
        assert_submission_allowed(settings, "https://jobs.attacker.example/apply", 0)


def test_tampered_audit_event_is_detected(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        event = append_audit(
            session,
            AuditInput("adversary", "fixture.created", "fixture", "1", {"value": "safe"}),
        )
        event.details_json = '{"value":"mutated"}'
    with database.session_scope() as session:
        result = verify_audit_chain(session)

    assert result.valid is False
    assert result.broken_event_id is not None
