"""Owner-thread lifecycle tests for the application navigator."""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.automation.runner import AutomationRunner, SubmissionBlocked, _OwnerThreadJourney
from app.automation.types import (
    RunMode,
    SessionCommand,
    SessionCommandType,
    SessionEventType,
    SessionState,
)
from app.automation.host_policy import origin_for_url
from app.automation.targets import SubmissionTarget, TargetResolution
from app.domain.targets import TargetKind
from app.models import Application, Opportunity
from app.services.navigator import DuplicateSessionError


def _worker_for_command_guard(*, deadline_monotonic: float) -> tuple[object, list[int]]:
    """Build an owner-bound worker without starting Playwright."""

    from app.services.navigator import HeadedSessionWorker

    calls: list[int] = []

    class Journey:
        def submit(self, _page, _confirmation_id):
            calls.append(threading.get_ident())
            return {"state": "CONFIRMED"}

    worker = HeadedSessionWorker(
        session_id="session-guard",
        application_id="application-guard",
        mode=RunMode.SUBMIT.value,
        url="http://127.0.0.1:8787/application",
        summary={},
        command_queue=__import__("queue").Queue(),
        event_queue=__import__("queue").Queue(),
        ttl_seconds=30,
        deadline_monotonic=deadline_monotonic,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        headless=True,
        journey_executor=Journey(),
        resumable_on_expiry=True,
    )
    worker.owner_thread_id = threading.get_ident()
    worker._page = SimpleNamespace(url="http://127.0.0.1:8787/application")
    worker._state = SessionState.FINAL_REVIEW
    worker._manifest = {
        "application_id": "application-guard",
        "employer": "Example Employer",
        "role": "R",
        "provider": "greenhouse",
        "requisition": "/application",
        "target_fingerprint": "target-1",
        "control_fingerprint": "control-1",
        "form_action": "http://127.0.0.1:8787/application/submit",
        "method": "POST",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "final_url": "http://127.0.0.1:8787/application",
        "expected_receipt_url": "http://127.0.0.1:8787/application/receipt",
    }
    return worker, calls


def _verified_resolution(url: str = "http://127.0.0.1:8787/application") -> TargetResolution:
    return TargetResolution(
        source_url=url,
        final_url=url,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={
            "synthetic_lab": True,
            "provider": "greenhouse",
            "application_origin": origin_for_url(url),
            "employer": "Example Employer",
            "role": "R",
            "requisition": "/application",
            "form_identity": "application",
        },
    )


def _stored_resolution_envelope(
    *,
    source_url: str = "http://127.0.0.1:8787/source",
    final_url: str = "http://127.0.0.1:8787/application",
    kind: str = TargetKind.APPLICATION_ENTRY.value,
    provider: str = "greenhouse",
    identity_verified: bool = True,
    form_verified: bool = False,
) -> str:
    return json.dumps(
        {
            "source_url": source_url,
            "final_url": final_url,
            "kind": kind,
            "provider": provider,
            "identity_verified": identity_verified,
            "form_verified": form_verified,
            "reason_codes": ["test_verified_target"],
            "evidence": {
                "synthetic_lab": True,
                "provider": provider,
                "application_origin": origin_for_url(final_url),
                "employer": "Example Employer",
                "role": "R",
                "requisition": "/application",
                "form_identity": "application",
            },
        }
    )


class _TargetSession:
    def __init__(self, application: Application, opportunity: Opportunity) -> None:
        self.application = application
        self.opportunity = opportunity

    def get(self, model, identifier):
        if model is Application and identifier == self.application.id:
            return self.application
        if model is Opportunity and identifier == self.opportunity.id:
            return self.opportunity
        return None


