from __future__ import annotations

import atexit
import ipaddress
import json
import logging
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Protocol
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import inspect as sqlalchemy_inspect

from app.config import Settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.db import Database


LOGGER = logging.getLogger(__name__)
_HUMAN_STATES = frozenset({"NEEDS_USER", "NEEDS_OA", "HUMAN_REQUIRED"})
_MAX_DIGEST_ITEMS = 10
_HTTP_TIMEOUT_SECONDS = 5
_HERMES_NOTIFY_TARGETS = frozenset({"telegram", "slack", "signal", "discord"})
_SHUTDOWN_DRAIN_SECONDS = 15
_CLAIM_LEASE_SECONDS = 30
_SCHEDULING = object()
_SESSION_EVENTS_KEY = "argus_human_attention_events"
_SESSION_EXPLICIT_EVENTS_KEY = "argus_explicit_human_attention_events"
_OBSERVER_ATTRIBUTE = "_argus_notification_observer"
_RUNNER_ORIGIN_KEY = "notification_origin"
_RUNNER_ORIGIN_VALUE = "automation_runner"
_SERVICE_CACHE_LOCK = threading.Lock()
_SERVICE_CACHE: dict[tuple[object, ...], "NotificationService"] = {}

# Only these codes may reach a backend. Free text is used solely as local
# classification evidence and is never copied into an event or payload.
_APPROVED_REASON_CODES = frozenset(
    {
        "application_entry_unresolved",
        "application_root_unverified",
        "approved_legal_answer_missing",
        "assessment_handoff",
        "authentication_handoff",
        "captcha",
        "destination_identity_unverified",
        "field_verification_failed",
        "human_review_required",
        "programme_framing_required",
        "required_answer_missing",
        "required_cover_letter_missing",
        "required_cv_missing",
        "sensitive_demographic",
        "step_budget_exceeded",
        "step_transition_unproven",
        "submission_network_guard",
        "submission_target_guard",
        "work_authorisation_missing",
    }
)
_REASON_ALIASES = {
    "captcha_detected": "captcha",
    "captcha_handoff": "captcha",
    "online_assessment": "assessment_handoff",
    "work_authorization_missing": "work_authorisation_missing",
}


def _one_line(value: object, *, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _canonical_reason_code(value: object) -> str | None:
    code = _one_line(value, limit=80).casefold()
    code = _REASON_ALIASES.get(code, code)
    return code if code in _APPROVED_REASON_CODES else None


def select_human_attention_reason(
    *,
    state: str = "",
    boundary_kind: str = "",
    blocked_reasons: Iterable[object] = (),
    detail: str = "",
) -> str:
    """Map bounded internal evidence to one closed, non-PII reason code."""

    if _one_line(state, limit=40).upper() == "NEEDS_OA":
        return "assessment_handoff"
    candidates = (boundary_kind, *tuple(blocked_reasons))
    for candidate in candidates:
        code = _canonical_reason_code(candidate)
        if code is not None and code != "human_review_required":
            return code
    text = " ".join(str(value or "") for value in (*candidates, detail)).casefold()
    classifiers = (
        (("captcha", "challenge"), "captcha"),
        (("assessment", "online test"), "assessment_handoff"),
        (("programme framing", "program framing"), "programme_framing_required"),
        (("cover letter", "cover_letter"), "required_cover_letter_missing"),
        (
            (
                "curriculum vitae",
                "required_cv",
                "upload and approve a cv",
                "programme-compatible cv",
            ),
            "required_cv_missing",
        ),
        (("demographic",), "sensitive_demographic"),
        (
            ("work authorisation", "work authorization", "sponsorship"),
            "work_authorisation_missing",
        ),
        (("legal", "attestation"), "approved_legal_answer_missing"),
        (
            ("authentication", "auth_wall", "log in", "login", "sign in"),
            "authentication_handoff",
        ),
        (
            ("source inspection", "application target", "resolve target"),
            "application_entry_unresolved",
        ),
    )
    for fragments, code in classifiers:
        if any(fragment in text for fragment in fragments):
            return code
    return "human_review_required"


def _reason_label(reason: str) -> str:
    labels = {
        "captcha": "Captcha",
        "required_cv_missing": "Required CV missing",
        "required_cover_letter_missing": "Required cover letter missing",
        "needs_oa": "Online assessment",
    }
    if reason in labels:
        return labels[reason]
    expanded = reason.replace("_", " ")
    return expanded[:1].upper() + expanded[1:]


@dataclass(frozen=True, slots=True)
class HumanAttentionEvent:
    """The complete, deliberately PII-free notifier input contract."""

    application_id: str
    employer: str
    role: str
    state: str
    reason: str

    def __post_init__(self) -> None:
        application_id = _one_line(self.application_id, limit=80)
        employer = _one_line(self.employer, limit=240)
        role = _one_line(self.role, limit=320)
        state = _one_line(self.state, limit=40).upper()
        reason = _canonical_reason_code(self.reason)
        if not application_id or not employer or not role or reason is None:
            if reason is None and self.reason:
                raise ValueError("notification reason is not an approved reason code")
            raise ValueError(
                "application_id, employer, role, and a specific reason are required"
            )
        if state not in _HUMAN_STATES:
            raise ValueError(f"notification state is not human-blocking: {state!r}")
        object.__setattr__(self, "application_id", application_id)
        object.__setattr__(self, "employer", employer)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)

    @classmethod
    def from_application(
        cls,
        application: object,
        *,
        reason: str,
        state: str | None = None,
    ) -> "HumanAttentionEvent":
        opportunity = getattr(application, "opportunity", None)
        if opportunity is None:
            raise ValueError("application opportunity is required for notification")
        return cls(
            application_id=str(getattr(application, "id", "")),
            employer=str(getattr(opportunity, "employer", "")),
            role=str(getattr(opportunity, "role_title", "")),
            state=state or str(getattr(application, "state", "")),
            reason=reason,
        )


