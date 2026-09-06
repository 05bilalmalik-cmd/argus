from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from app.domain.questions import FormQuestion, QuestionMapping
from app.domain.risk import RiskDecision


class RunMode(StrEnum):
    DRY_RUN = "dry-run"
    INSPECT = "inspect"      # no form mutation, no candidate-bearing requests
    PREFILL = "prefill"      # fill only after explicit user action; never submit
    REVIEW = "review"
    SUBMIT = "submit"


class SessionState(StrEnum):
    """Truthful state of one persistent application-browser session."""

    OPENING = "OPENING"
    ACTIVE = "ACTIVE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    FINAL_REVIEW = "FINAL_REVIEW"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in {
            SessionState.CONFIRMED,
            SessionState.UNKNOWN,
            SessionState.CANCELLED,
            SessionState.EXPIRED,
            SessionState.FAILED,
        }


class SessionCommandType(StrEnum):
    """Commands accepted by a :class:`HeadedSessionWorker` queue."""

    CONTINUE = "CONTINUE"
    RESUME = "RESUME"
    CANCEL = "CANCEL"
    CLOSE = "CLOSE"
    FINAL_MANIFEST = "FINAL_MANIFEST"
    PREFLIGHT_SUBMISSION = "PREFLIGHT_SUBMISSION"
    CONFIRM = "CONFIRM"
    EXPIRE = "EXPIRE"
    INJECT_HUMAN_REQUIRED = "INJECT_HUMAN_REQUIRED"
    CAPTCHA = "CAPTCHA"
    RETRY_CLEANUP = "RETRY_CLEANUP"
    SHUTDOWN = "SHUTDOWN"
    RUN_JOURNEY = "RUN_JOURNEY"
    CONFIRM_SUBMISSION = "CONFIRM_SUBMISSION"


class SessionEventType(StrEnum):
    """Events emitted by the owner thread and consumed by the manager."""

    STATE_CHANGED = "STATE_CHANGED"
    REQUEST = "REQUEST"
    WORKER_READY = "WORKER_READY"
    CLEANUP_COMPLETE = "CLEANUP_COMPLETE"
    ERROR = "ERROR"
    JOURNEY_RESULT = "JOURNEY_RESULT"


@dataclass(frozen=True, slots=True)
class SessionCommand:
    """Typed message sent to a worker; it never contains Playwright objects."""

    command: SessionCommandType
    session_id: str
    reason: str = ""
    payload: Mapping[str, object] = field(default_factory=dict)

    @property
    def kind(self) -> SessionCommandType:
        """Alias used by queue consumers that call the discriminator ``kind``."""

        return self.command


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """Typed owner-thread event used to update an immutable session snapshot."""

    event: SessionEventType
    session_id: str
    state: SessionState | None = None
    reason: str = ""
    payload: Mapping[str, object] = field(default_factory=dict)
    occurred_at: datetime | None = None

    @property
    def kind(self) -> SessionEventType:
        return self.event


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """Serializable session view; deliberately contains no browser handles."""

    session_id: str
    application_id: str
    mode: str
    state: SessionState
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    reason: str = ""
    summary: Mapping[str, object] = field(default_factory=dict)
    owner_thread_id: int | None = None
    worker_alive: bool = False
    cleanup_complete: bool = False
    manifest: Mapping[str, object] = field(default_factory=dict)
    # Visibility is a per-session intent.  A shared ApplicationNavigator may
    # be configured headless for automated work, but a review/human handoff
    # must be able to demand a headed owner without changing the singleton.
    headed: bool = False
    # Last typed human-boundary envelope emitted by the owner thread.  It is
    # deliberately scalar/serialisable so the handoff API can persist and
    # display the resumable contract without exposing browser handles.
    human_boundary: Mapping[str, object] = field(default_factory=dict)
    # Expiry revokes all ordinary browser commands but a service-owned worker
    # may retain the exact page for a bounded preservation window.  Resume is
    # the sole command accepted in that state and issues a fresh finite TTL.
    resumable: bool = False
    resume_until: datetime | None = None

    @property
    def status(self) -> str:
        """Compatibility alias used by the existing handoff UI/API."""

        return self.state.value

    @property
    def outcome(self) -> str:
        """Compatibility alias; never reports a generic submitted outcome."""

        return self.reason

    @property
    def visibility(self) -> str:
        """Return the effective per-session browser visibility posture."""

        return "headed" if self.headed else "headless"


@dataclass(frozen=True, slots=True)
class SessionDiagnostics:
    """Immutable lifecycle evidence without browser/page handles."""

    session_id: str
    owner_thread_id: int | None
    worker_alive: bool
    operation_log: tuple[tuple[str, int], ...] = ()
    teardown_failures: tuple[str, ...] = ()
    teardown_complete: bool = False
    page_token: int | None = None
    page_count: int = 0


@dataclass(frozen=True, slots=True)
class InspectedField:
    selector: str
    question: FormQuestion
    control_type: str
    value_attribute: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedFieldValue:
    value: str
    source: str
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class FieldAction:
    field: InspectedField
    mapping: QuestionMapping
    value: str | None
    source: str
    status: str


@dataclass(frozen=True, slots=True)
class FillPlan:
    actions: tuple[FieldAction, ...]
    risk: RiskDecision


@dataclass(frozen=True, slots=True)
class Receipt:
    confirmation_text: str
    url: str
    reference: str = ""


@dataclass(frozen=True, slots=True)
class AutomationOutcome:
    state: str
    risk_level: int
    adapter: str
    blocked_reasons: tuple[str, ...] = ()
    receipt: Receipt | None = None
    trace_path: str = ""
    screenshot_path: str = ""
    run_id: str = ""
    source_url: str = ""
    application_url: str = ""
    target_status: str = ""
    resolution_evidence: Mapping[str, object] = field(default_factory=dict)
    # A human-only result remains attached to a live Navigator owner.  These
    # fields let direct callers persist/display the exact resumable handoff
    # rather than mistaking NEEDS_USER/NEEDS_OA for a completed run.
    handoff_session_id: str = ""
    human_boundary: Mapping[str, object] = field(default_factory=dict)