class _TargetDatabase:
    def __init__(self, application: Application, opportunity: Opportunity) -> None:
        self.session = _TargetSession(application, opportunity)

    @contextmanager
    def session_scope(self):
        yield self.session


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", TargetKind.NON_HTML.value),
        ("kind", TargetKind.MISMATCH.value),
        ("kind", TargetKind.LISTING.value),
        ("final_url", "http://127.0.0.1:8787/attacker"),
        ("provider", "lever"),
        ("identity_verified", False),
        ("form_verified", True),
    ],
)
def test_db_envelope_tampering_refuses_before_worker_creation(field, value):
    envelope = json.loads(_stored_resolution_envelope())
    envelope[field] = value
    opportunity = Opportunity(
        id="opp-envelope-tamper",
        employer="Example Employer",
        role_title="R",
        programme_group="summer",
        cycle="2027",
        url="http://127.0.0.1:8787/source",
        application_url="http://127.0.0.1:8787/application",
        target_status=TargetKind.APPLICATION_ENTRY.value,
        resolved_ats_type="greenhouse",
        resolved_at=datetime.now(timezone.utc),
        resolution_evidence_json=json.dumps(envelope),
    )
    application = Application(
        id="application-envelope-tamper",
        opportunity_id=opportunity.id,
        state="NEEDS_USER",
    )
    created: list[object] = []

    def worker_factory(**kwargs):
        created.append(kwargs)
        raise AssertionError("tampered target must not create a worker")

    from app.services.navigator import ApplicationNavigator

    navigator = ApplicationNavigator(
        _TargetDatabase(application, opportunity),
        worker_factory=worker_factory,
        headless=True,
    )
    with pytest.raises(ValueError, match="resolution|verified|envelope"):
        navigator.start(application.id, RunMode.REVIEW)
    assert created == []

    with pytest.raises(SubmissionBlocked, match="resolution|verified|evidence"):
        AutomationRunner._resolution_from_opportunity(opportunity)


def test_missing_serialized_target_envelope_refuses_before_worker_creation():
    opportunity = Opportunity(
        id="opp-envelope-missing",
        employer="Example Employer",
        role_title="R",
        programme_group="summer",
        cycle="2027",
        url="http://127.0.0.1:8787/source",
        application_url="http://127.0.0.1:8787/application",
        target_status=TargetKind.APPLICATION_ENTRY.value,
        resolved_ats_type="greenhouse",
        resolved_at=datetime.now(timezone.utc),
        resolution_evidence_json=json.dumps({"evidence": {"synthetic_lab": True}}),
    )
    application = Application(
        id="application-envelope-missing",
        opportunity_id=opportunity.id,
        state="NEEDS_USER",
    )
    created: list[object] = []
    from app.services.navigator import ApplicationNavigator

    navigator = ApplicationNavigator(
        _TargetDatabase(application, opportunity),
        worker_factory=lambda **kwargs: created.append(kwargs),
        headless=True,
    )
    with pytest.raises(ValueError, match="resolution|verified|envelope"):
        navigator.start(application.id, RunMode.REVIEW)
    with pytest.raises(SubmissionBlocked, match="resolution|verified|evidence"):
        AutomationRunner._resolution_from_opportunity(opportunity)
    assert created == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "lever"),
        ("ats", "lever"),
        ("employer", "Other Employer"),
        ("role", "Other Role"),
        ("requisition", "/other-application"),
        ("form_identity", "other-form"),
        ("application_origin", "http://127.0.0.1:8788"),
    ],
)
def test_nested_resolution_evidence_contradiction_refuses_before_worker(field, value, monkeypatch):
    envelope = json.loads(_stored_resolution_envelope())
    envelope["evidence"][field] = value
    opportunity = Opportunity(
        id=f"opp-nested-{field}",
        employer="Example Employer",
        role_title="R",
        programme_group="summer",
        cycle="2027",
        url="http://127.0.0.1:8787/source",
        application_url="http://127.0.0.1:8787/application",
        target_status=TargetKind.APPLICATION_ENTRY.value,
        resolved_ats_type="greenhouse",
        resolved_at=datetime.now(timezone.utc),
        resolution_evidence_json=json.dumps(envelope),
    )
    application = Application(
        id=f"application-nested-{field}",
        opportunity_id=opportunity.id,
        state="NEEDS_USER",
    )
    created: list[object] = []
    from app.services.navigator import ApplicationNavigator

    navigator = ApplicationNavigator(
        _TargetDatabase(application, opportunity),
        worker_factory=lambda **kwargs: created.append(kwargs),
        headless=True,
    )
    with pytest.raises(ValueError, match="evidence|identity|contract|envelope|path"):
        navigator.start(application.id, RunMode.REVIEW)
    with pytest.raises(SubmissionBlocked, match="evidence|identity|contract|envelope|path"):
        AutomationRunner._resolution_from_opportunity(opportunity)
    runs: list[object] = []
    monkeypatch.setattr(
        "app.automation.runner.AutomationRun",
        lambda *args, **kwargs: runs.append((args, kwargs)),
    )
    runner = object.__new__(AutomationRunner)
    with pytest.raises(SubmissionBlocked, match="evidence|identity|contract|envelope|path"):
        runner._run_claimed_owner(
            _TargetSession(application, opportunity),
            application,
            application.id,
            RunMode.REVIEW,
            False,
        )
    assert runs == []
    assert created == []