@dataclass(frozen=True, slots=True)
class OpportunityDiscoveredEvent:
    """PII-free discovery notification for a newly ingested opportunity."""

    employer: str
    role_title: str
    deadline: str | None
    application_url: str | None

    def __post_init__(self) -> None:
        employer = _one_line(self.employer, limit=240)
        role_title = _one_line(self.role_title, limit=320)
        object.__setattr__(self, "employer", employer)
        object.__setattr__(self, "role_title", role_title)


_DISCOVERY_EVENTS_KEY = "argus_discovery_events"
_DISCOVERY_OBSERVER_ATTRIBUTE = "_argus_discovery_notification_observer"


def queue_discovery_notification(
    session: object,
    event: OpportunityDiscoveredEvent,
) -> None:
    """Queue a discovery event for batched delivery after the session commits."""
    pending = session.info.setdefault(_DISCOVERY_EVENTS_KEY, [])
    pending.append(event)


class NotificationBackend(Protocol):
    def send(self, payload: dict[str, object]) -> None: ...


class _HttpClient(Protocol):
    def post(self, url: str, **kwargs: object) -> object: ...


class _SubprocessResult(Protocol):
    returncode: int


class _SubprocessRunner(Protocol):
    def __call__(self, argv: list[str], **kwargs: object) -> _SubprocessResult: ...


class _StdoutBackend:
    def send(self, payload: dict[str, object]) -> None:
        LOGGER.info("%s", payload["message"])


