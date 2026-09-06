"""Phase 29 regressions for exact CAPTCHA egress and handoff ownership."""
from __future__ import annotations

import gc
import queue
import threading
import time
from types import SimpleNamespace

from app.automation import host_policy
from app.automation.host_policy import origin_for_url
from app.automation.runner import AutomationRunner, _OwnerThreadJourney, _handoff_next_action
from app.automation.targets import TargetResolution
from app.automation.types import (
    RunMode,
    SessionCommandType,
    SessionDiagnostics,
    SessionEvent,
    SessionEventType,
    SessionState,
)
from app.domain.targets import TargetKind
from app.services import navigator as navigator_module
from app.services.navigator import ApplicationNavigator, HeadedSessionWorker


def _captcha_decision(**overrides: object):
    values = {
        "url": (
            "https://www.gstatic.com/recaptcha/releases/"
            "ox8dsmiqR62P1bqhciWOn7Fg/recaptcha__en.js"
        ),
        "method": "GET",
        "resource_type": "other",
        "is_navigation_request": False,
        "is_main_frame_navigation": False,
        "mode": RunMode.PREFILL.value,
        "resolved_vendor": "greenhouse",
        "current_page_url": (
            "https://job-boards.greenhouse.io/example/jobs/1234567"
        ),
        "carries_candidate_data": False,
    }
    values.update(overrides)
    return host_policy.authorize_captcha_request(**values)


def test_observed_gstatic_recaptcha_release_other_type_is_allowed() -> None:
    decision = _captcha_decision()

    assert decision.allowed is True
    assert decision.reason == "captcha_provider_exact_host"
    assert decision.host == "www.gstatic.com"
    assert decision.path.startswith("/recaptcha/releases/")
    assert decision.resource_type == "other"
    assert decision.carries_candidate_data is False
    assert decision.defect is False


def test_arbitrary_path_on_captcha_host_remains_fatal() -> None:
    decision = _captcha_decision(
        url="https://www.gstatic.com/recaptcha/unobserved/challenge.js",
        resource_type="script",
    )

    assert decision.allowed is False
    assert decision.reason == "captcha_path_not_allowed"
    assert decision.defect is False


def test_unknown_resource_type_on_observed_path_remains_fatal() -> None:
    decision = _captcha_decision(resource_type="websocket")

    assert decision.allowed is False
    assert decision.reason == "captcha_resource_type_not_allowed"
    assert decision.defect is False


def test_candidate_data_on_observed_captcha_path_is_a_defect() -> None:
    decision = _captcha_decision(carries_candidate_data=True)

    assert decision.allowed is False
    assert decision.reason == "captcha_candidate_data_defect"
    assert decision.carries_candidate_data is True
    assert decision.defect is True


def test_captcha_handoff_copy_uses_permitted_evidence_and_safe_blank_reasons() -> None:
    action = _handoff_next_action(
        {
            "state": "NEEDS_USER",
            "manifest": {
                "captcha_requests": [{"host": "www.gstatic.com"}],
                "blank_field_reasons": [
                    {"label": "School", "reason": "no approved stored answer"},
                    {
                        "label": "GPA",
                        "reason": "plausibility check rejected the stored value",
                    },
                ],
            },
        }
    )

    assert action.startswith("CAPTCHA needs solving. Fields requiring review:")
    assert "School: no approved stored answer" in action
    assert "GPA: plausibility check rejected the stored value" in action
    assert action.endswith("Press Submit yourself")


