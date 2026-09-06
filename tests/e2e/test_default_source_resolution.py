from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.main import create_app
from app.models import Application, AuditEvent, Opportunity
from app.services.navigator import ApplicationNavigator, SourceResolutionCapability
from app.services.target_resolution import TargetResolutionService


LISTING_HTML = """<!doctype html>
<html><body>
  <main data-source-listing="true">
    <h1>Summer Analyst Internship</h1>
    <p>ARGUS Test Capital</p>
    <a id="apply-link" href="/application">Apply on employer site</a>
  </main>
</body></html>
"""

APPLICATION_HTML = """<!doctype html>
<html><body>
  <main data-ats="greenhouse" data-employer="ARGUS Test Capital"
        data-role="Summer Analyst Internship"
        data-requisition="/application"
        data-argus-form-identity="argus-loopback-application">
    <h1>Summer Analyst Internship</h1>
    <p>ARGUS Test Capital</p>
    <form id="grnhse_app" action="/submit" method="post">
      <label>First name <input name="first_name" required></label>
      <label>Last name <input name="last_name" required></label>
      <label>Email <input name="email" type="email" required></label>
      <button type="submit" data-automation-id="submitButton">Submit application</button>
    </form>
  </main>
</body></html>
"""

DECEPTIVE_LISTING_HTML = """<!doctype html>
<html><body>
  <main data-source-listing="true">
    <h1>Summer Analyst Internship</h1>
    <p>ARGUS Test Capital</p>
    <p>This is a listing only; it contains no employer application form.</p>
  </main>
</body></html>
"""


class _LoopbackListingHandler(BaseHTTPRequestHandler):
    requests: list[str] = []
    lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        with self.lock:
            self.requests.append(self.path)
        if urlsplit(self.path).path == "/listing":
            body = LISTING_HTML.encode("utf-8")
            status = 200
        elif urlsplit(self.path).path == "/deceptive":
            body = DECEPTIVE_LISTING_HTML.encode("utf-8")
            status = 200
        elif urlsplit(self.path).path == "/application":
            body = APPLICATION_HTML.encode("utf-8")
            status = 200
        else:
            body = b"not found"
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture()
def loopback_listing():
    _LoopbackListingHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackListingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _LoopbackListingHandler
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _create_manual_application(
    client: TestClient, source_url: str
) -> tuple[str, str]:
    opportunity = client.post(
        "/api/opportunities",
        json={
            "employer": "ARGUS Test Capital",
            "role_title": "Summer Analyst Internship",
            "programme_group": "summer",
            "cycle": "2027",
            "url": source_url,
            "source": "manual",
            "ats_type": "greenhouse",
        },
    )
    assert opportunity.status_code == 201, opportunity.text
    opportunity_id = opportunity.json()["id"]
    evaluated = client.post(f"/api/opportunities/{opportunity_id}/evaluate")
    assert evaluated.status_code == 200, evaluated.text
    return opportunity_id, evaluated.json()["application_id"]