class _NtfyBackend:
    def __init__(
        self, client: _HttpClient, server: str, topic: str, title: str = "ARGUS needs you"
    ) -> None:
        self._client = client
        self._url = f"{server.rstrip('/')}/{quote(topic, safe='')}"
        self._title = title

    def send(self, payload: dict[str, object]) -> None:
        items = payload.get("items")
        first_url = ""
        if isinstance(items, list) and items and isinstance(items[0], Mapping):
            first_url = str(items[0].get("url") or "")
        title = str(payload.get("title", self._title))
        response = self._client.post(
            self._url,
            content=str(payload["message"]).encode("utf-8"),
            headers={
                "Title": title,
                "Click": first_url,
                "Tags": "bell",
                "Idempotency-Key": str(payload["idempotency_key"]),
            },
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()


class _WebhookBackend:
    def __init__(self, client: _HttpClient, url: str) -> None:
        self._client = client
        self._url = url

    def send(self, payload: dict[str, object]) -> None:
        response = self._client.post(
            self._url,
            json=payload,
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()


class _HermesBackend:
    def __init__(
        self,
        runner: _SubprocessRunner,
        target: str,
        executable: str,
    ) -> None:
        self._runner = runner
        self._target = target
        self._executable = executable

    def send(self, payload: dict[str, object]) -> None:
        safe_command = [self._executable, "send", "-t", self._target]
        argv = [*safe_command, str(payload["message"])]
        try:
            result = self._runner(
                argv,
                check=True,
                shell=False,
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except subprocess.CalledProcessError as exc:
            raise subprocess.CalledProcessError(exc.returncode, safe_command) from None
        except subprocess.TimeoutExpired as exc:
            raise subprocess.TimeoutExpired(safe_command, timeout=exc.timeout) from None
        except FileNotFoundError:
            raise FileNotFoundError(self._executable) from None
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, safe_command)


class _FanoutBackend:
    def __init__(self, backends: list[NotificationBackend]) -> None:
        self._backends = tuple(backends)

    def send(self, payload: dict[str, object]) -> None:
        for backend in self._backends:
            try:
                backend.send(payload)
            except Exception as exc:  # noqa: BLE001 - notification is never load-bearing
                LOGGER.warning(
                    "notification delivery failed; application run continues (%s: %s)",
                    type(exc).__name__,
                    exc,
                )


def _valid_http_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if not (
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    ):
        return False
    if parsed.scheme == "https":
        return True
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _thread_scheduler(delay: float, callback: Callable[[], None]):
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer


def runner_owned_navigator_summary(summary: Mapping[str, object]) -> dict[str, object]:
    """Mark immutable Navigator context as owned by the committing runner."""

    marked = dict(summary)
    marked[_RUNNER_ORIGIN_KEY] = _RUNNER_ORIGIN_VALUE
    return marked


class _NotificationStore:
    """Small transactional outbox shared safely by server and CLI processes."""

    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        self.path = path
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = self._connect()
        try:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS notification_state (
                    application_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    last_reason TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (application_id, state)
                );
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    application_id TEXT PRIMARY KEY,
                    employer TEXT NOT NULL,
                    role TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    claim_token TEXT,
                    claimed_at REAL
                );
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempted_at REAL NOT NULL,
                    claim_token TEXT NOT NULL UNIQUE,
                    delivery_key TEXT UNIQUE,
                    reserved_at REAL,
                    completed_at REAL,
                    status TEXT NOT NULL DEFAULT 'claimed'
                );
                CREATE INDEX IF NOT EXISTS ix_notification_deliveries_attempted
                ON notification_deliveries(attempted_at);
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(notification_deliveries)"
                ).fetchall()
            }
            additions = {
                "delivery_key": "TEXT",
                "reserved_at": "REAL",
                "completed_at": "REAL",
                "status": "TEXT NOT NULL DEFAULT 'claimed'",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE notification_deliveries "
                        f"ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                "UPDATE notification_deliveries SET "
                "delivery_key = COALESCE(delivery_key, claim_token), "
                "reserved_at = COALESCE(reserved_at, attempted_at), "
                "status = COALESCE(status, 'claimed')"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_notification_deliveries_delivery_key "
                "ON notification_deliveries(delivery_key)"
            )
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path), timeout=10, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def accept(
        self, event: HumanAttentionEvent, rate_limit_per_hour: int
    ) -> tuple[bool, bool]:
        now = self.clock()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM notification_deliveries WHERE reserved_at <= ?",
                (now - 3600.0,),
            )
            existing = connection.execute(
                "SELECT last_reason FROM notification_state "
                "WHERE application_id = ? AND state = ?",
                (event.application_id, event.state),
            ).fetchone()
            if existing is not None and existing["last_reason"] == event.reason:
                connection.commit()
                return False, False
            connection.execute(
                "INSERT INTO notification_state "
                "(application_id, state, last_reason, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(application_id, state) DO UPDATE SET "
                "last_reason = excluded.last_reason, updated_at = excluded.updated_at",
                (event.application_id, event.state, event.reason, now),
            )
            delivered = int(
                connection.execute(
                    "SELECT COUNT(*) FROM notification_deliveries"
                ).fetchone()[0]
            )
            if delivered >= rate_limit_per_hour:
                connection.commit()
                return False, True
            connection.execute(
                "INSERT INTO notification_outbox "
                "(application_id, employer, role, state, reason, created_at, "
                "claim_token, claimed_at) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL) "
                "ON CONFLICT(application_id) DO UPDATE SET "
                "employer = excluded.employer, role = excluded.role, "
                "state = excluded.state, reason = excluded.reason, "
                "created_at = excluded.created_at, claim_token = NULL, claimed_at = NULL",
                (
                    event.application_id,
                    event.employer,
                    event.role,
                    event.state,
                    event.reason,
                    now,
                ),
            )
            connection.commit()
            return True, False
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def has_pending(self) -> bool:
        connection = self._connect()
        try:
            return (
                connection.execute(
                    "SELECT 1 FROM notification_outbox LIMIT 1"
                ).fetchone()
                is not None
            )
        finally:
            connection.close()

    def claim(
        self, rate_limit_per_hour: int
    ) -> tuple[str, list[HumanAttentionEvent], bool]:
        now = self.clock()
        token = uuid.uuid4().hex
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM notification_deliveries WHERE reserved_at <= ?",
                (now - 3600.0,),
            )
            stale = connection.execute(
                "SELECT claim_token FROM notification_outbox "
                "WHERE claim_token IS NOT NULL AND claimed_at <= ? "
                "ORDER BY claimed_at, claim_token LIMIT 1",
                (now - _CLAIM_LEASE_SECONDS,),
            ).fetchone()
            if stale is not None:
                stable_token = str(stale["claim_token"])
                reservation = connection.execute(
                    "SELECT 1 FROM notification_deliveries WHERE delivery_key = ?",
                    (stable_token,),
                ).fetchone()
                if reservation is None:
                    delivered = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM notification_deliveries"
                        ).fetchone()[0]
                    )
                    if delivered >= rate_limit_per_hour:
                        connection.commit()
                        return "", [], True
                    connection.execute(
                        "INSERT INTO notification_deliveries "
                        "(attempted_at, claim_token, delivery_key, reserved_at, status) "
                        "VALUES (?, ?, ?, ?, 'claimed')",
                        (now, stable_token, stable_token, now),
                    )
                connection.execute(
                    "UPDATE notification_outbox SET claimed_at = ? "
                    "WHERE claim_token = ?",
                    (now, stable_token),
                )
                rows = connection.execute(
                    "SELECT application_id, employer, role, state, reason "
                    "FROM notification_outbox WHERE claim_token = ? "
                    "ORDER BY created_at, application_id",
                    (stable_token,),
                ).fetchall()
                connection.commit()
                return stable_token, self._events(rows), False
            connection.execute(
                "UPDATE notification_deliveries SET status = 'claimed' "
                "WHERE status NOT IN ('attempted', 'completed', 'failed')"
            )
            delivered = int(
                connection.execute(
                    "SELECT COUNT(*) FROM notification_deliveries"
                ).fetchone()[0]
            )
            if delivered >= rate_limit_per_hour:
                connection.commit()
                return "", [], True
            rows = connection.execute(
                "SELECT application_id, employer, role, state, reason "
                "FROM notification_outbox WHERE claim_token IS NULL "
                "ORDER BY created_at, application_id LIMIT 1000"
            ).fetchall()
            if not rows:
                connection.commit()
                return "", [], False
            application_ids = [str(row["application_id"]) for row in rows]
            connection.executemany(
                "UPDATE notification_outbox SET claim_token = ?, claimed_at = ? "
                "WHERE application_id = ? AND claim_token IS NULL",
                ((token, now, application_id) for application_id in application_ids),
            )
            connection.execute(
                "INSERT INTO notification_deliveries "
                "(attempted_at, claim_token, delivery_key, reserved_at, status) "
                "VALUES (?, ?, ?, ?, 'claimed')",
                (now, token, token, now),
            )
            connection.commit()
            return token, self._events(rows), False
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _events(rows: Iterable[sqlite3.Row]) -> list[HumanAttentionEvent]:
        return [
            HumanAttentionEvent(
                application_id=str(row["application_id"]),
                employer=str(row["employer"]),
                role=str(row["role"]),
                state=str(row["state"]),
                reason=str(row["reason"]),
            )
            for row in rows
        ]

    def mark_attempted(self, token: str) -> None:
        if not token:
            return
        now = self.clock()
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE notification_deliveries SET status = 'attempted', "
                "attempted_at = ? WHERE delivery_key = ?",
                (now, token),
            )
        finally:
            connection.close()

    def complete(self, token: str, *, delivered: bool) -> None:
        if not token:
            return
        now = self.clock()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM notification_outbox WHERE claim_token = ?", (token,)
            )
            connection.execute(
                "UPDATE notification_deliveries SET status = ?, completed_at = ? "
                "WHERE delivery_key = ?",
                ("completed" if delivered else "failed", now, token),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def retry_delay(self) -> float:
        """Return the earliest useful retry for capacity or claim recovery."""

        now = self.clock()
        connection = self._connect()
        try:
            oldest_reservation = connection.execute(
                "SELECT MIN(reserved_at) FROM notification_deliveries"
            ).fetchone()[0]
            oldest_claim = connection.execute(
                "SELECT MIN(claimed_at) FROM notification_outbox "
                "WHERE claim_token IS NOT NULL"
            ).fetchone()[0]
        finally:
            connection.close()
        delays = []
        if oldest_reservation is not None:
            delays.append(float(oldest_reservation) + 3600.0 - now)
        if oldest_claim is not None:
            delays.append(float(oldest_claim) + _CLAIM_LEASE_SECONDS - now)
        return max(0.05, min(delays, default=float(_CLAIM_LEASE_SECONDS)))


class NotificationService:
    """Batch, deduplicate, rate-limit, and fail-soft deliver attention events."""

    def __init__(
        self,
        *,
        enabled: bool,
        backend: NotificationBackend,
        local_base_url: str,
        batch_window_seconds: float,
        rate_limit_per_hour: int,
        scheduler: Callable[[float, Callable[[], None]], object] | None = None,
        clock: Callable[[], float] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._backend = backend
        self._local_base_url = local_base_url.rstrip("/")
        self._batch_window_seconds = max(0.0, float(batch_window_seconds))
        self._rate_limit_per_hour = max(1, int(rate_limit_per_hour))
        self._scheduler = scheduler or _thread_scheduler
        self._clock = clock or time.time
        self._seen: dict[tuple[str, str], str] = {}
        self._pending: dict[str, HumanAttentionEvent] = {}
        self._delivery_times: deque[float] = deque()
        self._timer: object | None = None
        self._active_flushes = 0
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._store: _NotificationStore | None = None
        if self._enabled and state_path is not None:
            try:
                self._store = _NotificationStore(state_path, self._clock)
            except (OSError, sqlite3.Error) as exc:
                self._enabled = False
                LOGGER.warning(
                    "notification outbox unavailable; notifications disabled (%s)",
                    type(exc).__name__,
                )
        if self._store is not None:
            try:
                if self._store.has_pending():
                    self._timer = _SCHEDULING
                    self._schedule_flush()
            except (OSError, sqlite3.Error) as exc:
                LOGGER.warning(
                    "notification outbox recovery check failed (%s)", type(exc).__name__
                )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        backend: NotificationBackend | None = None,
        http_client: _HttpClient | None = None,
        subprocess_runner: _SubprocessRunner | None = None,
        scheduler: Callable[[float, Callable[[], None]], object] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> "NotificationService":
        cacheable = (
            backend is None
            and http_client is None
            and subprocess_runner is None
            and scheduler is None
            and clock is None
        )
        cache_key = (
            str(settings.data_dir),
            settings.notifications_enabled,
            settings.port,
            settings.ntfy_topic,
            settings.ntfy_server,
            settings.notify_webhook_url,
            settings.hermes_notify_target,
            settings.hermes_bin,
            settings.notify_batch_window_seconds,
            settings.notify_rate_limit_per_hour,
        )
        if cacheable:
            with _SERVICE_CACHE_LOCK:
                cached = _SERVICE_CACHE.get(cache_key)
                if cached is not None:
                    return cached
        selected = backend
        if selected is None:
            client = http_client or httpx
            backends: list[NotificationBackend] = []
            if settings.ntfy_topic:
                if _valid_http_endpoint(settings.ntfy_server):
                    backends.append(
                        _NtfyBackend(client, settings.ntfy_server, settings.ntfy_topic)
                    )
                else:
                    LOGGER.warning("invalid ARGUS_NTFY_SERVER; ntfy backend disabled")
            if settings.notify_webhook_url:
                if _valid_http_endpoint(settings.notify_webhook_url):
                    backends.append(_WebhookBackend(client, settings.notify_webhook_url))
                else:
                    LOGGER.warning("invalid ARGUS_NOTIFY_WEBHOOK_URL; webhook backend disabled")
            if settings.hermes_notify_target is not None:
                if settings.hermes_notify_target in _HERMES_NOTIFY_TARGETS:
                    backends.append(
                        _HermesBackend(
                            subprocess_runner or subprocess.run,
                            settings.hermes_notify_target,
                            settings.hermes_bin,
                        )
                    )
                else:
                    LOGGER.warning(
                        "invalid ARGUS_HERMES_NOTIFY_TARGET; hermes backend disabled"
                    )
            selected = _FanoutBackend(backends) if backends else _StdoutBackend()
        service = cls(
            enabled=settings.notifications_enabled,
            backend=selected,
            local_base_url=f"http://127.0.0.1:{settings.port}",
            batch_window_seconds=settings.notify_batch_window_seconds,
            rate_limit_per_hour=settings.notify_rate_limit_per_hour,
            scheduler=scheduler,
            clock=clock,
            state_path=settings.data_dir / "notification_delivery_state.sqlite3",
        )
        if cacheable:
            with _SERVICE_CACHE_LOCK:
                selected_service = _SERVICE_CACHE.setdefault(cache_key, service)
                if selected_service is service and service._enabled:
                    atexit.register(service.flush)
                return selected_service
        return service

    def notify_discovery(
        self, events: list[OpportunityDiscoveredEvent]
    ) -> bool:
        """Send one digest for newly discovered opportunities.

        Delivery is immediate (no batch window) since the events were already
        collected across the session.  Returns whether delivery was attempted.
        """
        if not self._enabled or not events:
            return False
        idempotency_key = uuid.uuid4().hex
        self._backend.send(
            self._discovery_payload(events, idempotency_key=idempotency_key)
        )
        return True

    def _discovery_payload(
        self,
        events: list[OpportunityDiscoveredEvent],
        *,
        idempotency_key: str = "",
    ) -> dict[str, object]:
        items = [self._discovery_item(event) for event in events[:_MAX_DIGEST_ITEMS]]
        count = len(events)
        if count == 1:
            item = items[0]
            deadline = f" deadl:{item['deadline']}" if item["deadline"] else ""
            message = (
                f"ARGUS discovered: {item['employer']} — {item['role']}.{deadline}"
                f" -> {item['url']}"
            )
        else:
            summaries = " | ".join(
                f"{item['employer']} — {item['role']}"
                for item in items
            )
            omitted = count - len(items)
            suffix = f" | +{omitted} more" if omitted else ""
            message = (
                f"ARGUS discovered {count} new roles. {summaries}{suffix}"
            )
        return {
            "event": "opportunity_discovered",
            "idempotency_key": idempotency_key or uuid.uuid4().hex,
            "count": count,
            "title": "ARGUS discovered new roles",
            "message": _one_line(message, limit=4000),
            "items": items,
        }

    def _discovery_item(
        self, event: OpportunityDiscoveredEvent
    ) -> dict[str, str]:
        return {
            "employer": event.employer,
            "role": event.role_title,
            "deadline": str(event.deadline) if event.deadline else "",
            "url": event.application_url or "",
        }

    def notify(self, event: HumanAttentionEvent) -> bool:
        """Accept one typed event; return whether it entered the delivery path."""

        if not self._enabled:
            return False
        if self._store is not None:
            try:
                accepted, rate_limited = self._store.accept(
                    event, self._rate_limit_per_hour
                )
            except (OSError, sqlite3.Error) as exc:
                LOGGER.warning("notification outbox write failed (%s)", type(exc).__name__)
                return False
            if rate_limited:
                LOGGER.warning("hourly rate limit reached; dropping notification")
                return False
            if not accepted:
                return False
        else:
            with self._lock:
                dedupe_key = (event.application_id, event.state)
                if self._seen.get(dedupe_key) == event.reason:
                    return False
                self._seen[dedupe_key] = event.reason
                now = self._clock()
                self._prune_delivery_times(now)
                if len(self._delivery_times) >= self._rate_limit_per_hour:
                    LOGGER.warning("hourly rate limit reached; dropping notification")
                    return False
                self._pending[event.application_id] = event
        should_schedule = False
        with self._lock:
            if self._timer is None:
                self._timer = _SCHEDULING
                should_schedule = True
        if should_schedule:
            self._schedule_flush()
        return True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _schedule_flush(self, *, delay: float | None = None) -> None:
        selected_delay = self._batch_window_seconds if delay is None else max(0.0, delay)
        try:
            handle = self._scheduler(selected_delay, self._flush)
        except Exception as exc:  # noqa: BLE001 - delivery must never block the caller
            LOGGER.warning(
                "notification batch scheduling failed; using background fallback (%s)",
                type(exc).__name__,
            )
            try:
                handle = _thread_scheduler(0, self._flush)
            except Exception as fallback_exc:  # noqa: BLE001 - retained for drain
                with self._lock:
                    if self._timer is _SCHEDULING:
                        self._timer = None
                LOGGER.warning(
                    "notification fallback scheduling failed; pending digest retained (%s)",
                    type(fallback_exc).__name__,
                )
                return
        with self._lock:
            if self._timer is _SCHEDULING:
                self._timer = handle

    def _prune_delivery_times(self, now: float) -> None:
        cutoff = now - 3600.0
        while self._delivery_times and self._delivery_times[0] <= cutoff:
            self._delivery_times.popleft()

    def _claim_events(self) -> tuple[str, list[HumanAttentionEvent], bool]:
        if self._store is not None:
            return self._store.claim(self._rate_limit_per_hour)
        with self._lock:
            events = list(self._pending.values())
            self._pending.clear()
            if not events:
                return "", [], False
            now = self._clock()
            self._prune_delivery_times(now)
            if len(self._delivery_times) >= self._rate_limit_per_hour:
                return "", [], True
            self._delivery_times.append(now)
            return "", events, False

    def _flush(self, *, schedule_followup: bool = True) -> None:
        with self._condition:
            self._timer = None
            self._active_flushes += 1
        try:
            try:
                token, events, rate_limited = self._claim_events()
            except (OSError, sqlite3.Error, ValueError) as exc:
                LOGGER.warning("notification outbox claim failed (%s)", type(exc).__name__)
                return
            if rate_limited:
                LOGGER.warning("hourly rate limit reached; deferring pending digest")
                if schedule_followup and self._store is not None:
                    self._schedule_store_retry()
                return
            if not events:
                if schedule_followup and self._store is not None:
                    self._schedule_store_retry(only_if_pending=True)
                return
            delivered = False
            if self._store is not None:
                try:
                    self._store.mark_attempted(token)
                except (OSError, sqlite3.Error) as exc:
                    LOGGER.warning(
                        "notification attempt marker failed; delivery suppressed (%s)",
                        type(exc).__name__,
                    )
                    return
            try:
                self._backend.send(self._payload(events, idempotency_key=token))
                delivered = True
            except Exception as exc:  # noqa: BLE001 - never fail or retry-storm a run
                LOGGER.warning(
                    "notification delivery failed; application run continues (%s: %s)",
                    type(exc).__name__,
                    exc,
                )
            finally:
                if self._store is not None:
                    try:
                        self._store.complete(token, delivered=delivered)
                    except (OSError, sqlite3.Error) as exc:
                        LOGGER.warning(
                            "notification outbox completion failed (%s)",
                            type(exc).__name__,
                        )
            if schedule_followup and self._store is not None:
                self._schedule_store_retry(only_if_pending=True, immediate=True)
        finally:
            with self._condition:
                self._active_flushes -= 1
                self._condition.notify_all()

    def _schedule_store_retry(
        self, *, only_if_pending: bool = False, immediate: bool = False
    ) -> None:
        if self._store is None:
            return
        try:
            if only_if_pending and not self._store.has_pending():
                return
            delay = 0.0 if immediate else self._store.retry_delay()
        except (OSError, sqlite3.Error):
            return
        with self._lock:
            if self._timer is not None:
                return
            self._timer = _SCHEDULING
        try:
            # Recovery/capacity retries must respect wall-clock delay even in
            # tests or integrations that inject a synchronous batch scheduler.
            # A real daemon timer also prevents recursive retry calls.
            handle = _thread_scheduler(delay, self._flush)
        except Exception as exc:  # noqa: BLE001 - pending outbox remains durable
            with self._lock:
                if self._timer is _SCHEDULING:
                    self._timer = None
            LOGGER.warning(
                "notification retry scheduling failed; pending digest retained (%s)",
                type(exc).__name__,
            )
            return
        with self._lock:
            if self._timer is _SCHEDULING:
                self._timer = handle

    def flush(self) -> None:
        """Drain pending and in-flight delivery during graceful shutdown."""

        with self._lock:
            handle = self._timer
            self._timer = None
        cancel = getattr(handle, "cancel", None)
        if callable(cancel):
            cancel()
        self._flush(schedule_followup=False)
        deadline = time.monotonic() + _SHUTDOWN_DRAIN_SECONDS
        with self._condition:
            while self._active_flushes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    LOGGER.warning("notification shutdown drain timed out")
                    return
                self._condition.wait(remaining)

    def _payload(
        self,
        events: list[HumanAttentionEvent],
        *,
        idempotency_key: str = "",
    ) -> dict[str, object]:
        items = [self._item(event) for event in events[:_MAX_DIGEST_ITEMS]]
        count = len(events)
        if count == 1:
            item = items[0]
            message = (
                f"ARGUS: {item['employer']} — {item['role']}. "
                f"{item['reason']}. -> {item['url']}"
            )
        else:
            summaries = " | ".join(
                f"{item['employer']} — {item['role']}: {item['reason']} -> {item['url']}"
                for item in items
            )
            omitted = count - len(items)
            suffix = f" | +{omitted} more on the Needs-You page" if omitted else ""
            message = f"ARGUS: {count} applications need you. {summaries}{suffix}"
        return {
            "event": "human_attention_required",
            "idempotency_key": idempotency_key or uuid.uuid4().hex,
            "count": count,
            "message": _one_line(message, limit=4000),
            "items": items,
        }

    def _item(self, event: HumanAttentionEvent) -> dict[str, str]:
        return {
            "employer": event.employer,
            "role": event.role,
            "reason": _reason_label(event.reason),
            "url": f"{self._local_base_url}/needs-you/{quote(event.application_id, safe='')}",
        }


class NavigatorNotificationMonitor:
    """Observe immutable Navigator snapshots without crossing browser ownership."""

    def __init__(
        self,
        navigator: object,
        database: Database,
        notifier: NotificationService,
        *,
        interval_seconds: float = 0.25,
        enabled: bool = True,
    ) -> None:
        self._navigator = navigator
        self._database = database
        self._notifier = notifier
        self._interval_seconds = max(0.05, float(interval_seconds))
        self._enabled = bool(enabled)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._seen_edges: set[tuple[str, str, str]] = set()

    def start(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="argus-notification-monitor",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception as exc:  # noqa: BLE001 - notification is optional
                self._thread = None
                LOGGER.warning(
                    "notification Navigator monitor could not start (%s)",
                    type(exc).__name__,
                )

    def stop(self, *, timeout: float = 2.0) -> None:
        if not self._enabled:
            return
        self.scan_once()
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(max(0.0, float(timeout)))

    def _run(self) -> None:
        while not self._stop.is_set():
            self.scan_once()
            self._stop.wait(self._interval_seconds)

    def scan_once(self) -> None:
        from app.models import Application

        try:
            snapshots = self._navigator.all_sessions()
        except Exception as exc:  # noqa: BLE001 - monitor must not affect Navigator
            LOGGER.warning("notification Navigator scan failed (%s)", type(exc).__name__)
            return
        active_session_ids = {
            _one_line(getattr(snapshot, "session_id", ""), limit=80)
            for snapshot in snapshots
        }
        with self._lock:
            self._seen_edges = {
                edge for edge in self._seen_edges if edge[0] in active_session_ids
            }
        for snapshot in snapshots:
            edge: tuple[str, str, str] | None = None
            state_value = _one_line(getattr(snapshot, "state", ""), limit=40).upper()
            if state_value not in {"HUMAN_REQUIRED", "NEEDS_USER", "NEEDS_OA"}:
                continue
            summary = getattr(snapshot, "summary", {})
            if (
                isinstance(summary, Mapping)
                and summary.get(_RUNNER_ORIGIN_KEY) == _RUNNER_ORIGIN_VALUE
            ):
                # The runner projects its uncommitted owner-thread boundary as
                # HUMAN_REQUIRED. Its exact NEEDS_* event is emitted by the
                # root-commit observer, so monitoring this projection would
                # be both premature and a second durable transition.
                continue
            application_id = _one_line(
                getattr(snapshot, "application_id", ""), limit=80
            )
            if not application_id:
                continue
            try:
                with self._database.SessionLocal() as session:
                    application = session.get(Application, application_id)
                    if application is None:
                        continue
                    persisted_state = _one_line(application.state, limit=40).upper()
                    event_state = (
                        persisted_state
                        if persisted_state in {"NEEDS_USER", "NEEDS_OA"}
                        else "HUMAN_REQUIRED"
                    )
                    boundary = getattr(snapshot, "human_boundary", {})
                    boundary = boundary if isinstance(boundary, Mapping) else {}
                    reason = select_human_attention_reason(
                        state=event_state,
                        boundary_kind=str(boundary.get("kind") or ""),
                        detail=" ".join(
                            (
                                str(boundary.get("reason") or ""),
                                str(getattr(snapshot, "reason", "") or ""),
                            )
                        ),
                    )
                    notification = HumanAttentionEvent.from_application(
                        application,
                        state=event_state,
                        reason=reason,
                    )
                session_id = _one_line(
                    getattr(snapshot, "session_id", ""), limit=80
                )
                edge = (session_id, notification.state, notification.reason)
                with self._lock:
                    if edge in self._seen_edges:
                        continue
                    self._seen_edges.add(edge)
                self._notifier.notify(notification)
            except Exception as exc:  # noqa: BLE001 - monitor is never load-bearing
                if edge is not None:
                    with self._lock:
                        self._seen_edges.discard(edge)
                LOGGER.warning(
                    "notification Navigator event failed; session continues (%s: %s)",
                    type(exc).__name__,
                    exc,
                )


def queue_application_notification(session: Session, event: HumanAttentionEvent) -> None:
    """Queue an exact runner event for dispatch only after the session commits."""

    transaction = session.get_nested_transaction() or session.get_transaction()
    pending = session.info.setdefault(_SESSION_EVENTS_KEY, [])
    pending[:] = [
        queued
        for queued in pending
        if not (
            queued[0] == event.application_id and queued[2] is transaction
        )
    ]
    pending.append((event.application_id, event, transaction))
    explicit = session.info.setdefault(_SESSION_EXPLICIT_EVENTS_KEY, set())
    explicit.add((event.application_id, transaction))


def _reason_evidence(application: object) -> tuple[object, ...]:
    evidence: list[object] = [getattr(application, "next_action", "")]
    for attribute in ("eligibility_json", "conflict_json"):
        try:
            document = json.loads(str(getattr(application, attribute, "") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(document, Mapping):
            values = document.get("reason_codes") or ()
            if isinstance(values, (list, tuple, set)):
                evidence.extend(values)
    return tuple(evidence)


def install_application_notification_observer(
    database: Database,
    notifier: NotificationService,
) -> None:
    """Observe every committed Application transition without editing its producers."""

    existing = getattr(database, _OBSERVER_ATTRIBUTE, None)
    if isinstance(existing, dict):
        existing["notifier"] = notifier
        return
    holder: dict[str, NotificationService] = {"notifier": notifier}
    setattr(database, _OBSERVER_ATTRIBUTE, holder)

    def after_flush(session: Session, _context: object) -> None:
        from app.models import Application, Opportunity

        pending = session.info.setdefault(_SESSION_EVENTS_KEY, [])
        explicit = session.info.setdefault(_SESSION_EXPLICIT_EVENTS_KEY, set())
        for application in session.new.union(session.dirty):
            if not isinstance(application, Application):
                continue
            transaction = session.get_nested_transaction() or session.get_transaction()
            inspection = sqlalchemy_inspect(application)
            if not (
                application in session.new
                or inspection.attrs.state.history.has_changes()
                or inspection.attrs.next_action.history.has_changes()
                or inspection.attrs.eligibility_json.history.has_changes()
                or inspection.attrs.conflict_json.history.has_changes()
            ):
                continue
            state = _one_line(application.state, limit=40).upper()
            if (application.id, transaction) in explicit:
                if state in {"NEEDS_USER", "NEEDS_OA"}:
                    continue
                explicit.discard((application.id, transaction))
            pending[:] = [
                queued
                for queued in pending
                if not (queued[0] == application.id and queued[2] is transaction)
            ]
            if state not in {"NEEDS_USER", "NEEDS_OA"}:
                pending.append((application.id, None, transaction))
                continue
            try:
                opportunity = application.opportunity or session.get(
                    Opportunity, application.opportunity_id
                )
                if opportunity is None:
                    continue
                evidence = _reason_evidence(application)
                reason = select_human_attention_reason(
                    state=state,
                    blocked_reasons=evidence,
                    detail=" ".join(str(item) for item in evidence),
                )
                event = HumanAttentionEvent.from_application(
                    application,
                    reason=reason,
                    state=state,
                )
                pending.append(
                    (
                        application.id,
                        event,
                        transaction,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - observer must never break commit
                LOGGER.warning(
                    "notification transition observation failed; commit continues (%s: %s)",
                    type(exc).__name__,
                    exc,
                )

    def after_commit(session: Session) -> None:
        if session.in_nested_transaction():
            transaction = session.get_nested_transaction()
            if transaction is None:
                return
            parent = getattr(transaction, "parent", None) or getattr(
                transaction, "_parent", None
            )
            pending = session.info.get(_SESSION_EVENTS_KEY, [])
            moving = [queued for queued in pending if queued[2] is transaction]
            pending[:] = [queued for queued in pending if queued[2] is not transaction]
            for application_id, event, _transaction in moving:
                pending[:] = [
                    queued
                    for queued in pending
                    if not (queued[0] == application_id and queued[2] is parent)
                ]
                pending.append((application_id, event, parent))
            explicit = session.info.get(_SESSION_EXPLICIT_EVENTS_KEY, set())
            moving_explicit = {
                marker for marker in explicit if marker[1] is transaction
            }
            explicit.difference_update(moving_explicit)
            explicit.update(
                (application_id, parent)
                for application_id, _transaction in moving_explicit
            )
            return
        pending = session.info.pop(_SESSION_EVENTS_KEY, [])
        session.info.pop(_SESSION_EXPLICIT_EVENTS_KEY, None)
        latest: dict[str, HumanAttentionEvent | None] = {}
        for application_id, event, _transaction in pending:
            latest[application_id] = event
        for event in latest.values():
            if event is None:
                continue
            try:
                holder["notifier"].notify(event)
            except Exception as exc:  # noqa: BLE001 - observer is never load-bearing
                LOGGER.warning(
                    "notification post-commit hook failed; application continues (%s: %s)",
                    type(exc).__name__,
                    exc,
                )

    def after_rollback(session: Session) -> None:
        if session.in_nested_transaction():
            return
        session.info.pop(_SESSION_EVENTS_KEY, None)
        session.info.pop(_SESSION_EXPLICIT_EVENTS_KEY, None)

    def after_soft_rollback(session: Session, previous_transaction: object) -> None:
        if not bool(getattr(previous_transaction, "nested", False)):
            return
        pending = session.info.get(_SESSION_EVENTS_KEY, [])
        session.info[_SESSION_EVENTS_KEY] = [
            queued for queued in pending if queued[2] is not previous_transaction
        ]
        explicit = session.info.get(_SESSION_EXPLICIT_EVENTS_KEY, set())
        explicit.difference_update(
            marker for marker in tuple(explicit) if marker[1] is previous_transaction
        )

    sqlalchemy_event.listen(database.SessionLocal, "after_flush", after_flush)
    sqlalchemy_event.listen(database.SessionLocal, "after_commit", after_commit)
    sqlalchemy_event.listen(database.SessionLocal, "after_rollback", after_rollback)
    sqlalchemy_event.listen(
        database.SessionLocal, "after_soft_rollback", after_soft_rollback
    )


def reconcile_existing_human_applications(
    database: Database,
    notifier: NotificationService,
) -> None:
    """Catch up human states, including the one governed startup SQL repair."""

    if not notifier.enabled:
        return
    from sqlalchemy import select

    from app.models import Application

    try:
        with database.SessionLocal() as session:
            applications = session.scalars(
                select(Application).where(
                    Application.state.in_(("NEEDS_USER", "NEEDS_OA"))
                )
            ).all()
            events = []
            for application in applications:
                evidence = _reason_evidence(application)
                reason = select_human_attention_reason(
                    state=str(application.state),
                    blocked_reasons=evidence,
                    detail=" ".join(str(item) for item in evidence),
                )
                events.append(
                    HumanAttentionEvent.from_application(application, reason=reason)
                )
        for event in events:
            notifier.notify(event)
    except Exception as exc:  # noqa: BLE001 - notification is never load-bearing
        LOGGER.warning(
            "notification startup reconciliation failed; startup continues (%s: %s)",
            type(exc).__name__,
            exc,
        )


def install_discovery_notification_observer(
    database: Database,
    notifier: NotificationService,
) -> None:
    """Observe every committed session for queued discovery events."""

    existing = getattr(database, _DISCOVERY_OBSERVER_ATTRIBUTE, None)
    if isinstance(existing, dict):
        existing["notifier"] = notifier
        return
    holder: dict[str, NotificationService] = {"notifier": notifier}
    setattr(database, _DISCOVERY_OBSERVER_ATTRIBUTE, holder)

    def after_commit(session: Session) -> None:
        if session.in_nested_transaction():
            return
        pending = session.info.pop(_DISCOVERY_EVENTS_KEY, None)
        if not pending:
            return
        try:
            holder["notifier"].notify_discovery(pending)
        except Exception as exc:  # noqa: BLE001 - notification is never load-bearing
            LOGGER.warning(
                "discovery notification post-commit hook failed; "
                "session continues (%s: %s)",
                type(exc).__name__,
                exc,
            )

    def after_rollback(session: Session) -> None:
        if session.in_nested_transaction():
            return
        session.info.pop(_DISCOVERY_EVENTS_KEY, None)

    sqlalchemy_event.listen(database.SessionLocal, "after_commit", after_commit)
    sqlalchemy_event.listen(database.SessionLocal, "after_rollback", after_rollback)