def test_every_partial_prefill_handoff_carries_blank_reasons_without_values() -> None:
    from app.domain.questions import Sensitivity

    journey = object.__new__(_OwnerThreadJourney)
    journey.last_plan = SimpleNamespace(
        actions=(
            SimpleNamespace(
                status="resolved",
                value="approved fixture value",
                field=SimpleNamespace(question=SimpleNamespace(label="First name", name="first_name")),
                source="profile",
                mapping=SimpleNamespace(canonical_key=None, sensitivity=None),
            ),
            SimpleNamespace(
                status="unresolved",
                value=None,
                field=SimpleNamespace(question=SimpleNamespace(label="School", name="school")),
                source="missing",
                mapping=SimpleNamespace(canonical_key=None, sensitivity=None),
            ),
            SimpleNamespace(
                status="unresolved",
                value=None,
                field=SimpleNamespace(question=SimpleNamespace(label="GPA", name="gpa")),
                source="plausibility_guard",
                mapping=SimpleNamespace(canonical_key=None, sensitivity=None),
            ),
            SimpleNamespace(
                status="unresolved",
                value=None,
                field=SimpleNamespace(question=SimpleNamespace(label="Work authorisation", name="work_authorisation")),
                source="missing",
                mapping=SimpleNamespace(canonical_key=None, sensitivity=Sensitivity.LEGAL),
            ),
        )
    )

    manifest = journey._handoff_field_manifest(prefilled_fields=["first_name"])

    assert manifest["prefilled_fields"] == ["first_name"]
    assert manifest["blank_fields"] == ["School", "GPA", "Work authorisation"]
    assert manifest["plausibility_rejected_fields"] == ["GPA"]
    assert manifest["blank_field_reasons"] == [
        {"label": "School", "reason": "no approved stored answer"},
        {
            "label": "GPA",
            "reason": "plausibility check rejected the stored value",
        },
        {"label": "Work authorisation", "reason": "no exact stored-profile match"},
    ]


class _Frame:
    url = "https://job-boards.greenhouse.io/example/jobs/1234567"


class _Request:
    def __init__(self, *, url: str, resource_type: str) -> None:
        self.url = url
        self.method = "GET"
        self.resource_type = resource_type
        self.post_data = ""
        self.headers: dict[str, str] = {}
        self.frame = _Frame()

    def is_navigation_request(self) -> bool:
        return False


class _Route:
    def __init__(self, request: _Request) -> None:
        self.request = request
        self.continued = 0
        self.aborted: list[str] = []

    def continue_(self) -> None:
        self.continued += 1

    def abort(self, reason: str) -> None:
        self.aborted.append(reason)


def _route_worker() -> HeadedSessionWorker:
    page_url = "https://job-boards.greenhouse.io/example/jobs/1234567"
    worker = HeadedSessionWorker(
        session_id="phase29-captcha-route",
        application_id="application-phase29",
        mode=RunMode.PREFILL.value,
        url=page_url,
        summary={"provider": "greenhouse"},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        headless=True,
        allowlist=frozenset(),
        egress_impact_classification_enabled=True,
    )
    worker.owner_thread_id = threading.get_ident()
    worker._page = SimpleNamespace(url=page_url)
    return worker


def test_observed_gstatic_other_resource_continues_and_is_recorded_as_captcha() -> None:
    route = _Route(
        _Request(
            url=(
                "https://www.gstatic.com/recaptcha/releases/"
                "ox8dsmiqR62P1bqhciWOn7Fg/recaptcha__en.js"
            ),
            resource_type="other",
        )
    )
    worker = _route_worker()

    worker._route(route)

    assert route.continued == 1
    assert route.aborted == []
    assert worker._egress_fatal_reason == ""
    record = worker._egress_records[-1]
    assert record["captcha_provider"] is True
    assert record["captcha_permission"] is True
    assert record["delivery"] == "permitted"
    assert record["resource_type"] == "other"
    assert record["carries_candidate_data"] is False


def test_runner_discovers_the_app_bound_navigator_after_creator_returns() -> None:
    service_navigator = object()
    database = SimpleNamespace()
    bind = getattr(navigator_module, "bind_service_navigator", None)
    assert callable(bind), "Phase 29 service-navigator binding is missing"
    bind(database, service_navigator)

    runner = object.__new__(AutomationRunner)
    runner._handoff_manager = None
    runner.database = database
    runner.settings = SimpleNamespace(browser_headless=True)

    selected, owned = runner._navigator_for_run(headed=True)

    assert selected is service_navigator
    assert owned is False


def _verified_resolution() -> TargetResolution:
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