def test_nested_resolution_evidence_canonical_aliases_are_accepted():
    envelope = json.loads(_stored_resolution_envelope())
    envelope["evidence"].update(
        {
            "provider": " GREENHOUSE ",
            "ats": "greenhouse",
            "employer": "Example, Employer",
            "role": " r ",
            "requisition": "/APPLICATION/",
            "form_identity": " APPLICATION ",
            "application_origin": "HTTP://127.0.0.1:8787/",
        }
    )
    opportunity = Opportunity(
        id="opp-nested-canonical",
        employer="Example Employer",
        role_title="R",
        programme_group="summer",
        cycle="2027",
        url="http://127.0.0.1:8787/source",
        application_url="http://127.0.0.1:8787/application",
        target_status=TargetKind.APPLICATION_ENTRY.value,
        resolved_ats_type="greenhouse",
        resolved_at=datetime.now(timezone.utc),
        resolution_evidence_json=json.dumps(envelope),
    )
    application = Application(
        id="application-nested-canonical",
        opportunity_id=opportunity.id,
        state="NEEDS_USER",
    )
    from app.services.navigator import ApplicationNavigator

    navigator = ApplicationNavigator(
        _TargetDatabase(application, opportunity),
        worker_factory=lambda **kwargs: pytest.fail("consistent target must resolve before worker use"),
        headless=True,
    )
    resolution, _summary = navigator._resolve_target(application.id)
    runner_resolution = AutomationRunner._resolution_from_opportunity(opportunity)
    assert resolution == runner_resolution
    assert resolution.provider == "greenhouse"
    assert resolution.evidence["employer"] == "Example, Employer"


@pytest.mark.parametrize(
    ("label", "evidence_update"),
    [
        (
            "provider-verified",
            {"provider": "greenhouse", "provider_verified": "lever"},
        ),
        ("ats-verified", {"ats": "greenhouse", "ats_verified": "lever"}),
        (
            "employer-verified",
            {"employer": "Example Employer", "employer_verified": "Other"},
        ),
        ("role-verified", {"role": "R", "role_verified": "Other Role"}),
        (
            "requisition-path",
            {"requisition": "/application", "target_path": "/other"},
        ),
        (
            "application-root",
            {"application_root": "#application", "root_selector": "#other"},
        ),
        (
            "form-selector",
            {"form_selector": "#application", "root_selector": "#other"},
        ),
        (
            "frame-url",
            {
                "frame_url": "http://127.0.0.1:8787/frame-a",
                "bound_frame_url": "http://127.0.0.1:8787/frame-b",
            },
        ),
        (
            "bound-target-url",
            {
                "form": {
                    "control_count": 1,
                    "submit_present": True,
                    "root_selector": "#application",
                    "root_token": "application",
                    "binding_verified": True,
                    "bound_provider": "greenhouse",
                    "bound_target_url": "http://127.0.0.1:8787/application",
                    "bound-target-url": "http://127.0.0.1:8787/other",
                }
            },
        ),
    ],
)
def test_normalized_duplicate_alias_conflicts_refuse_before_worker_and_run(
    label, evidence_update, monkeypatch
):
    envelope = json.loads(_stored_resolution_envelope())
    envelope["evidence"].update(evidence_update)
    opportunity = Opportunity(
        id=f"opp-alias-conflict-{label}",
        employer="Example Employer",
        role_title="R",
        programme_group="summer",
        cycle="2027",
        url="http://127.0.0.1:8787/source",
        application_url="http://127.0.0.1:8787/application",
        target_status=TargetKind.APPLICATION_ENTRY.value,
        resolved_ats_type="greenhouse",
        resolved_at=datetime.now(timezone.utc),
        resolution_evidence_json=json.dumps(envelope),
    )
    application = Application(
        id=f"application-alias-conflict-{label}",
        opportunity_id=opportunity.id,
        state="NEEDS_USER",
    )
    created: list[object] = []
    runs: list[object] = []
    from app.services.navigator import ApplicationNavigator

    monkeypatch.setattr(
        "app.automation.runner.AutomationRun",
        lambda *args, **kwargs: runs.append((args, kwargs)),
    )
    navigator = ApplicationNavigator(
        _TargetDatabase(application, opportunity),
        worker_factory=lambda **kwargs: created.append(kwargs),
        headless=True,
    )
    with pytest.raises(ValueError, match="evidence|identity|contract|envelope|binding|aliases"):
        navigator.start(application.id, RunMode.REVIEW)
    runner = object.__new__(AutomationRunner)
    with pytest.raises(SubmissionBlocked, match="evidence|identity|contract|envelope|binding|aliases"):
        runner._run_claimed_owner(
            _TargetSession(application, opportunity),
            application,
            application.id,
            RunMode.REVIEW,
            False,
        )
    assert created == []
    assert runs == []


