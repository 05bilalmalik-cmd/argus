from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Application, AutomationRun, Opportunity
from app.security.audit import AuditInput, append_audit


def _client(tmp_path: Path) -> TestClient:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    return TestClient(create_app(settings))


def _seed_application(client: TestClient, *, employer: str, state: str = "NEEDS_USER") -> Application:
    with client.app.state.db.session_scope() as session:
        opportunity = Opportunity(
            employer=employer,
            role_title=f"{employer} Summer Analyst",
            division="Private Credit",
            programme_group="summer",
            location="London",
            cycle="2027",
            url=f"https://example.test/{employer.casefold().replace(' ', '-')}",
            source="trackr_live",
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=state,
            priority=70,
            risk_level=3,
            next_action="Review eligibility evidence",
            eligibility_json=json.dumps({"eligible": False, "reason_codes": ["missing_answer"]}),
            conflict_json=json.dumps({"blocked": False, "reason_codes": []}),
        )
        session.add(application)
        session.flush()
        return application


def test_applications_page_uses_server_side_query_and_truthful_totals(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        for employer in ("Alpha One", "Alpha Two", "Alpha Three"):
            _seed_application(client, employer=employer)
        _seed_application(client, employer="Beta One", state="QUEUED")

        response = client.get(
            "/applications?q=alpha&state=NEEDS_USER&sort=priority&direction=asc&page=2&per_page=1"
        )

        assert response.status_code == 200
        assert "3 total" in response.text
        assert "Page 2 of 3" in response.text
        assert "q=alpha" in response.text
        assert "state=NEEDS_USER" in response.text


def test_opportunities_page_marks_new_since_explicit_review_and_preserves_query(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        for employer in ("Alpha One", "Alpha Two"):
            _seed_application(client, employer=employer)

        response = client.get("/opportunities?q=alpha&sort=employer&direction=desc&page=1&per_page=1")

        assert response.status_code == 200
        assert "2 total" in response.text
        assert "NEW" in response.text
        assert "q=alpha" in response.text
        assert "Mark current results reviewed" in response.text

        reviewed = client.post(
            "/opportunities/review?q=alpha&sort=employer&direction=desc&page=1&per_page=1",
            follow_redirects=False,
        )
        assert reviewed.status_code == 303
        assert "argus_opportunities_reviewed_at=" in reviewed.headers["set-cookie"]

        after_review = client.get("/opportunities?q=alpha&sort=employer&direction=desc&page=1&per_page=1")
        assert 'class="new-marker">NEW' not in after_review.text
        assert "Last reviewed" in after_review.text


def test_application_detail_exposes_recovery_evidence_artifacts_and_audit(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        application = _seed_application(client, employer="Detail Firm")
        with client.app.state.db.session_scope() as session:
            session.add(
                AutomationRun(
                    application_id=application.id,
                    mode="dry-run",
                    state="BLOCKED",
                    adapter="generic",
                    trace_path="artifacts/traces/run.zip",
                    screenshot_path="artifacts/screenshots/run.png",
                    error="Declaration requires review",
                )
            )
            append_audit(
                session,
                AuditInput(
                    "system",
                    "application.evaluated",
                    "application",
                    application.id,
                    {"eligible": False, "reason_codes": ["missing_answer"]},
                ),
            )

        response = client.get(f"/applications/{application.id}")

        assert response.status_code == 200
        assert "Detail Firm" in response.text
        assert "Blockers" in response.text
        assert "missing_answer" in response.text
        assert "run.zip" in response.text
        assert "Declaration requires review" in response.text
        assert "Audit history" in response.text
        assert f"/api/applications/{application.id}/queue" in response.text
        assert f"/api/applications/{application.id}/run?mode=submit" not in response.text


def test_confirmed_application_keeps_failed_run_in_history_not_current_blockers(
    tmp_path: Path,
) -> None:
    stale_error = "Playwright timeout waiting for old submit selector"
    with _client(tmp_path) as client:
        application = _seed_application(client, employer="Confirmed Firm")
        with client.app.state.db.session_scope() as session:
            persisted = session.get(Application, application.id)
            assert persisted is not None
            persisted.state = "CONFIRMATION_VERIFIED"
            persisted.next_action = "Monitor for assessment updates"
            persisted.eligibility_json = json.dumps({"eligible": True, "reason_codes": []})
            persisted.conflict_json = json.dumps({"blocked": False, "reason_codes": []})
            session.add(
                AutomationRun(
                    application_id=application.id,
                    mode="dry-run",
                    state="FAILED_RETRYABLE",
                    adapter="playwright",
                    error=stale_error,
                )
            )
            append_audit(
                session,
                AuditInput(
                    "system",
                    "application.run_failed",
                    "application",
                    application.id,
                    {"error": stale_error},
                ),
            )

        response = client.get(f"/applications/{application.id}")

        assert response.status_code == 200
        blocker_card = response.text.split('<section class="panel">', 1)[1].split(
            "</section>", 1
        )[0]
        assert "No recorded blockers" in blocker_card
        assert stale_error not in blocker_card
        assert "Monitor for assessment updates" in response.text
        assert "Run history" in response.text
        assert "Historical run evidence" in response.text
        assert stale_error in response.text
        assert "Audit history" in response.text
        assert "application.run_failed" in response.text


def test_needs_you_links_to_detail_and_scout_assets_are_csp_safe(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        application = _seed_application(client, employer="Needs You Firm")

        needs = client.get("/needs-you")
        scout = client.get("/scout")
        scout_js = client.get("/static/js/scout.js")

        assert needs.status_code == 200
        assert f'href="/applications/{application.id}"' in needs.text
        assert scout.status_code == 200
        assert '<script src="/static/js/scout.js" defer></script>' in scout.text
        assert '<script>\n' not in scout.text
        assert 'style="display:none' not in scout.text
        assert "unsafe-inline" not in scout.headers["content-security-policy"]
        assert scout_js.status_code == 200
        assert "textContent" in scout_js.text


def test_blocked_recovery_is_human_bound_and_never_generic_requeue(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        application = _seed_application(client, employer="Blocked Recovery Firm", state="BLOCKED")

        needs = client.get("/needs-you")
        assert needs.status_code == 200
        assert f"/api/applications/{application.id}/resolve-blocker" in needs.text
        assert f"/api/applications/{application.id}/queue" not in needs.text
        assert "No submission will be made" in needs.text

        missing_confirmation = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "resolution_reason": "I reviewed the blocker evidence.",
                "confirmed": False,
            },
        )
        assert missing_confirmation.status_code == 409

        mismatched = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": "a-different-application",
                "resolution_reason": "I reviewed the blocker evidence.",
                "confirmed": True,
            },
        )
        assert mismatched.status_code == 409

        result = client.post(
            f"/api/applications/{application.id}/resolve-blocker",
            json={
                "application_id": application.id,
                "resolution_reason": "I reviewed the blocker evidence.",
                "confirmed": True,
            },
        )
        assert result.status_code == 200
        payload = result.json()
        assert payload["application_id"] == application.id
        assert payload["rechecked"] is True
        assert payload["submitted"] is False
        assert payload["resolution_recorded"] is True
        # The fixture has no approved CV, so a successful re-check advances to
        # the package gate and stops at NEEDS_USER rather than submitting.
        assert payload["state"] == "NEEDS_USER"