def test_unpatched_default_app_promotes_only_browser_verified_source_target(
    tmp_path: Path, loopback_listing
) -> None:
    """The production default must resolve visibly before Apply can use it.

    This deliberately does not inject ``target_resolution_resolver`` and does
    not replace any Navigator method.  The first public action must retain a
    headed, application-bound handoff.  A later status/resolution pass may
    promote only the browser's typed proof.  Apply is then checked at the
    loopback server boundary: its new requests may include ``/application``
    but must never revisit the discovery ``/listing`` URL.
    """

    base_url, request_log = loopback_listing
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_ENABLE_TRACKR_LIVE": "false",
            "ARGUS_BROWSER_HEADLESS": "true",
            "ARGUS_LIVE_DOMAIN_ALLOWLIST": "",
        }
    )
    app = create_app(settings)

    with TestClient(app) as client:
        opportunity_id, application_id = _create_manual_application(
            client, f"{base_url}/listing"
        )

        # This is intentionally the unpatched production state.  The default
        # adapter must obtain the concrete visible resolver from Navigator;
        # tests must not smuggle in a callable callback or URL.
        assert callable(getattr(app.state, "target_resolution_resolver", None))

        first = client.post(f"/api/applications/{application_id}/resolve-target")
        assert first.status_code == 202, first.text
        first_payload = first.json()
        assert first_payload["promoted"] is False
        assert first_payload["human_handoff_required"] is True
        handoff = first_payload["handoff"]
        session_id = handoff.get("session_id")
        assert isinstance(session_id, str) and session_id
        assert handoff.get("headed") is True
        assert first_payload["source_url"] == f"{base_url}/listing"
        assert first_payload["application_url"] is None

        # A handoff is a persistent owner-thread session, not a success.  The
        # public session endpoint must retain the exact application binding.
        session_response = client.get(f"/api/handoff/sessions/{session_id}")
        assert session_response.status_code == 200, session_response.text
        session_payload = session_response.json()
        assert session_payload["session_id"] == session_id
        assert session_payload["application_id"] == application_id
        assert session_payload["headed"] is True

        # Continue is an explicit, non-submitting owner-thread callback.  It
        # must reuse the same visible session rather than starting a second
        # browser or silently treating the first 202 as success.
        continued = client.post(
            f"/api/handoff/sessions/{session_id}/continue",
            json={"application_id": application_id},
        )
        assert continued.status_code == 200, continued.text
        assert continued.json()["session_id"] == session_id
        assert continued.json()["application_id"] == application_id

        # The headed owner completes source inspection asynchronously.  The
        # public resolution action is the callback/poll boundary; it may need
        # several bounded passes while the owner reaches a typed proof.
        resolved = None
        for _ in range(80):
            candidate = client.post(
                f"/api/applications/{application_id}/resolve-target",
                json={"confirmed": True},
            )
            if candidate.status_code == 200:
                resolved = candidate
                break
            assert candidate.status_code == 202, candidate.text
            payload = candidate.json()
            assert payload["promoted"] is False
            assert payload["application_url"] is None
            assert payload["handoff"].get("session_id") == session_id
            time.sleep(0.05)
        with request_log.lock:
            observed_resolution_paths = list(request_log.requests)
        assert resolved is not None, (
            "visible Navigator never returned verified target; "
            f"observed paths={observed_resolution_paths!r}"
        )
        resolved_payload = resolved.json()
        assert resolved_payload["promoted"] is True
        assert resolved_payload["application_id"] == application_id
        assert resolved_payload["source_url"] == f"{base_url}/listing"
        assert resolved_payload["application_url"] == f"{base_url}/application"
        assert resolved_payload["application_url"] != resolved_payload["source_url"]
        assert resolved_payload["automation_url"] == f"{base_url}/application"

        with request_log.lock:
            resolution_paths = list(request_log.requests)
        assert any(urlsplit(path).path == "/listing" for path in resolution_paths)
        assert any(urlsplit(path).path == "/application" for path in resolution_paths)

        # The persisted proof must contain the structured browser observation
        # bundle, not merely the opportunity's copied employer/role strings.
        with app.state.db.session_scope() as session:
            stored = session.get(Opportunity, opportunity_id)
            assert stored is not None
            envelope = json.loads(stored.resolution_evidence_json)
        observed = envelope["evidence"]
        assert observed["source_inspection"] is True
        assert observed["provider"] == "greenhouse"
        assert observed["employer"] == "ARGUS Test Capital"
        assert observed["role"] == "Summer Analyst Internship"
        assert observed["requisition"] == "/application"
        assert observed["form_identity"]

        before_apply = len(request_log.requests)
        apply_response = client.post(
            f"/api/handoff/applications/{application_id}/start?mode=review"
        )
        assert apply_response.status_code == 202, apply_response.text
        apply_payload = apply_response.json()
        assert apply_payload["application_id"] == application_id
        assert apply_payload["headed"] is True

        # Give the owner thread a bounded window to open the verified target.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with request_log.lock:
                apply_paths = list(request_log.requests[before_apply:])
            if any(urlsplit(path).path == "/application" for path in apply_paths):
                break
            time.sleep(0.05)
        with request_log.lock:
            apply_paths = list(request_log.requests[before_apply:])
        assert any(urlsplit(path).path == "/application" for path in apply_paths)
        assert not any(urlsplit(path).path == "/listing" for path in apply_paths)

        # Identical database employer/role values cannot promote a deceptive
        # listing that contains no independently observed application target.
        _deceptive_opportunity, deceptive_application = _create_manual_application(
            client, f"{base_url}/deceptive"
        )
        deceptive = client.post(
            f"/api/applications/{deceptive_application}/resolve-target"
        )
        assert deceptive.status_code == 202, deceptive.text
        assert deceptive.json()["promoted"] is False
        assert deceptive.json()["application_url"] is None