def test_same_origin_frame_and_equivalent_bound_target_urls_are_accepted():
    final_url = "http://127.0.0.1:80/application/"
    envelope = json.loads(
        _stored_resolution_envelope(final_url=final_url, source_url="http://127.0.0.1:80/source")
    )
    envelope["evidence"].update(
        {
            "provider_verified": " GREENHOUSE ",
            "ats_verified": "greenhouse",
            "employer_verified": "Example Employer",
            "role_verified": "R",
            "requisition": "/application/",
            "application_root": "#application",
            "form_selector": "#application",
            "root_selector": "#application",
            "frame_url": "http://127.0.0.1:80/embed/application/",
            "bound_frame_url": "http://127.0.0.1/embed/application/",
            "form": {
                "control_count": 1,
                "submit_present": True,
                "root_selector": "#application",
                "root_token": "application",
                "form_identity": "application",
                "binding_verified": True,
                "bound_provider": "GREENHOUSE",
                "bound_target_url": "http://127.0.0.1:80/application/",
                "bound-target-url": "http://127.0.0.1/application",
                "bound_requisition": "/application/",
            },
        }
    )
    opportunity = Opportunity(
        id="opp-alias-canonical",
        employer="Example Employer",
        role_title="R",
        programme_group="summer",
        cycle="2027",
        url="http://127.0.0.1:80/source",
        application_url=final_url,
        target_status=TargetKind.APPLICATION_ENTRY.value,
        resolved_ats_type="greenhouse",
        resolved_at=datetime.now(timezone.utc),
        resolution_evidence_json=json.dumps(envelope),
    )
    application = Application(
        id="application-alias-canonical",
        opportunity_id=opportunity.id,
        state="NEEDS_USER",
    )
    from app.services.navigator import ApplicationNavigator

    navigator = ApplicationNavigator(
        _TargetDatabase(application, opportunity),
        worker_factory=lambda **kwargs: pytest.fail("canonical target must resolve"),
        headless=True,
    )
    resolution, _summary = navigator._resolve_target(application.id)
    assert resolution.final_url == final_url


