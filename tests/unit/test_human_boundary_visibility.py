"""Regression tests for visible, resumable human-boundary sessions."""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timedelta, timezone

from app.automation.types import RunMode, SessionCommand, SessionCommandType, SessionEventType, SessionState
from app.automation.host_policy import origin_for_url
from app.automation.targets import TargetResolution
from app.domain.targets import TargetKind
from app.services.navigator import ApplicationNavigator, HeadedSessionWorker


def _resolution() -> TargetResolution:
    url = "http://127.0.0.1:8787/application"
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
            "role": "Analyst",
            "requisition": "/application",
            "form_identity": "application",
        },
    )


class _CaptureWorker:
    instances: list["_CaptureWorker"] = []

    def __init__(self, **kwargs):
        self.__class__.instances.append(self)
        self.session_id = kwargs["session_id"]
        self.headless = bool(kwargs["headless"])
        self.mode = kwargs["mode"]
        self.owner_thread_id = None
        self.cleanup_complete = True
        self.cleanup_escalated = False

    def start(self):
        return None

    def is_alive(self):
        return False


def test_explicit_headed_request_overrides_headless_navigator_singleton():
    """A shared headless manager must honour one session's headed intent."""

    _CaptureWorker.instances.clear()
    navigator = ApplicationNavigator(
        ttl_seconds=30,
        headless=True,
        worker_factory=_CaptureWorker,
        url_resolver=lambda _application_id: (_resolution(), {}),
    )

    snapshot = navigator.start("headed-app", RunMode.SUBMIT, headed=True)

    worker = _CaptureWorker.instances[-1]
    assert worker.headless is False
    assert snapshot.headed is True
    assert snapshot.visibility == "headed"


def test_review_is_visible_even_when_caller_omits_headed_flag():
    """Review is always a visible handoff, including on a headless singleton."""

    _CaptureWorker.instances.clear()
    navigator = ApplicationNavigator(
        ttl_seconds=30,
        headless=True,
        worker_factory=_CaptureWorker,
        url_resolver=lambda _application_id: (_resolution(), {}),
    )

    snapshot = navigator.start("review-app", RunMode.REVIEW, headed=False)

    worker = _CaptureWorker.instances[-1]
    assert worker.headless is False
    assert snapshot.headed is True


def test_mutation_modes_are_visible_before_any_human_boundary_is_discovered():
    """PREFILL/SUBMIT cannot discover a challenge inside a hidden browser."""

    for mode in (RunMode.PREFILL, RunMode.SUBMIT):
        _CaptureWorker.instances.clear()
        navigator = ApplicationNavigator(
            ttl_seconds=30,
            headless=True,
            worker_factory=_CaptureWorker,
            url_resolver=lambda _application_id: (_resolution(), {}),
        )
        snapshot = navigator.start(f"visible-{mode.value}", mode, headed=False)
        assert _CaptureWorker.instances[-1].headless is False
        assert snapshot.visibility == "headed"


def test_nonterminal_human_result_becomes_typed_persistent_boundary():
    """NEEDS_USER/OA results stay resumable instead of becoming ACTIVE/closed."""

    worker = HeadedSessionWorker(
        session_id="boundary-session",
        application_id="boundary-app",
        mode=RunMode.PREFILL.value,
        url="http://127.0.0.1:8787/application",
        summary={},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        deadline_monotonic=time.monotonic() + 30,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        headless=True,
    )
    worker.owner_thread_id = threading.get_ident()

    worker._emit_journey_result(
        {
            "state": "NEEDS_USER",
            "reason": "Sensitive demographic question requires human choice",
            "risk_level": 3,
            "blocked_reasons": ("sensitive_demographic",),
        }
    )

    assert worker._state is SessionState.HUMAN_REQUIRED
    events = []
    while not worker.event_queue.empty():
        events.append(worker.event_queue.get_nowait())
    state_event = next(event for event in events if event.event is SessionEventType.STATE_CHANGED)
    journey_event = next(event for event in events if event.event is SessionEventType.JOURNEY_RESULT)
    assert journey_event.state is SessionState.HUMAN_REQUIRED
    assert journey_event.payload["state"] == "NEEDS_USER"
    boundary = state_event.payload["human_boundary"]
    assert boundary["session_id"] == "boundary-session"
    assert boundary["application_id"] == "boundary-app"
    assert boundary["reason"] == "Sensitive demographic question requires human choice"
    assert boundary["status"] == "awaiting_human"
    assert boundary["can_continue"] is True
    assert boundary["can_cancel"] is True
    assert boundary["no_auto_submit"] is True
    assert boundary["expires_at"]