def test_guarded_js_apply_resolves_in_sterile_context_and_is_audited(
    live_server,
) -> None:  # noqa: ANN001 - shared E2E fixture
    source_url = f"{live_server.base_url}/lab/resolution/js-apply"
    expected_form_url = f"{live_server.base_url}/lab/resolution/js-application"
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(live_server.data_dir),
            "ARGUS_API_TOKEN": "e2e-token",
            "ARGUS_ENABLE_APPLY_CLICK": "true",
            "ARGUS_APPLY_CLICK_RUN_CAP": "3",
            "ARGUS_APPLY_CLICK_TIMEOUT_SECONDS": "5",
            "ARGUS_BROWSER_HEADLESS": "true",
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_LIVE_DOMAIN_ALLOWLIST": "",
        }
    )
    database = Database(settings)
    navigator = ApplicationNavigator(
        database,
        settings,
        headless=True,
        ttl_seconds=15,
    )
    try:
        with database.session_scope() as session:
            opportunity = Opportunity(
                employer="ARGUS Test Capital",
                role_title="Summer Analyst",
                programme_group="year_in_industry",
                cycle="2027",
                url=source_url,
                source="phase13_lab",
                ats_type="unknown",
                target_status=TargetKind.JOB_DETAIL.value,
            )
            session.add(opportunity)
            session.flush()
            application = Application(
                opportunity_id=opportunity.id,
                state=ApplicationState.NEEDS_USER.value,
            )
            session.add(application)
            session.flush()
            opportunity_id = opportunity.id
            application_id = application.id

        capability = SourceResolutionCapability(
            source_url=source_url,
            hostname="127.0.0.1",
            origin=live_server.base_url,
        )
        resolution, handoff = navigator.resolve_application_target(
            application_id,
            source_capability=capability,
            headed=False,
        )
        capability.revoke()

        assert resolution is not None
        assert resolution.kind is TargetKind.APPLICATION_FORM, repr(
            resolution.evidence
        )
        assert resolution.final_url == expected_form_url
        assert resolution.verified_for_automation is True
        click = resolution.evidence["apply_click"]
        assert click["accessible_name"] == "Apply now"
        assert click["control_role"] == "button"
        assert click["result_url"] == expected_form_url
        assert click["outcome"] == "verified_application_form"
        assert click["capability"]["id"] == capability.capability_id
        assert handoff["resumable"] is False
        assert handoff["can_continue"] is False
        diagnostics = navigator.diagnostics(str(handoff["session_id"]))
        assert diagnostics.worker_alive is False
        assert diagnostics.teardown_complete is True

        with database.session_scope() as session:
            outcome = TargetResolutionService(session).resolve(
                opportunity_id,
                application_id=application_id,
                resolver=lambda _context: resolution,
            )
            assert outcome.promoted is True, repr(outcome.resolution)
            assert outcome.target_status == TargetKind.APPLICATION_FORM.value

        with database.session_scope() as session:
            audit = session.scalar(
                select(AuditEvent)
                .where(
                    AuditEvent.entity_id == opportunity_id,
                    AuditEvent.event_type
                    == "opportunity.apply_click_resolution",
                )
                .order_by(AuditEvent.id.desc())
            )
            assert audit is not None
            details = json.loads(audit.details_json)
            assert details == {
                "accessible_name": "Apply now",
                "capability": {
                    "hostname": "127.0.0.1",
                    "id": capability.capability_id,
                    "origin": live_server.base_url,
                    "source_url": source_url,
                },
                "classification_outcome": TargetKind.APPLICATION_FORM.value,
                "clicked": True,
                "context_discarded": True,
                "outcome": "verified_application_form",
                "page_url": source_url,
                "post_click_url": expected_form_url,
                "pre_click_url": source_url,
                "result_url": expected_form_url,
                "role": "button",
                "verification_verdict": "verified",
            }
    finally:
        try:
            navigator.shutdown()
        finally:
            database.engine.dispose()