def test_dispatch_then_typeerror_is_unknown_without_a_second_click(monkeypatch):
    action_url = "http://127.0.0.1:8787/application/submit"
    target = SubmissionTarget(
        page_id="page-1",
        frame_url="http://127.0.0.1:8787/application",
        root_selector="#application",
        provider="greenhouse",
        control_selector="#submit",
        control_fingerprint="submit-1",
        form_action=action_url,
        method="POST",
        provider_step="final",
        employer="Example Employer",
        role="R",
        requisition="/application",
        destination=action_url,
        evidence={"expected_final_url": action_url},
    )

    class Body:
        def inner_text(self, **_kwargs):
            return ""

    class ResponseRequest:
        url = action_url
        method = "POST"

    class Response:
        url = action_url
        status = 200
        request = ResponseRequest()

    class ExpectResponse:
        def __init__(self):
            self.value = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.value = Response()
            return False

    class Control:
        def __init__(self):
            self.calls = 0
            self.post_count = 0

        def click(self, **_kwargs):
            self.calls += 1
            self.post_count += 1
            raise TypeError("synthetic unsupported click option after dispatch")

    class Page:
        url = "http://127.0.0.1:8787/application"

        def __init__(self):
            self.control = Control()

        def expect_response(self, *_args, **_kwargs):
            return ExpectResponse()

        def locator(self, selector):
            if selector == "body":
                return Body()
            assert selector == "#submit"
            return self.control

        def on(self, *_args):
            return None

        def remove_listener(self, *_args):
            return None

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

    class Adapter:
        name = "greenhouse"

        @staticmethod
        def current_scope(page):
            return page

        @staticmethod
        def detect_human_boundary(_page):
            return False, ""

        @staticmethod
        def submission_target(_scope, _handle):
            return target

    page = Page()
    journey = object.__new__(_OwnerThreadJourney)
    journey.runner = SimpleNamespace(settings=SimpleNamespace())
    journey.application_id = "application-1"
    journey.adapter = Adapter()
    journey.adapter_name = "greenhouse"
    journey.final_handle = object()
    journey.final_target = target
    journey.final_scope = page
    journey.submission_binding = {"id": "intent-1", "nonce": "nonce-1"}
    journey.last_receipt_evidence = {}
    journey._click_boundary_crossed = False
    boundary_callbacks = []
    journey.before_click = lambda: boundary_callbacks.append("CLICKED")

    monkeypatch.setattr("app.automation.runner.assert_submission_allowed", lambda *_args: None)
    result = journey.submit(page, "application-1")

    assert result["state"] == "UNKNOWN"
    assert result["click_boundary_crossed"] is True
    assert page.control.calls == 1
    assert page.control.post_count == 1
    assert boundary_callbacks == ["CLICKED"]
    assert result["receipt_evidence"]["bound_intent"] == {
        "id": "intent-1",
        "nonce": "nonce-1",
    }
    assert result["receipt_evidence"]["expected_final_url"] == action_url


@pytest.fixture()
def navigator(monkeypatch):
    from app.services.navigator import ApplicationNavigator

    class FakePage:
        def __init__(self, log):
            self.log = log
            self.closed = False
            self.url = "about:blank"
            self.fail_evaluate = False
            self.fail_close = False
            self.frames = []

        def route(self, *args):
            self.log.append(("page.route", threading.get_ident()))

        def goto(self, url, **kwargs):
            self.log.append(("page.goto", threading.get_ident()))
            self.url = url

        def wait_for_timeout(self, _ms):
            self.log.append(("page.wait_for_timeout", threading.get_ident()))

        def evaluate(self, _script, *args):
            self.log.append(("page.evaluate", threading.get_ident()))
            if self.fail_evaluate:
                raise RuntimeError("synthetic evaluate failure")
            return {"captcha": False, "reason": ""}

        def close(self):
            self.log.append(("page.close", threading.get_ident()))
            if self.fail_close:
                raise RuntimeError("synthetic page close failure")
            self.closed = True

    class FakeContext:
        def __init__(self, log):
            self.log = log
            self.pages = []
            self.closed = False
            self._page_callbacks = []

        def route(self, *args):
            self.log.append(("context.route", threading.get_ident()))

        def on(self, event, callback):
            self.log.append((f"context.on:{event}", threading.get_ident()))
            if event == "page":
                self._page_callbacks.append(callback)

        def new_page(self):
            self.log.append(("context.new_page", threading.get_ident()))
            page = FakePage(self.log)
            self.pages.append(page)
            for callback in self._page_callbacks:
                callback(page)
            return page

        def close(self):
            self.log.append(("context.close", threading.get_ident()))
            self.closed = True

    class FakeBrowser:
        def __init__(self, log):
            self.log = log
            self.closed = False
            self.disconnected = False
            self.context = FakeContext(log)

        def new_context(self, **kwargs):
            self.log.append(("browser.new_context", threading.get_ident()))
            return self.context

        def close(self):
            self.log.append(("browser.close", threading.get_ident()))
            self.closed = True
            self.disconnected = True

    class FakeChromium:
        def __init__(self, log):
            self.log = log
            self.browser = None
            self.launch_options = {}

        def launch(self, **kwargs):
            self.launch_options = dict(kwargs)
            self.log.append(("chromium.launch", threading.get_ident()))
            self.browser = FakeBrowser(self.log)
            return self.browser

    class FakePlaywright:
        def __init__(self, log):
            self.log = log
            self.chromium = FakeChromium(log)
            self.stopped = False

        def stop(self):
            self.log.append(("playwright.stop", threading.get_ident()))
            self.stopped = True

    logs = []
    runtime = FakePlaywright(logs)
    monkeypatch.setattr(
        "app.services.navigator.sync_playwright",
        lambda: type("Factory", (), {"start": lambda _self: runtime})(),
    )
    nav = ApplicationNavigator(
        ttl_seconds=0.15,
        url_resolver=lambda _application_id: (_verified_resolution(), {"role": "R"}),
        headless=True,
    )
    nav._test_log = logs
    nav._test_runtime = runtime
    yield nav
    try:
        nav.shutdown()
    except Exception:
        pass