def test_boundary_reports_effective_visibility_after_mutation_mode_is_forced_visible():
    """A stale headless request must not make a visible boundary look hidden."""

    worker = HeadedSessionWorker(
        session_id="effective-visibility-session",
        application_id="effective-visibility-app",
        mode=RunMode.PREFILL.value,
        url="http://127.0.0.1:8787/application",
        summary={},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        deadline_monotonic=time.monotonic() + 30,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        headless=True,
    )
    worker._effective_headless = False

    boundary = worker._human_boundary_payload("CAPTCHA requires a person")

    assert boundary["visibility"] == "headed"
    assert boundary["requires_visible"] is False


def test_continue_from_boundary_never_calls_submit():
    """A Continue command may resume inspection but can never auto-submit."""

    calls: list[str] = []
    worker = HeadedSessionWorker(
        session_id="continue-session",
        application_id="continue-app",
        mode=RunMode.SUBMIT.value,
        url="http://127.0.0.1:8787/application",
        summary={},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        deadline_monotonic=time.monotonic() + 30,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        headless=False,
    )
    worker.owner_thread_id = threading.get_ident()
    worker._state = SessionState.HUMAN_REQUIRED
    worker._journey_started = True
    worker._journey_result = {
        "state": "NEEDS_USER",
        "human_boundary_resume_allowed": True,
    }
    worker._run_journey = lambda *, resume=False: calls.append(f"resume={resume}")
    worker._confirm_submission = lambda _command: calls.append("submit")

    worker._handle_command(
        SessionCommand(
            command=SessionCommandType.CONTINUE,
            session_id="continue-session",
        )
    )

    assert calls == ["resume=True"]


def test_boundary_envelope_keeps_specific_human_reason():
    cases = [
        ("NEEDS_USER", "sensitive_demographic", "demographic"),
        ("NEEDS_USER", "legal_attestation", "legal"),
        ("NEEDS_USER", "mfa_required", "authentication"),
        ("NEEDS_OA", "assessment_handoff", "assessment"),
        ("NEEDS_USER", "captcha_handoff", "captcha"),
        ("BLOCKED", "unknown_required_field", "risk_review"),
    ]
    for state, code, kind in cases:
        worker = HeadedSessionWorker(
            session_id=f"boundary-{kind}",
            application_id=f"application-{kind}",
            mode=RunMode.SUBMIT.value,
            url="http://127.0.0.1:8787/application",
            summary={},
            command_queue=queue.Queue(),
            event_queue=queue.Queue(),
            ttl_seconds=30,
            deadline_monotonic=time.monotonic() + 30,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            headless=False,
        )
        worker.owner_thread_id = threading.get_ident()
        worker._emit_journey_result(
            {
                "state": state,
                "reason": (
                    "Online assessment requires human completion"
                    if state == "NEEDS_OA"
                    else "Approved mapping or human review is required"
                ),
                "risk_level": 4 if state == "BLOCKED" else 3,
                "blocked_reasons": (code,),
            }
        )
        assert worker._state is SessionState.HUMAN_REQUIRED
        events = []
        while not worker.event_queue.empty():
            events.append(worker.event_queue.get_nowait())
        state_event = next(event for event in events if event.event is SessionEventType.STATE_CHANGED)
        boundary = state_event.payload["human_boundary"]
        assert boundary["kind"] == kind
        assert boundary["can_continue"] is True
        assert boundary["no_auto_submit"] is True