class _PersistentOwnerWorker(threading.Thread):
    """Small owner-thread double for the service-boundary lifecycle test."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(daemon=False)
        self.session_id = str(kwargs["session_id"])
        self._commands = kwargs["command_queue"]
        self._events = kwargs["event_queue"]
        self._deadline = float(kwargs["deadline_monotonic"])
        self._state = SessionState.OPENING
        self._page = object()
        self._cleanup_complete = False
        self._cleanup_escalated = False
        self.owner_thread_id: int | None = None
        self._operation_log: list[tuple[str, int]] = []

    @property
    def cleanup_complete(self) -> bool:
        return self._cleanup_complete

    @property
    def cleanup_escalated(self) -> bool:
        return self._cleanup_escalated

    def _emit(
        self,
        event: SessionEventType,
        *,
        state: SessionState | None = None,
        reason: str = "",
        payload: dict[str, object] | None = None,
    ) -> None:
        self._events.put(
            SessionEvent(
                event=event,
                session_id=self.session_id,
                state=state,
                reason=reason,
                payload=payload or {},
            )
        )

    def diagnostics(self) -> SessionDiagnostics:
        return SessionDiagnostics(
            session_id=self.session_id,
            owner_thread_id=self.owner_thread_id,
            worker_alive=self.is_alive(),
            operation_log=tuple(self._operation_log),
            page_token=id(self._page),
            page_count=1,
            teardown_complete=self._cleanup_complete,
        )

    def run(self) -> None:
        self.owner_thread_id = threading.get_ident()
        self._operation_log.append(("owner_ready", self.owner_thread_id))
        self._emit(SessionEventType.WORKER_READY)
        self._state = SessionState.ACTIVE
        self._emit(SessionEventType.STATE_CHANGED, state=SessionState.ACTIVE)
        try:
            while True:
                if (
                    self._state in {SessionState.ACTIVE, SessionState.HUMAN_REQUIRED}
                    and time.monotonic() >= self._deadline
                ):
                    self._state = SessionState.EXPIRED
                    self._emit(
                        SessionEventType.STATE_CHANGED,
                        state=SessionState.EXPIRED,
                        reason="session TTL expired",
                    )
                try:
                    command = self._commands.get(timeout=0.005)
                except queue.Empty:
                    continue
                command_type = getattr(command, "command", None)
                if command_type is SessionCommandType.RESUME:
                    self._deadline = float(command.payload["deadline_monotonic"])
                    self._state = SessionState.ACTIVE
                    self._emit(
                        SessionEventType.STATE_CHANGED,
                        state=SessionState.ACTIVE,
                        reason="resumed",
                    )
                elif command_type in {
                    SessionCommandType.CANCEL,
                    SessionCommandType.CLOSE,
                    SessionCommandType.SHUTDOWN,
                }:
                    self._state = SessionState.CANCELLED
                    self._emit(
                        SessionEventType.STATE_CHANGED,
                        state=SessionState.CANCELLED,
                        reason=str(getattr(command, "reason", "")),
                    )
                    return
        finally:
            self._cleanup_complete = True
            self._emit(
                SessionEventType.CLEANUP_COMPLETE,
                payload={"owner_thread_id": self.owner_thread_id or 0},
            )


def test_service_owned_page_survives_creator_collection_expiry_and_resume() -> None:
    """The service owner, not a short-lived runner, retains the page handle."""

    database = SimpleNamespace()
    navigator = ApplicationNavigator(
        database,
        ttl_seconds=0.05,
        expiry_preservation_seconds=0.3,
        worker_factory=_PersistentOwnerWorker,
        headless=True,
    )
    navigator_module.bind_service_navigator(database, navigator)
    runner = object.__new__(AutomationRunner)
    runner._handoff_manager = None
    runner.database = database
    runner.settings = SimpleNamespace(browser_headless=True)

    try:
        selected, owned = runner._navigator_for_run(headed=True)
        assert selected is navigator
        assert owned is False
        session = selected.start(
            "phase29-lifecycle",
            RunMode.PREFILL,
            resolution=_verified_resolution(),
        )
        del runner
        gc.collect()

        active = selected.wait_for_state(
            session.session_id, SessionState.ACTIVE, timeout=1
        )
        page_token = selected.diagnostics(session.session_id).page_token
        assert active.worker_alive is True
        assert active.visibility == "headed"
        assert page_token is not None

        expired = selected.wait_for_state(
            session.session_id, SessionState.EXPIRED, timeout=1
        )
        assert expired.resumable is True
        assert expired.worker_alive is True
        assert selected.diagnostics(session.session_id).page_token == page_token

        resumed = selected.resume(session.session_id)
        assert resumed.state is SessionState.ACTIVE
        assert selected.diagnostics(session.session_id).page_token == page_token
    finally:
        navigator.shutdown()