def test_start_returns_immediately_and_playwright_is_owner_thread_only(navigator):
    caller_thread = threading.get_ident()
    started = time.monotonic()
    session = navigator.start("app-1", RunMode.REVIEW)
    elapsed = time.monotonic() - started

    assert elapsed < 1
    assert session.session_id
    assert session.state is SessionState.OPENING
    navigator.wait_for_state(session.session_id, SessionState.ACTIVE, timeout=2)
    diagnostics = navigator.diagnostics(session.session_id)
    assert diagnostics.owner_thread_id != caller_thread
    assert navigator._test_log
    # REVIEW is a human-bound handoff: the worker must visibly launch the
    # browser even when the automated runner's default is headless.
    assert navigator._test_runtime.chromium.launch_options["headless"] is False
    assert all(
        thread_id == diagnostics.owner_thread_id
        for _name, thread_id in navigator._test_log
    )


def test_worker_refuses_mismatched_submission_command_without_callback():
    from app.services.navigator import HeadedSessionWorker

    worker, calls = _worker_for_command_guard(
        deadline_monotonic=time.monotonic() + 30,
    )
    worker._handle_command(
        SessionCommand(
            command=SessionCommandType.CONFIRM_SUBMISSION,
            session_id="different-session",
            payload={"confirmation_id": "application-guard"},
        )
    )

    assert calls == []
    assert worker._state is SessionState.FINAL_REVIEW
    event = worker.event_queue.get_nowait()
    assert event.event is SessionEventType.REQUEST
    assert event.payload["command_refused"] == "session_id_mismatch"


def test_worker_refuses_expired_submission_command_before_callback():
    worker, calls = _worker_for_command_guard(
        deadline_monotonic=time.monotonic() - 1,
    )
    worker._handle_command(
        SessionCommand(
            command=SessionCommandType.CONFIRM_SUBMISSION,
            session_id="session-guard",
            payload={"confirmation_id": "application-guard"},
        )
    )

    assert calls == []
    assert worker._state is SessionState.EXPIRED
    assert worker._stop_requested is False
    events = []
    while not worker.event_queue.empty():
        events.append(worker.event_queue.get_nowait())
    assert any(
        event.event is SessionEventType.REQUEST
        and event.payload.get("command_refused") == "deadline_expired"
        for event in events
    )


def test_worker_confirm_refuses_incomplete_manifest_without_advancing():
    worker, calls = _worker_for_command_guard(
        deadline_monotonic=time.monotonic() + 30,
    )
    worker._manifest.pop("expected_receipt_url")
    worker._handle_command(
        SessionCommand(
            command=SessionCommandType.CONFIRM,
            session_id="session-guard",
            payload={"confirmed": True},
        )
    )

    assert calls == []
    assert worker._state is SessionState.ACTIVE
    assert worker._stop_requested is False


def test_worker_submission_refuses_manifest_after_page_navigation():
    worker, calls = _worker_for_command_guard(
        deadline_monotonic=time.monotonic() + 30,
    )
    worker._page.url = "http://127.0.0.1:8787/application/changed"
    worker._handle_command(
        SessionCommand(
            command=SessionCommandType.CONFIRM_SUBMISSION,
            session_id="session-guard",
            payload={"confirmation_id": "application-guard"},
        )
    )

    assert calls == []
    assert worker._state is SessionState.ACTIVE
    assert "stale" in worker.event_queue.get_nowait().reason.lower()


