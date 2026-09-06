"""Owner-thread final submission preflight regressions."""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.automation.targets import SubmissionTarget
from app.automation.types import RunMode, SessionState
from app.services.navigator import HeadedSessionWorker


_PAGE_URL = "http://127.0.0.1:8787/application"
_ACTION_URL = "http://127.0.0.1:8787/application/submit"


class _Page:
    url = _PAGE_URL

    def evaluate(self, _script):
        return {"captcha": False, "reason": ""}


def _manifest(*, control_selector: str = "button[type=submit]") -> dict[str, object]:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    return {
        "application_id": "preflight-app",
        "employer": "Example Employer",
        "role": "Analyst",
        "requisition": "req-1",
        "provider": "greenhouse",
        "form_identity": "form-1",
        "destination": _PAGE_URL,
        "frame_url": _PAGE_URL,
        "page_id": "page-1",
        "root_selector": "form#application",
        "control_selector": control_selector,
        "target_fingerprint": "target-1",
        "control_fingerprint": "control-1",
        "form_action": _ACTION_URL,
        "method": "POST",
        "expected_final_url": _ACTION_URL,
        "final_url": _PAGE_URL,
        "expires_at": expires_at.isoformat(),
    }


class _ReadyExecutor:
    final_target = None

    def __init__(self, result=None):
        self.result = result or {"state": "READY", "preflight_passed": True}
        self.calls: list[int] = []

    def preflight_submission(self, _page):
        self.calls.append(threading.get_ident())
        return self.result


def _worker(executor, manifest=None):
    worker = HeadedSessionWorker(
        session_id="preflight-session",
        application_id="preflight-app",
        mode=RunMode.SUBMIT.value,
        url=_PAGE_URL,
        summary={},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        deadline_monotonic=time.monotonic() + 30,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        headless=False,
        journey_executor=executor,
    )
    worker.owner_thread_id = threading.get_ident()
    worker._page = _Page()
    worker._pages = [worker._page]
    worker._state = SessionState.FINAL_REVIEW
    worker._manifest = dict(manifest or _manifest())
    worker._journey_result = {"manifest": dict(worker._manifest)}
    return worker


def _events(worker):
    events = []
    while not worker.event_queue.empty():
        events.append(worker.event_queue.get_nowait())
    return events


def test_preflight_runs_on_owner_thread_and_never_clicks():
    executor = _ReadyExecutor()
    worker = _worker(executor)

    worker._preflight_submission()

    assert executor.calls == [threading.get_ident()]
    assert worker._state is SessionState.FINAL_REVIEW
    request = next(event for event in _events(worker) if event.event.value == "REQUEST")
    assert request.payload["preflight_passed"] is True
    assert request.payload["submission_blocked"] is False


def test_captcha_preflight_stops_before_provider_validator_or_click():
    executor = _ReadyExecutor()
    worker = _worker(executor)
    worker._injected_human_required = True
    worker._injected_human_reason = "captcha_detected"

    worker._preflight_submission()

    assert executor.calls == []
    assert worker._state is SessionState.HUMAN_REQUIRED
    request = next(event for event in _events(worker) if event.event.value == "REQUEST")
    assert request.payload["preflight_passed"] is False
    assert request.payload["submission_blocked"] is True
    assert request.payload["human_boundary"]["kind"] == "captcha"


def test_preflight_refuses_changed_exact_control_without_click():
    target = SimpleNamespace(
        page_id="page-1",
        root_selector="form#application",
        control_selector="button[type=submit]",
        control_fingerprint="control-1",
        form_action=_ACTION_URL,
        method="POST",
        frame_url=_PAGE_URL,
        provider="greenhouse",
        destination=_PAGE_URL,
    )
    changed = SimpleNamespace(**{**target.__dict__, "control_selector": "button#changed"})

    class Adapter:
        def submission_target(self, _scope, _handle):
            return changed

    class Executor(_ReadyExecutor):
        final_target = target
        final_handle = object()
        final_scope = None
        adapter = Adapter()

        @staticmethod
        def _scope(page):
            return page

        @staticmethod
        def _target_binding(_target):
            return {"target_fingerprint": "target-1"}

    executor = Executor()
    worker = _worker(executor, _manifest())
    executor.final_scope = worker._page

    worker._preflight_submission()

    assert executor.calls == [threading.get_ident()]
    assert worker._state is SessionState.FINAL_REVIEW
    request = next(event for event in _events(worker) if event.event.value == "REQUEST")
    assert request.payload["preflight_passed"] is False
    assert request.payload["submission_blocked"] is True
    assert "changed" in request.reason.lower()


def test_executor_human_boundary_is_refused_without_activation():
    executor = _ReadyExecutor(
        {"state": "HUMAN_REQUIRED", "reason": "assessment_handoff"}
    )
    worker = _worker(executor)

    worker._preflight_submission()

    assert worker._state is SessionState.HUMAN_REQUIRED
    request = next(event for event in _events(worker) if event.event.value == "REQUEST")
    assert request.payload["preflight_passed"] is False
    assert request.payload["submission_blocked"] is True
    assert request.payload["human_boundary"]["kind"] == "assessment"

