"""Task 5 loopback journey contracts.

These tests intentionally drive the real local Chromium binary. They never
contact an employer, Trackr, or any non-loopback origin.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import sync_playwright

from app.automation.runner import AutomationRunner, SubmissionBlocked
from app.automation.types import RunMode, SessionState
from app.automation.adapters.greenhouse import GreenhouseAdapter
from app.automation.adapters.lever import LeverAdapter
from app.automation.targets import TargetResolution
from app.automation.host_policy import origin_for_url
from app.config import Settings
from app.db import Database
from app.domain.targets import TargetKind
from app.security.crypto import CryptoBox
from app.services.navigator import ApplicationNavigator
from tests.e2e.conftest import persist_verified_lab_target
from tests.e2e.test_lab_adapter_variants import (
    _assert_step_one_prefill,
    _configure_candidate,
    _lab_settings,
    _observe_journey_prepare,
    _prepare,
)


class _JourneyHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib handler API
        query = urlsplit(self.path).query
        path = urlsplit(self.path).path
        provider = "lever" if "provider=lever" in query else "greenhouse"
        if "popup=1" in query:
            body = f'''<!doctype html><html><body><main data-ats="{provider}" data-employer="Journey Corp" data-role="Analyst"><iframe src="/entry?frame=1&provider={provider}" title="Application form"></iframe></main></body></html>'''.encode()
        elif "frame=1" in query:
            body = f'''<!doctype html><html><body><main data-ats="{provider}" data-employer="Journey Corp" data-role="Analyst"><form class="ats-form {provider}-form" data-argus-form-identity="journey-entry" action="/entry/submit" method="post"><label>First name<input name="first_name" required></label><label>Last name<input name="last_name" required></label><button type="submit" data-automation-id="submitButton">Submit application</button></form></main></body></html>'''.encode()
        elif path == "/entry":
            body = f'''<!doctype html><html><body><main data-ats="{provider}" data-employer="Journey Corp" data-role="Analyst"><button data-automation-id="applyButton">Apply</button><script>document.querySelector('button').onclick=()=>window.open('/entry?popup=1&provider={provider}','journey-popup')</script></main></body></html>'''.encode()
        else:
            body = b"<!doctype html><html><body><main data-ats='greenhouse'><h1>Journey</h1></main></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.fixture()
def journey_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JourneyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/application"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _verified_loopback_resolution(url: str, *, provider: str = "greenhouse") -> TargetResolution:
    return TargetResolution(
        source_url=url,
        final_url=url,
        kind=TargetKind.APPLICATION_ENTRY,
        provider=provider,
        identity_verified=True,
        evidence={
            "synthetic_lab": True,
            "provider": provider,
            "application_origin": origin_for_url(url),
            "employer": "Journey Corp",
            "role": "Analyst",
            # Keep the persisted requisition proof bound to the exact
            # application URL used by this fixture.  The provider-adapter
            # test below intentionally exercises its separate /entry popup.
            "requisition": urlsplit(url).path or "/",
            "form_identity": f"journey-{provider}",
        },
    )


def test_typed_journey_command_runs_on_navigator_owner_thread(journey_url):
    owner_ids: list[int] = []

    class Journey:
        def prepare(self, page):
            owner_ids.append(threading.get_ident())
            return {"state": "ACTIVE", "manifest": {"url": page.url}}

    navigator = ApplicationNavigator(
        ttl_seconds=5,
        headless=True,
        url_resolver=lambda _application_id: (
            _verified_loopback_resolution(journey_url),
            {"source_url": "http://source.invalid/listing"},
        ),
    )
    try:
        session = navigator.start(
            "journey-1",
            RunMode.REVIEW,
            journey_executor=Journey(),
        )
        navigator.run_journey(session.session_id)
        result = navigator.wait_for_journey_result(session.session_id, timeout=5)
        assert result["state"] == "ACTIVE"
        diagnostics = navigator.diagnostics(session.session_id)
        assert owner_ids == [diagnostics.owner_thread_id]
        assert all(thread_id == diagnostics.owner_thread_id for _, thread_id in diagnostics.operation_log)
    finally:
        navigator.cancel(session.session_id)
        navigator.shutdown()


def test_navigator_refuses_raw_or_unresolved_target_before_worker_creation(journey_url):
    created: list[object] = []

    def worker_factory(**kwargs):
        created.append(kwargs)
        raise AssertionError("worker creation must not occur for an unresolved target")

    navigator = ApplicationNavigator(
        ttl_seconds=5,
        headless=True,
        worker_factory=worker_factory,
        url_resolver=lambda _application_id: (journey_url, {}),
    )
    with pytest.raises(ValueError, match="TargetResolution|raw URL"):
        navigator.start("unresolved-1", RunMode.REVIEW)
    with pytest.raises(ValueError, match="raw url"):
        navigator.start(
            "unresolved-2",
            RunMode.REVIEW,
            url=journey_url,
        )
    assert created == []


def test_final_submission_requires_exact_action_time_confirmation(journey_url):
    clicked: list[int] = []

    class Journey:
        def prepare(self, _page):
            return {
                "state": "FINAL_REVIEW",
                "manifest": {
                    "application_id": "journey-2",
                    "employer": "Journey Corp",
                    "role": "Analyst",
                    "provider": "greenhouse",
                    "requisition": urlsplit(journey_url).path,
                    "application_url": journey_url,
                    "destination": f"{journey_url}/submit",
                    "form_identity": "journey-form",
                    "root_selector": "form",
                    "control_selector": "button[type=submit]",
                    "target_fingerprint": "journey-target-v1",
                    "control_fingerprint": "journey-submit-v1",
                    "form_action": f"{journey_url}/submit",
                    "method": "POST",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                    "final_url": journey_url,
                    "frame_url": journey_url,
                    "expected_receipt_url": f"{journey_url}/receipt",
                    "expected_final_url": f"{journey_url}/receipt",
                    "documents": [],
                    "answers": [],
                    "submission": "bound",
                },
            }

        def submit(self, _page, confirmation_id):
            assert confirmation_id == "journey-2"
            clicked.append(threading.get_ident())
            return {"state": "CONFIRMED", "submission": {"posted": True}}

    navigator = ApplicationNavigator(
        ttl_seconds=5,
        headless=True,
        url_resolver=lambda _application_id: _verified_loopback_resolution(journey_url),
    )
    try:
        session = navigator.start("journey-2", RunMode.SUBMIT, journey_executor=Journey())
        navigator.run_journey(session.session_id)
        ready = navigator.wait_for_journey_result(session.session_id, timeout=5)
        assert ready["state"] == "FINAL_REVIEW"
        assert clicked == []
        navigator.confirm_submission(session.session_id, confirmation_id="wrong-id")
        assert clicked == []
        navigator.confirm_submission(session.session_id, confirmation_id="journey-2")
        deadline = time.monotonic() + 5
        result = {}
        while time.monotonic() < deadline:
            result = dict(navigator.journey_result(session.session_id))
            if "authority gate" in str(result.get("reason") or "").casefold():
                break
            time.sleep(0.01)
        assert result["state"] == "FINAL_REVIEW"
        assert "authority gate" in result["reason"].casefold()
        assert clicked == []
    finally:
        navigator.shutdown()


@pytest.mark.parametrize("adapter_type", [GreenhouseAdapter, LeverAdapter])
def test_provider_adopts_popup_and_iframe_application_root(journey_url, adapter_type):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            provider = "greenhouse" if adapter_type is GreenhouseAdapter else "lever"
            entry_url = journey_url.replace("/application", "/entry") + f"?provider={provider}"
            page.goto(entry_url)
            resolution = TargetResolution(
                source_url=page.url,
                final_url=page.url,
                kind=TargetKind.APPLICATION_ENTRY,
                provider=provider,
                identity_verified=True,
                evidence={
                    "synthetic_lab": True,
                    "employer": "Journey Corp",
                    "role": "Analyst",
                    "requisition": "/entry",
                },
            )
            adapter = adapter_type(resolution)
            assert adapter.enter_application_flow(page) is True
            fields, evidence = adapter.inspect_with_evidence(page)
            assert fields
            assert evidence["root_found"] is True
            scope = adapter.current_scope(page)
            assert scope is not page
            assert "frame=1" in scope.url
        finally:
            browser.close()


def test_workday_prefill_hands_off_on_first_step_without_next_or_post(live_server, monkeypatch):
    """PREFILL fills only the first step; CAPTCHA is not reached autonomously."""
    dom = _assert_step_one_prefill(live_server, monkeypatch, "workday-journey", "workday")
    assert dom["workday"] == {"step": 0}
    assert dom["fields"] == {"first_name": "Demo", "last_name": "Candidate"}


@pytest.mark.parametrize(
    ("scenario", "adapter"),
    [
        ("workday-journey", "workday"),
        ("smartrecruiters-journey", "smartrecruiters"),
        ("workable-journey", "workable"),
    ],
)
def test_low_level_submit_mode_journey_stops_before_activation(
    live_server, monkeypatch, tmp_path, scenario, adapter
):
    """Prepare-only adapter coverage, NOT a public autonomous submit success.

    The low-level SUBMIT journey can exercise Next/CAPTCHA/final targeting.
    It never requests authority or calls submit/confirm, and retains the real
    Navigator route guard. Public runner SUBMIT still requires exact authority.
    """
    from app.automation.runner import _OwnerThreadJourney
    from app.models import Opportunity
    from tests.document_helpers import cv_docx_bytes

    settings = _lab_settings(live_server)
    database = Database(settings)
    navigator = ApplicationNavigator(database, settings, headless=True)
    runner = AutomationRunner(
        database, settings, CryptoBox.from_path(settings.secret_key_path),
        handoff_manager=navigator,
    )
    target_url = f"{live_server.base_url}/lab/ats/{scenario}"
    resolution = TargetResolution(
        source_url=target_url, final_url=target_url,
        kind=TargetKind.APPLICATION_ENTRY, provider=adapter, identity_verified=True,
        evidence={
            "synthetic_lab": True, "provider": adapter,
            "application_origin": origin_for_url(target_url),
            "employer": "ARGUS Test Capital", "role": "Summer Analyst",
            "requisition": urlsplit(target_url).path, "form_identity": scenario,
        },
    )
    cv = tmp_path / "Synthetic Journey CV.docx"
    cv.write_bytes(cv_docx_bytes(2028, "Prepare-only journey"))
    application_id = f"prepare-only-{scenario}"
    journey = _OwnerThreadJourney(
        runner, application_id=application_id,
        opportunity=Opportunity(
            employer="ARGUS Test Capital", role_title="Summer Analyst",
            programme_group="summer", cycle="2027", url=target_url,
        ),
        resolution=resolution, mode=RunMode.SUBMIT,
        profile_values={
            "identity.first_name": "Demo", "identity.last_name": "Candidate",
            "contact.email": "demo@example.test",
            "education.university": "Example University",
            "education.graduation_year": 2028, "legal.sponsorship": False,
        },
        answers={}, documents={"document.cv": str(cv)},
    )
    observations = _observe_journey_prepare(monkeypatch)
    try:
        session = navigator.start(
            application_id, RunMode.SUBMIT, resolution=resolution,
            journey_executor=journey,
        )
        navigator.run_journey(session.session_id)
        result = navigator.wait_for_journey_result(session.session_id, timeout=30)
        snapshot = navigator.get(session.session_id)
        assert snapshot.worker_alive is True
        assert len(observations) == 1, result
        observed = observations[0]
        assert observed["owner_thread_id"] == navigator.diagnostics(session.session_id).owner_thread_id
        assert observed["mutations"] == []
        assert observed["dom"]["clicks"]["submit"] == 0
        assert journey.submission_binding == {}
        assert journey._click_boundary_crossed is False
        if adapter == "workday":
            assert result["state"] == "HUMAN_REQUIRED", result
            assert snapshot.state is SessionState.HUMAN_REQUIRED
            assert "human_boundary" in result["blocked_reasons"]
            assert result["step_index"] == 3
            assert result["manifest"]["submission"] == "not_clicked"
            assert result["manifest"]["stage"] == "after_next"
            assert "captcha" in result["manifest"]["human_boundary"].casefold()
            assert observed["dom"]["captcha"] is True
            assert observed["dom"]["workday"] == {"step": 3}
            assert observed["dom"]["clicks"]["next"] == 3
        else:
            assert result["state"] == "FINAL_REVIEW", result
            assert snapshot.state is SessionState.FINAL_REVIEW
            assert result["step_index"] == 1
            assert observed["dom"]["provider"] == {
                "step": 1, "nextClicks": 1, "distractionClicks": 0,
            }
            assert observed["dom"]["clicks"]["next"] == 1
            assert observed["dom"]["final_submit"] is True
            fields = observed["dom"]["fields"]
            assert fields["first_name"] == "Demo"
            assert fields["last_name"] == "Candidate"
            assert fields["email"] == "demo@example.test"
            assert fields["graduation_year"] == "2028"
            assert fields["sponsor"] == "no"
            assert len(fields["cv"]) == 1
            assert fields["cv"][0]["size"] > 0
            assert fields["lab_distraction_clicks"] == "0"
            assert fields["lab_next_clicks"] == "1"
            assert fields["lab_step_marker"] == "review"
            assert result["manifest"]["method"] == "POST"
            assert result["manifest"]["form_action"] == f"{target_url}/submit"
        assert httpx.get(f"{live_server.base_url}/api/lab/submissions", timeout=10).json() == []
    finally:
        navigator.shutdown()
        database.engine.dispose()


def test_route_rejects_candidate_egress_on_different_canonical_origin():
    """A same-host, different-port candidate POST is not same-origin."""

    from app.services.navigator import HeadedSessionWorker

    class Request:
        url = "http://127.0.0.1:9999/api/collect"
        method = "POST"
        post_data = "email=alex%40example.test"
        headers = {"content-type": "application/x-www-form-urlencoded"}

    class Route:
        request = Request()
        aborted = ""
        continued = False

        def abort(self, reason):
            self.aborted = reason

        def continue_(self):
            self.continued = True

    worker = object.__new__(HeadedSessionWorker)
    worker.owner_thread_id = threading.get_ident()
    worker._operation_log = []
    worker.allowlist = frozenset({"127.0.0.1"})
    worker.url = "http://127.0.0.1:8787/lab/ats/greenhouse"
    worker._verified_origin = "http://127.0.0.1:8787"
    worker._egress_records = []
    worker._egress_fatal_reason = ""
    worker.journey_executor = None

    route = Route()
    worker._route(route)

    assert route.aborted == "blockedbyclient"
    assert route.continued is False
    assert worker._egress_fatal_reason


def test_route_missing_method_is_unknown_and_fatal_even_on_verified_origin():
    from app.services.navigator import HeadedSessionWorker

    class Request:
        url = "http://127.0.0.1:8787/assets/unknown.js"
        post_data = ""
        headers = {}

    class Route:
        request = Request()
        aborted = ""
        continued = False

        def abort(self, reason):
            self.aborted = reason

        def continue_(self):
            self.continued = True

    worker = object.__new__(HeadedSessionWorker)
    worker.owner_thread_id = threading.get_ident()
    worker._operation_log = []
    worker.allowlist = frozenset({"127.0.0.1"})
    worker.url = "http://127.0.0.1:8787/lab/ats/greenhouse"
    worker._verified_origin = "http://127.0.0.1:8787"
    worker._egress_records = []
    worker._egress_fatal_reason = ""
    worker.journey_executor = None

    route = Route()
    worker._route(route)

    assert route.aborted == "blockedbyclient"
    assert route.continued is False
    assert worker._egress_records[-1]["method"] == ""
    assert worker._egress_records[-1]["fatal"] is True


def test_read_only_route_blocks_same_origin_candidate_request():
    """INSPECT/REVIEW/DRY_RUN never deliver candidate-bearing browser data."""

    from app.services.navigator import HeadedSessionWorker

    class Request:
        url = "http://127.0.0.1:8787/api/profile"
        method = "POST"
        post_data = "email=alex%40example.test"
        headers = {"content-type": "application/x-www-form-urlencoded"}

    class Route:
        request = Request()
        aborted = ""
        continued = False

        def abort(self, reason):
            self.aborted = reason

        def continue_(self):
            self.continued = True

    worker = object.__new__(HeadedSessionWorker)
    worker.owner_thread_id = threading.get_ident()
    worker._operation_log = []
    worker.mode = RunMode.REVIEW.value
    worker.allowlist = frozenset({"127.0.0.1"})
    worker.url = "http://127.0.0.1:8787/lab/ats/greenhouse"
    worker._verified_origin = "http://127.0.0.1:8787"
    worker._egress_records = []
    worker._egress_fatal_reason = ""
    worker.journey_executor = None

    route = Route()
    worker._route(route)

    assert route.aborted == "blockedbyclient"
    assert route.continued is False
    assert worker._egress_records[-1]["classification"] == "data_bearing"
    assert worker._egress_records[-1]["fatal"] is True


def test_owner_submit_exception_after_click_acquisition_emits_unknown_with_binding():
    from queue import Queue

    from app.automation.types import SessionCommand, SessionCommandType
    from app.services.navigator import HeadedSessionWorker

    class Journey:
        _click_boundary_crossed = True
        submission_binding = {"id": "intent-1", "nonce": "nonce-1"}

        def before_click(self):
            return None

        def submit(self, _page, _confirmation_id):
            raise RuntimeError("synthetic owner failure after acquisition")

        def receipt_evidence_payload(self):
            return {
                "bound_target": {
                    "target_fingerprint": "target-1",
                    "provider": "greenhouse",
                },
                "bound_intent": dict(self.submission_binding),
            }

    worker = object.__new__(HeadedSessionWorker)
    worker.owner_thread_id = threading.get_ident()
    worker._operation_log = []
    worker.session_id = "owner-session"
    worker.mode = RunMode.SUBMIT.value
    worker._state = SessionState.FINAL_REVIEW
    worker.application_id = "owner-exception"
    worker.url = "http://127.0.0.1:8787/application"
    worker._deadline_monotonic = time.monotonic() + 30
    worker.expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    worker.journey_executor = Journey()
    worker._page = SimpleNamespace(url=worker.url)
    worker.summary = {}
    worker._manifest = {
        "application_id": "owner-exception",
        "employer": "Example Employer",
        "role": "R",
        "provider": "greenhouse",
        "requisition": "/application",
        "target_fingerprint": "target-1",
        "control_fingerprint": "control-1",
        "form_action": f"{worker.url}/submit",
        "method": "POST",
        "expires_at": worker.expires_at.isoformat(),
        "final_url": worker.url,
        "expected_receipt_url": f"{worker.url}/receipt",
    }
    worker._journey_result = {}
    worker._human_required_sticky = False
    worker._human_required_reason = ""
    worker._stop_requested = False
    worker.event_queue = Queue()

    worker._confirm_submission(
        SessionCommand(
            command=SessionCommandType.CONFIRM_SUBMISSION,
            session_id="owner-session",
            payload={"confirmation_id": "owner-exception"},
        )
    )

    events = []
    while not worker.event_queue.empty():
        events.append(worker.event_queue.get_nowait())
    journey_event = next(
        event for event in events if event.event.value == "JOURNEY_RESULT"
    )
    assert journey_event.payload["state"] == "UNKNOWN"
    assert journey_event.payload["click_boundary_crossed"] is True
    assert journey_event.payload["receipt_evidence"]["bound_intent"] == {
        "id": "intent-1",
        "nonce": "nonce-1",
    }


def test_loopback_submit_uses_strict_receipt_contract_not_legacy_helper(
    live_server, monkeypatch
):
    """A real one-POST loopback submit must prove the bound receipt contract."""

    from fastapi.testclient import TestClient

    from app.automation import runner as runner_module
    from app.main import create_app

    _configure_candidate(live_server.base_url)
    application_id = _prepare(live_server, "greenhouse", "greenhouse")
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(live_server.data_dir),
            "ARGUS_API_TOKEN": "e2e-token",
            "ARGUS_PORT": live_server.base_url.rsplit(":", 1)[1],
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_BROWSER_HEADLESS": "true",
        }
    )

    def legacy_helper_called(*_args, **_kwargs):
        raise AssertionError("legacy receipt helper must not decide submission truth")

    monkeypatch.setattr(
        runner_module, "detect_receipt", legacy_helper_called, raising=False
    )
    monkeypatch.setattr(
        runner_module,
        "receipt_has_submission_evidence",
        legacy_helper_called,
        raising=False,
    )
    with TestClient(create_app(settings)) as client:
        review = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "prefill", "headed": "true"},
        )
        assert review.status_code == 200, review.text
        session_id = review.json()["handoff_session_id"]
        assert session_id, review.json()
        manifest = client.post(
            f"/api/handoff/sessions/{session_id}/final-manifest",
            json={"application_id": application_id},
        )
        assert manifest.status_code == 200, manifest.text
        confirmed = client.post(
            f"/api/handoff/sessions/{session_id}/confirm",
            json={"application_id": application_id},
        )
        assert confirmed.status_code == 200, confirmed.text
        authority = confirmed.json()["authority"]["authority_id"]
        response = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "submit"},
            json={"application_id": application_id, "session_id": session_id, "authority_id": authority},
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "CONFIRMATION_VERIFIED"
        retry = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "submit"},
            json={
                "application_id": application_id,
                "session_id": session_id,
                "authority_id": authority,
            },
        )
        assert retry.status_code == 409, retry.text
    assert len(httpx.get(f"{live_server.base_url}/api/lab/submissions").json()) == 1

    from sqlalchemy import select

    from app.models import Application, SubmissionAuthority, SubmissionIntent

    probe = Database(settings)
    with probe.session_scope() as session:
        authority_row = session.get(SubmissionAuthority, authority)
        assert authority_row is not None
        assert authority_row.consumed_at is not None
        intents = list(
            session.scalars(
                select(SubmissionIntent).where(
                    SubmissionIntent.application_id == application_id
                )
            ).all()
        )
        assert len(intents) == 1
        assert intents[0].status == SubmissionIntent.CONFIRMED
        application = session.get(Application, application_id)
        assert application is not None
        assert application.state == "CONFIRMATION_VERIFIED"
        assert application.submission_reference


def test_direct_runner_submit_without_exact_authority_refuses_and_cleans_owner(live_server):
    """CLI/scheduler-style callers cannot bootstrap or leak a submit journey."""

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import Application, AutomationRun, SubmissionIntent

    _configure_candidate(live_server.base_url)
    application_id = _prepare(live_server, "greenhouse", "greenhouse")
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(live_server.data_dir),
            "ARGUS_API_TOKEN": "e2e-token",
            "ARGUS_PORT": live_server.base_url.rsplit(":", 1)[1],
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_BROWSER_HEADLESS": "true",
        }
    )
    with TestClient(create_app(settings)) as client:
        assert client.app.state.navigator.all_sessions() == []
        runner = AutomationRunner(
            client.app.state.db,
            settings,
            client.app.state.crypto,
            handoff_manager=client.app.state.navigator,
        )
        with client.app.state.db.session_scope() as session:
            original_state = session.get(Application, application_id).state
        with pytest.raises(SubmissionBlocked, match="exact Navigator session and authority"):
            runner.run(application_id, RunMode.SUBMIT, headed=True)
        assert client.app.state.navigator.all_sessions() == []
        assert all(
            not snapshot.worker_alive and snapshot.cleanup_complete
            for snapshot in client.app.state.navigator.all_sessions()
        )
        with client.app.state.db.session_scope() as session:
            assert session.get(Application, application_id).state == original_state
            assert session.query(AutomationRun).filter_by(
                application_id=application_id
            ).count() == 0
            assert session.query(SubmissionIntent).filter_by(
                application_id=application_id
            ).count() == 0

    assert httpx.get(f"{live_server.base_url}/api/lab/submissions").json() == []


def test_pre_authority_confirmed_result_cannot_promote_application(
    live_server, monkeypatch
):
    """A custom/faulty prepare result cannot bypass the click authority path."""

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.automation import runner as runner_module
    from app.main import create_app
    from app.models import Application, SubmissionAuthority, SubmissionIntent

    _configure_candidate(live_server.base_url)
    application_id = _prepare(live_server, "greenhouse", "greenhouse")
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(live_server.data_dir),
            "ARGUS_API_TOKEN": "e2e-token",
            "ARGUS_PORT": live_server.base_url.rsplit(":", 1)[1],
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_BROWSER_HEADLESS": "true",
        }
    )
    with TestClient(create_app(settings)) as client:
        review = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "prefill", "headed": "true"},
        )
        assert review.status_code == 200, review.text
        review_session_id = review.json()["handoff_session_id"]
        displayed = client.post(
            f"/api/handoff/sessions/{review_session_id}/final-manifest",
            json={"application_id": application_id},
        )
        assert displayed.status_code == 200, displayed.text
        confirmed = client.post(
            f"/api/handoff/sessions/{review_session_id}/confirm",
            json={"application_id": application_id},
        )
        assert confirmed.status_code == 200, confirmed.text
        authority_id = confirmed.json()["authority"]["authority_id"]

        monkeypatch.setattr(
            runner_module._OwnerThreadJourney,
            "prepare",
            lambda self, _page: {
                "state": "CONFIRMED",
                "risk_level": 0,
                "click_boundary_crossed": False,
                "receipt": {
                    "reference": "forged-pre-authority",
                    "url": self.resolution.final_url,
                    "confirmation_text": "forged",
                },
            },
        )
        refused = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "submit", "headed": "true"},
            json={
                "application_id": application_id,
                "session_id": review_session_id,
                "authority_id": authority_id,
            },
        )
        assert refused.status_code == 409, refused.text
        with client.app.state.db.session_scope() as session:
            authority = session.get(SubmissionAuthority, authority_id)
            application = session.get(Application, application_id)
            assert authority is not None and authority.consumed_at is None
            assert application is not None
            assert application.state != "CONFIRMATION_VERIFIED"
            assert list(
                session.scalars(
                    select(SubmissionIntent).where(
                        SubmissionIntent.application_id == application_id
                    )
                ).all()
            ) == []

    assert httpx.get(f"{live_server.base_url}/api/lab/submissions").json() == []


@pytest.mark.parametrize("injection_stage", ["before_preflight", "after_preflight"])
def test_captcha_preflight_hands_off_without_consuming_authority_or_post(
    live_server, monkeypatch, injection_stage
):
    """A challenge appearing after review preserves the one-shot token."""

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.main import create_app
    from app.models import SubmissionAuthority, SubmissionIntent

    _configure_candidate(live_server.base_url)
    application_id = _prepare(live_server, "greenhouse", "greenhouse")
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(live_server.data_dir),
            "ARGUS_API_TOKEN": "e2e-token",
            "ARGUS_PORT": live_server.base_url.rsplit(":", 1)[1],
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_BROWSER_HEADLESS": "true",
        }
    )
    with TestClient(create_app(settings)) as client:
        review = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "prefill", "headed": "true"},
        )
        assert review.status_code == 200, review.text
        review_session_id = review.json()["handoff_session_id"]
        assert review_session_id, review.json()
        manifest = client.post(
            f"/api/handoff/sessions/{review_session_id}/final-manifest",
            json={"application_id": application_id},
        )
        assert manifest.status_code == 200, manifest.text
        confirmed = client.post(
            f"/api/handoff/sessions/{review_session_id}/confirm",
            json={"application_id": application_id},
        )
        assert confirmed.status_code == 200, confirmed.text
        authority_id = confirmed.json()["authority"]["authority_id"]
        with client.app.state.db.session_scope() as session:
            issued = session.get(SubmissionAuthority, authority_id)
            assert issued is not None
            fixed_times = (issued.issued_at, issued.expires_at)

        if injection_stage == "before_preflight":
            original_preflight = ApplicationNavigator.preflight_submission

            def challenge_before_preflight(self, session_id, *, timeout=5.0):
                self.inject_human_required(session_id, reason="captcha_detected")
                return original_preflight(self, session_id, timeout=timeout)

            monkeypatch.setattr(
                ApplicationNavigator,
                "preflight_submission",
                challenge_before_preflight,
            )
        else:
            original_confirm_submission = ApplicationNavigator.confirm_submission

            def challenge_after_preflight(self, session_id, *, confirmation_id):
                self.inject_human_required(session_id, reason="captcha_detected")
                return original_confirm_submission(
                    self,
                    session_id,
                    confirmation_id=confirmation_id,
                )

            monkeypatch.setattr(
                ApplicationNavigator,
                "confirm_submission",
                challenge_after_preflight,
            )
        response = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "submit", "headed": "true"},
            json={
                "application_id": application_id,
                "session_id": review_session_id,
                "authority_id": authority_id,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "NEEDS_USER"
        execution_session_id = response.json()["handoff_session_id"]
        assert execution_session_id
        assert response.json()["human_boundary"]["kind"] == "captcha"

        continued = client.post(
            f"/api/handoff/sessions/{execution_session_id}/continue",
            json={"application_id": application_id},
        )
        assert continued.status_code == 200, continued.text
        assert continued.json()["status"] in {"HUMAN_REQUIRED", "ACTIVE", "FINAL_REVIEW"}

        with client.app.state.db.session_scope() as session:
            authority = session.get(SubmissionAuthority, authority_id)
            assert authority is not None
            assert authority.consumed_at is None
            assert (authority.issued_at, authority.expires_at) == fixed_times
            intents = list(
                session.scalars(
                    select(SubmissionIntent).where(
                        SubmissionIntent.application_id == application_id
                    )
                ).all()
            )
            if injection_stage == "before_preflight":
                assert intents == []
            else:
                assert len(intents) == 1
                assert intents[0].status == SubmissionIntent.FAILED_LOCAL

    assert httpx.get(f"{live_server.base_url}/api/lab/submissions").json() == []


def test_approved_answer_mutation_after_review_refuses_before_consume_or_post(
    live_server,
):
    """The authority binds the encrypted identity of every approved answer."""

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.main import create_app
    from app.models import SubmissionAuthority, SubmissionIntent
    from app.services.answers import AnswerService

    _configure_candidate(live_server.base_url)
    application_id = _prepare(live_server, "greenhouse", "greenhouse")
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(live_server.data_dir),
            "ARGUS_API_TOKEN": "e2e-token",
            "ARGUS_PORT": live_server.base_url.rsplit(":", 1)[1],
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_BROWSER_HEADLESS": "true",
        }
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session_scope() as session:
            AnswerService(session, client.app.state.crypto).upsert(
                canonical_key="answer.motivation",
                prompt="Why this role?",
                answer="Reviewed answer",
                approved=True,
                sensitive=False,
            )
        review = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "prefill", "headed": "true"},
        )
        assert review.status_code == 200, review.text
        review_session_id = review.json()["handoff_session_id"]
        displayed = client.post(
            f"/api/handoff/sessions/{review_session_id}/final-manifest",
            json={"application_id": application_id},
        )
        assert displayed.status_code == 200, displayed.text
        assert displayed.json()["manifest"]["answers"]
        confirmed = client.post(
            f"/api/handoff/sessions/{review_session_id}/confirm",
            json={"application_id": application_id},
        )
        assert confirmed.status_code == 200, confirmed.text
        authority_id = confirmed.json()["authority"]["authority_id"]

        with client.app.state.db.session_scope() as session:
            AnswerService(session, client.app.state.crypto).upsert(
                canonical_key="answer.motivation",
                prompt="Why this role?",
                answer="Changed after review",
                approved=True,
                sensitive=False,
            )

        refused = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "submit"},
            json={
                "application_id": application_id,
                "session_id": review_session_id,
                "authority_id": authority_id,
            },
        )
        assert refused.status_code == 409, refused.text
        with client.app.state.db.session_scope() as session:
            authority = session.get(SubmissionAuthority, authority_id)
            assert authority is not None and authority.consumed_at is None
            assert list(
                session.scalars(
                    select(SubmissionIntent).where(
                        SubmissionIntent.application_id == application_id
                    )
                ).all()
            ) == []

    assert httpx.get(f"{live_server.base_url}/api/lab/submissions").json() == []


@pytest.mark.parametrize("mutation", ["path", "action", "control"])
def test_http_confirm_rejects_same_origin_target_mutation_after_display(
    live_server, mutation
):
    """The handoff route binds exact paths/actions/controls, not just origins."""

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import SubmissionAuthority

    _configure_candidate(live_server.base_url)
    application_id = _prepare(live_server, "greenhouse", "greenhouse")
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(live_server.data_dir),
            "ARGUS_API_TOKEN": "e2e-token",
            "ARGUS_PORT": live_server.base_url.rsplit(":", 1)[1],
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_BROWSER_HEADLESS": "true",
        }
    )
    with TestClient(create_app(settings)) as client:
        review = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "prefill", "headed": "true"},
        )
        assert review.status_code == 200, review.text
        review_session_id = review.json()["handoff_session_id"]
        displayed = client.post(
            f"/api/handoff/sessions/{review_session_id}/final-manifest",
            json={"application_id": application_id},
        )
        assert displayed.status_code == 200, displayed.text

        navigator = client.app.state.navigator
        with navigator._lock:
            record = navigator._find_record(review_session_id)
            if mutation == "path":
                record.manifest["application_url"] = (
                    f"{live_server.base_url}/lab/ats/same-origin-other"
                )
            elif mutation == "action":
                changed_action = f"{live_server.base_url}/lab/ats/other-submit"
                record.manifest["destination"] = changed_action
                record.manifest["form_action"] = changed_action
                record.manifest["expected_final_url"] = changed_action
                record.manifest["expected_receipt_url"] = changed_action
            else:
                record.manifest["control_selector"] = "form#other button[type=submit]"
                record.manifest["control_fingerprint"] = "same-origin-other-control"
                record.manifest["form_fingerprint"] = "same-origin-other-control"

        refused = client.post(
            f"/api/handoff/sessions/{review_session_id}/confirm",
            json={"application_id": application_id},
        )
        assert refused.status_code == 409, refused.text
        with client.app.state.db.session_scope() as session:
            assert session.query(SubmissionAuthority).filter_by(
                application_id=application_id,
                session_id=review_session_id,
            ).count() == 0


def test_concurrent_http_confirm_returns_one_durable_authority_id(
    live_server, monkeypatch
):
    """Two simultaneous confirmations cannot mint duplicate capabilities."""

    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import SubmissionAuthority

    _configure_candidate(live_server.base_url)
    application_id = _prepare(live_server, "greenhouse", "greenhouse")
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(live_server.data_dir),
            "ARGUS_API_TOKEN": "e2e-token",
            "ARGUS_PORT": live_server.base_url.rsplit(":", 1)[1],
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_BROWSER_HEADLESS": "true",
        }
    )
    with TestClient(create_app(settings)) as client:
        review = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "prefill", "headed": "true"},
        )
        assert review.status_code == 200, review.text
        review_session_id = review.json()["handoff_session_id"]
        displayed = client.post(
            f"/api/handoff/sessions/{review_session_id}/final-manifest",
            json={"application_id": application_id},
        )
        assert displayed.status_code == 200, displayed.text

        barrier = threading.Barrier(2)
        original_confirm = client.app.state.navigator.confirm

        def gated_confirm(session_id):
            barrier.wait(timeout=10)
            return original_confirm(session_id)

        monkeypatch.setattr(client.app.state.navigator, "confirm", gated_confirm)

        def confirm_once():
            return client.post(
                f"/api/handoff/sessions/{review_session_id}/confirm",
                json={"application_id": application_id},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _index: confirm_once(), range(2)))
        assert all(response.status_code == 200 for response in responses), [
            response.text for response in responses
        ]
        authority_ids = {
            response.json()["authority"]["authority_id"] for response in responses
        }
        assert len(authority_ids) == 1
        with client.app.state.db.session_scope() as session:
            rows = session.query(SubmissionAuthority).filter_by(
                application_id=application_id,
                session_id=review_session_id,
            ).all()
            assert len(rows) == 1
            assert rows[0].id == next(iter(authority_ids))
            assert rows[0].consumed_at is None


@pytest.mark.parametrize(
    "mutation",
    ["selected_id", "stored_hash", "approval", "file_bytes", "file_missing"],
)
def test_document_mutation_after_review_refuses_before_consume_or_post(
    live_server, mutation
):
    """Every selected-document mutation invalidates the reviewed authority."""

    from pathlib import Path

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.main import create_app
    from app.models import Application, Document, SubmissionAuthority, SubmissionIntent
    from app.services.documents import DocumentService

    _configure_candidate(live_server.base_url)
    application_id = _prepare(live_server, "greenhouse", "greenhouse")
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(live_server.data_dir),
            "ARGUS_API_TOKEN": "e2e-token",
            "ARGUS_PORT": live_server.base_url.rsplit(":", 1)[1],
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_BROWSER_HEADLESS": "true",
        }
    )
    with TestClient(create_app(settings)) as client:
        review = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "prefill", "headed": "true"},
        )
        assert review.status_code == 200, review.text
        review_session_id = review.json()["handoff_session_id"]
        assert review_session_id, review.json()
        manifest = client.post(
            f"/api/handoff/sessions/{review_session_id}/final-manifest",
            json={"application_id": application_id},
        )
        assert manifest.status_code == 200, manifest.text
        confirmed = client.post(
            f"/api/handoff/sessions/{review_session_id}/confirm",
            json={"application_id": application_id},
        )
        assert confirmed.status_code == 200, confirmed.text
        authority_id = confirmed.json()["authority"]["authority_id"]

        with client.app.state.db.session_scope() as session:
            application = session.get(Application, application_id)
            assert application is not None and application.selected_cv_id
            document = session.get(Document, application.selected_cv_id)
            assert document is not None
            if mutation == "selected_id":
                replacement = DocumentService(
                    session,
                    settings.documents_dir,
                ).store_bytes(
                    filename="replacement-cv.pdf",
                    content=b"replacement-reviewed-cv",
                    kind="document.cv",
                    approved=True,
                    actor="test",
                )
                application.selected_cv_id = replacement.id
            elif mutation == "stored_hash":
                document.sha256 = "f" * 64
            elif mutation == "approval":
                document.approved = False
            elif mutation == "file_bytes":
                Path(document.path).write_bytes(b"changed-after-authority")
            elif mutation == "file_missing":
                Path(document.path).unlink()

        refused = client.post(
            f"/api/applications/{application_id}/run",
            params={"mode": "submit"},
            json={
                "application_id": application_id,
                "session_id": review_session_id,
                "authority_id": authority_id,
            },
        )
        assert refused.status_code == 409, refused.text

        with client.app.state.db.session_scope() as session:
            authority = session.get(SubmissionAuthority, authority_id)
            assert authority is not None
            assert authority.consumed_at is None
            assert list(
                session.scalars(
                    select(SubmissionIntent).where(
                        SubmissionIntent.application_id == application_id
                    )
                ).all()
            ) == []

    assert httpx.get(f"{live_server.base_url}/api/lab/submissions").json() == []