def test_duplicate_session_refused_and_cancel_is_idempotent(navigator):
    first = navigator.start("app-2", RunMode.REVIEW)
    navigator.wait_for_state(first.session_id, SessionState.ACTIVE, timeout=2)

    with pytest.raises(Exception, match="active session"):
        navigator.start("app-2", RunMode.REVIEW)

    navigator.cancel(first.session_id, reason="test")
    navigator.cancel(first.session_id, reason="repeat")
    final = navigator.wait_for_terminal(first.session_id, timeout=2)
    assert final.state is SessionState.CANCELLED
    assert not navigator.get(first.session_id).worker_alive


def test_default_handoff_ttl_is_twenty_four_hours() -> None:
    from app.services.navigator import ApplicationNavigator

    nav = ApplicationNavigator()
    try:
        assert nav.ttl_seconds == 86_400
    finally:
        nav.shutdown()


def test_ttl_expires_authority_but_preserves_and_resumes_same_page(navigator):
    session = navigator.start("app-3", RunMode.REVIEW)
    active = navigator.wait_for_state(session.session_id, SessionState.ACTIVE, timeout=2)
    page_token = navigator.diagnostics(session.session_id).page_token
    expired = navigator.wait_for_state(session.session_id, SessionState.EXPIRED, timeout=3)
    assert expired.state is SessionState.EXPIRED
    assert expired.resumable is True
    assert expired.worker_alive is True
    assert expired.cleanup_complete is False
    diagnostics = navigator.diagnostics(session.session_id)
    assert diagnostics.worker_alive
    assert diagnostics.page_token == page_token
    owner = diagnostics.owner_thread_id
    assert owner
    assert all(thread_id == owner for _name, thread_id in navigator._test_log)

    resumed = navigator.resume(session.session_id)
    assert resumed.state is SessionState.ACTIVE
    assert resumed.expires_at > active.expires_at
    assert navigator.diagnostics(session.session_id).page_token == page_token

    navigator.cancel(session.session_id, reason="test teardown")
    cleaned = navigator.wait_for_cleanup(session.session_id, timeout=2)
    assert cleaned.cleanup_complete is True
    assert cleaned.worker_alive is False
    navigator.shutdown()
    navigator.shutdown()


def test_continue_rescans_without_replacing_browser_context(navigator):
    session = navigator.start("app-4", RunMode.REVIEW)
    navigator.wait_for_state(session.session_id, SessionState.ACTIVE, timeout=2)
    first_page = navigator.diagnostics(session.session_id).page_token
    navigator.continue_after_human(session.session_id)
    navigator.wait_for_state(session.session_id, SessionState.ACTIVE, timeout=2)
    assert navigator.diagnostics(session.session_id).page_token == first_page


def test_injected_captcha_command_stops_progress_without_external_traffic(navigator):
    session = navigator.start("app-5", RunMode.REVIEW)
    navigator.wait_for_state(session.session_id, SessionState.ACTIVE, timeout=2)
    injected = navigator.inject_human_required(session.session_id)
    required = navigator.wait_for_state(
        session.session_id,
        SessionState.HUMAN_REQUIRED,
        timeout=2,
    )
    assert injected.session_id == required.session_id
    assert required.reason == "captcha_injected"
    navigator.continue_after_human(session.session_id)
    assert navigator.wait_for_state(
        session.session_id,
        SessionState.ACTIVE,
        timeout=2,
    ).state is SessionState.ACTIVE


def test_continue_scan_failure_stays_failed_and_never_returns_active(navigator):
    session = navigator.start("app-6", RunMode.REVIEW)
    navigator.wait_for_state(session.session_id, SessionState.ACTIVE, timeout=2)
    navigator.inject_human_required(session.session_id)
    navigator.wait_for_state(session.session_id, SessionState.HUMAN_REQUIRED, timeout=2)
    fake_page = navigator._test_runtime.chromium.browser.context.pages[0]
    fake_page.fail_evaluate = True
    navigator.continue_after_human(session.session_id)
    failed = navigator.wait_for_terminal(session.session_id, timeout=2)
    assert failed.state is SessionState.FAILED
    assert "evaluate" in failed.reason.lower()


