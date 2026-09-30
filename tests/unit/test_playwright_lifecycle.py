"""Playwright driver-lifecycle tests for the application navigator.

Every sync Playwright instance is created in exactly one place
(``HeadedSessionWorker._open_browser``) and must be stopped in exactly one
place (``HeadedSessionWorker._cleanup``).  Closing a browser does NOT kill
``driver/node.exe``; only stopping the Playwright instance does.  These
tests use fakes only and never launch a real browser.
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.automation.types import (
    RunMode,
    SessionCommandType,
    SessionEventType,
    SessionState,
)
from app.automation.host_policy import origin_for_url
from app.automation.targets import TargetResolution
from app.domain.targets import TargetKind


class _FakePage:
    def __init__(self, log: list) -> None:
        self.log = log
        self.url = "about:blank"
        self.closed = False
        self.fail_close = False
        self.frames: list = []

    def goto(self, url: str, **kwargs) -> None:
        self.log.append(("page.goto", threading.get_ident()))
        self.url = url

    def wait_for_timeout(self, _ms: int) -> None:
        self.log.append(("page.wait_for_timeout", threading.get_ident()))

    def evaluate(self, _script: str, *args) -> dict:
        self.log.append(("page.evaluate", threading.get_ident()))
        return {"captcha": False, "reason": ""}

    def title(self) -> str:
        return ""

    def close(self) -> None:
        self.log.append(("page.close", threading.get_ident()))
        if self.fail_close:
            raise RuntimeError("synthetic page close failure")
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


class _FakeContext:
    def __init__(self, log: list) -> None:
        self.log = log
        self.pages: list[_FakePage] = []
        self.closed = False
        self._page_callbacks: list = []

    def route(self, *args) -> None:
        self.log.append(("context.route", threading.get_ident()))

    def on(self, event: str, callback) -> None:
        self.log.append((f"context.on:{event}", threading.get_ident()))
        if event == "page":
            self._page_callbacks.append(callback)

    def new_page(self) -> _FakePage:
        self.log.append(("context.new_page", threading.get_ident()))
        page = _FakePage(self.log)
        self.pages.append(page)
        for callback in self._page_callbacks:
            callback(page)
        return page

    def close(self) -> None:
        self.log.append(("context.close", threading.get_ident()))
        self.closed = True


class _FakeBrowser:
    def __init__(self, log: list) -> None:
        self.log = log
        self.closed = False
        self.context = _FakeContext(log)

    def new_context(self, **kwargs) -> _FakeContext:
        self.log.append(("browser.new_context", threading.get_ident()))
        return self.context

    def close(self) -> None:
        self.log.append(("browser.close", threading.get_ident()))
        self.closed = True


class _FakeChromium:
    def __init__(self, log: list, *, fail_launch: bool = False) -> None:
        self.log = log
        self.fail_launch = fail_launch
        self.browser: _FakeBrowser | None = None

    def launch(self, **kwargs) -> _FakeBrowser:
        self.log.append(("chromium.launch", threading.get_ident()))
        if self.fail_launch:
            raise RuntimeError("synthetic chromium launch failure")
        self.browser = _FakeBrowser(self.log)
        return self.browser


class _FakePlaywright:
    """Counting fake for one Playwright runtime instance."""

    def __init__(self, log: list, *, fail_launch: bool = False) -> None:
        self.log = log
        self.chromium = _FakeChromium(log, fail_launch=fail_launch)
        self.stop_count = 0
        self.stopped = False

    def stop(self) -> None:
        self.log.append(("playwright.stop", threading.get_ident()))
        self.stop_count += 1
        self.stopped = True


def _playwright_factory(runtime: _FakePlaywright):
    def factory():
        return SimpleNamespace(start=lambda: runtime)

    return factory


def _verified_resolution(
    url: str = "http://127.0.0.1:8787/application",
) -> TargetResolution:
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


@pytest.fixture()
def instrumented_navigator(monkeypatch):
    """Real workers against one counting fake Playwright runtime."""
    from app.services.navigator import ApplicationNavigator

    logs: list = []
    runtime = _FakePlaywright(logs)
    monkeypatch.setattr(
        "app.services.navigator.sync_playwright",
        _playwright_factory(runtime),
    )
    navigator = ApplicationNavigator(
        ttl_seconds=30,
        url_resolver=lambda _application_id: (_verified_resolution(), {"role": "R"}),
        headless=True,
    )
    yield navigator, runtime, logs
    try:
        navigator.shutdown()
    except Exception:
        pass


def test_playwright_stopped_after_normal_operation(instrumented_navigator) -> None:
    navigator, runtime, logs = instrumented_navigator

    session = navigator.start("app-lifecycle-1", RunMode.REVIEW)
    navigator.wait_for_state(session.session_id, SessionState.ACTIVE, timeout=5)
    navigator.close(session.session_id, reason="lifecycle test teardown")
    cleaned = navigator.wait_for_cleanup(session.session_id, timeout=5)

    assert cleaned.cleanup_complete is True
    assert cleaned.worker_alive is False
    assert runtime.stopped is True
    assert runtime.stop_count == 1
    assert ("playwright.stop", cleaned.owner_thread_id) in logs


def test_playwright_stopped_when_browser_launch_raises(monkeypatch) -> None:
    """The critical case: _open_browser fails AFTER runtime.start()."""
    from app.services.navigator import ApplicationNavigator

    logs: list = []
    runtime = _FakePlaywright(logs, fail_launch=True)
    monkeypatch.setattr(
        "app.services.navigator.sync_playwright",
        _playwright_factory(runtime),
    )
    navigator = ApplicationNavigator(
        ttl_seconds=30,
        url_resolver=lambda _application_id: (_verified_resolution(), {"role": "R"}),
        headless=True,
    )
    try:
        session = navigator.start("app-lifecycle-2", RunMode.REVIEW)
        failed = navigator.wait_for_terminal(session.session_id, timeout=5)
        assert failed.state is SessionState.FAILED
        assert "launch" in failed.reason.casefold()
        # The runtime was started, then launch raised; the owner-thread
        # finally path must still stop the runtime so no driver survives.
        assert runtime.stopped is True
        assert runtime.stop_count == 1
    finally:
        try:
            navigator.shutdown()
        except Exception:
            pass


def _owned_worker(*, fail_page_close: bool = False):
    from app.services.navigator import HeadedSessionWorker

    logs: list = []
    runtime = _FakePlaywright(logs)
    browser = _FakeBrowser(logs)
    context = browser.context
    page = _FakePage(logs)
    page.fail_close = fail_page_close
    context.pages.append(page)

    class Journey:
        def submit(self, _page, _confirmation_id):
            return {"state": "CONFIRMED"}

    worker = HeadedSessionWorker(
        session_id="session-lifecycle",
        application_id="application-lifecycle",
        mode=RunMode.SUBMIT.value,
        url="http://127.0.0.1:8787/application",
        summary={},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        deadline_monotonic=time.monotonic() + 30,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        headless=True,
        journey_executor=Journey(),
        resumable_on_expiry=True,
    )
    worker.owner_thread_id = threading.get_ident()
    worker._runtime = runtime
    worker._browser = browser
    worker._context = context
    worker._page = page
    return worker, runtime


def test_cleanup_stops_runtime_exactly_once() -> None:
    worker, runtime = _owned_worker()

    worker._cleanup()
    assert runtime.stop_count == 1
    assert worker.cleanup_complete is True

    # A second cleanup (or shutdown retry) must be a no-op, never a
    # second stop and never an exception.
    worker._cleanup()
    assert runtime.stop_count == 1
    assert worker.cleanup_complete is True


def test_cleanup_retry_after_unrelated_failure_does_not_restop() -> None:
    worker, runtime = _owned_worker(fail_page_close=True)

    worker._cleanup()
    assert runtime.stop_count == 1
    assert worker.cleanup_complete is False
    assert any("page.close" in failure for failure in worker._teardown_failures)

    worker._page.fail_close = False
    worker._cleanup()
    assert worker.cleanup_complete is True
    # The runtime handle was dropped after its successful stop, so the
    # retry closes the page without stopping Playwright a second time.
    assert runtime.stop_count == 1


class _DisposableResolutionWorker:
    """Fake owner: never runs a journey, honours teardown commands."""

    instances: list["_DisposableResolutionWorker"] = []

    def __init__(self, **kwargs) -> None:
        from app.automation.types import (
            SessionEvent,
            SessionEventType,
            SessionState,
        )

        self.__class__.instances.append(self)
        self.command_queue = kwargs["command_queue"]
        self.event_queue = kwargs["event_queue"]
        self.session_id = kwargs["session_id"]
        self.owner_thread_id = None
        self.cleanup_complete = False
        self.cleanup_escalated = False
        self._alive = True
        self._terminal = (SessionEvent, SessionEventType, SessionState)

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        while True:
            try:
                command = self.command_queue.get_nowait()
            except queue.Empty:
                break
            if command.command in {
                SessionCommandType.CLOSE,
                SessionCommandType.CANCEL,
                SessionCommandType.SHUTDOWN,
            }:
                event_cls, event_type, state = self._terminal
                self._alive = False
                self.cleanup_complete = True
                self.event_queue.put(
                    event_cls(
                        event=event_type.STATE_CHANGED,
                        session_id=self.session_id,
                        state=state.CANCELLED,
                        reason="closed",
                    )
                )
                self.event_queue.put(
                    event_cls(
                        event=event_type.CLEANUP_COMPLETE,
                        session_id=self.session_id,
                        state=state.CANCELLED,
                        payload={"owner_thread_id": 1},
                    )
                )
        return self._alive


def test_headless_batch_attempt_disposes_owner_without_verification() -> None:
    """One unverified headless resolve must not retain a live browser owner."""
    from app.services.navigator import ApplicationNavigator, SourceResolutionCapability

    source_url = "http://127.0.0.1:8787/lab/resolution/batch-dispose"
    settings = SimpleNamespace(
        live_domain_allowlist=frozenset(),
        apply_click_enabled=False,
        apply_click_timeout_ms=1_000,
        apply_click_run_cap=2,
    )
    _DisposableResolutionWorker.instances.clear()
    navigator = ApplicationNavigator(
        settings=settings,
        worker_factory=_DisposableResolutionWorker,
        headless=True,
        ttl_seconds=0.05,
    )
    navigator._source_context = lambda _application_id: {
        "application_id": "application-batch-dispose",
        "opportunity_id": "opportunity-batch-dispose",
        "source_url": source_url,
        "employer": "Example Employer",
        "role_title": "Analyst",
        "provider_hint": "unknown",
        "cycle": "2027",
        "source": "lab",
    }
    capability = SourceResolutionCapability(
        source_url, "127.0.0.1", "http://127.0.0.1:8787"
    )
    try:
        resolution, handoff = navigator.resolve_application_target(
            "application-batch-dispose",
            source_capability=capability,
            headed=False,
        )
    finally:
        navigator._shutdown = True

    assert resolution is None
    worker = _DisposableResolutionWorker.instances[-1]
    assert worker.cleanup_complete is True
    assert worker.is_alive() is False
    # The handoff must not advertise a continuation on a destroyed owner.
    assert handoff["can_continue"] is False
    assert handoff["resumable"] is False
    assert handoff["status"] == "batch_attempt_complete"


def test_headed_interactive_attempt_stays_resumable() -> None:
    """The leak fix must not strand an interactive headed handoff session."""
    from app.services.navigator import ApplicationNavigator, SourceResolutionCapability

    source_url = "http://127.0.0.1:8787/lab/resolution/batch-dispose"
    settings = SimpleNamespace(
        live_domain_allowlist=frozenset(),
        apply_click_enabled=False,
        apply_click_timeout_ms=1_000,
        apply_click_run_cap=2,
    )
    _DisposableResolutionWorker.instances.clear()
    navigator = ApplicationNavigator(
        settings=settings,
        worker_factory=_DisposableResolutionWorker,
        headless=True,
        ttl_seconds=0.05,
    )
    navigator._source_context = lambda _application_id: {
        "application_id": "application-batch-keep",
        "opportunity_id": "opportunity-batch-keep",
        "source_url": source_url,
        "employer": "Example Employer",
        "role_title": "Analyst",
        "provider_hint": "unknown",
        "cycle": "2027",
        "source": "lab",
    }
    capability = SourceResolutionCapability(
        source_url, "127.0.0.1", "http://127.0.0.1:8787"
    )
    try:
        resolution, handoff = navigator.resolve_application_target(
            "application-batch-keep",
            source_capability=capability,
            headed=True,
        )
    finally:
        navigator._shutdown = True

    assert resolution is None
    worker = _DisposableResolutionWorker.instances[-1]
    assert worker.cleanup_complete is False
    assert worker.is_alive() is True
    assert handoff["status"] != "batch_attempt_complete"