def test_teardown_failure_is_failed_and_not_cleanup_complete(navigator):
    session = navigator.start("app-7", RunMode.REVIEW)
    navigator.wait_for_state(session.session_id, SessionState.ACTIVE, timeout=2)
    fake_page = navigator._test_runtime.chromium.browser.context.pages[0]
    fake_page.fail_close = True
    navigator.cancel(session.session_id, reason="synthetic teardown failure")
    failed = navigator.wait_for_terminal(session.session_id, timeout=2)
    assert failed.state is SessionState.FAILED
    assert failed.cleanup_complete is False
    diagnostics = navigator.diagnostics(session.session_id)
    assert diagnostics.teardown_failures
    assert "page.close" in diagnostics.teardown_failures[0]
    with pytest.raises(Exception, match="active session"):
        navigator.start("app-7", RunMode.REVIEW)

    fake_page.fail_close = False
    recovered = navigator.retry_cleanup(session.session_id)
    assert recovered.state is SessionState.FAILED
    complete = navigator.wait_for_cleanup(session.session_id, timeout=2)
    assert complete.cleanup_complete is True
    replacement = navigator.start("app-7", RunMode.REVIEW)
    assert replacement.session_id != session.session_id
    navigator.cancel(replacement.session_id, reason="recovery complete")


def test_persistent_teardown_failure_is_bounded_and_blocks_replacement(navigator):
    """Escalated teardown is bounded but never counts as successful cleanup."""

    navigator.cleanup_grace_seconds = 0.2
    session = navigator.start("app-persistent-teardown", RunMode.REVIEW)
    navigator.wait_for_state(session.session_id, SessionState.ACTIVE, timeout=2)
    fake_page = navigator._test_runtime.chromium.browser.context.pages[0]
    fake_page.fail_close = True
    worker = navigator._sessions[session.session_id].worker
    assert worker is not None

    try:
        navigator.cancel(session.session_id, reason="persistent synthetic teardown failure")
        worker.join(0.6)
        assert not worker.is_alive()
        assert worker.cleanup_escalated is True
        failed = navigator.get(session.session_id)
        assert failed.state is SessionState.FAILED
        assert failed.cleanup_complete is False

        with pytest.raises(DuplicateSessionError, match="incomplete teardown"):
            navigator.start("app-persistent-teardown", RunMode.REVIEW)
    finally:
        fake_page.fail_close = False
        if worker.is_alive():
            navigator.retry_cleanup(session.session_id)
            worker.join(2)


def test_session_listing_reaps_terminal_records_after_retention(navigator):
    navigator.terminal_session_retention_seconds = 0
    session = navigator.start("app-reap", RunMode.REVIEW)
    navigator.wait_for_state(session.session_id, SessionState.ACTIVE, timeout=2)
    navigator.cancel(session.session_id, reason="reaper regression")
    navigator.wait_for_terminal(session.session_id, timeout=2)

    listed = navigator.all_sessions()

    assert all(item.session_id != session.session_id for item in listed)


@pytest.mark.parametrize("malformed_url", ["https:///missing-host", "http:///missing-host", "https://["])
def test_http_url_without_valid_hostname_is_rejected_by_egress_policy(
    navigator, malformed_url
):
    with pytest.raises(ValueError, match="raw url"):
        navigator.start("app-malformed-url", RunMode.REVIEW, url=malformed_url)


def test_shutdown_timeout_raises_incomplete_teardown(navigator):
    from app.services.navigator import NavigatorShutdownError

    session = navigator.start("app-8", RunMode.REVIEW)
    navigator.wait_for_state(session.session_id, SessionState.ACTIVE, timeout=2)
    worker = navigator._sessions[session.session_id].worker
    original_join = worker.join
    worker.join = lambda _timeout=None: None
    try:
        with pytest.raises(NavigatorShutdownError, match="incomplete"):
            navigator.shutdown(timeout=0.01)
    finally:
        worker.join = original_join
        navigator.shutdown(timeout=3)


def test_public_navigator_does_not_expose_playwright_handles(navigator):
    from app.services.navigator import HeadedSessionWorker

    assert not hasattr(navigator, "worker")
    assert not hasattr(HeadedSessionWorker, "page")
    assert not hasattr(HeadedSessionWorker, "context")
    assert not hasattr(HeadedSessionWorker, "browser")
    assert not hasattr(HeadedSessionWorker, "runtime")
