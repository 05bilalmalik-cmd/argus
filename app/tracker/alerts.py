"""Transactional, tracker-only alert delivery.

The public boundary is ``AlertService(data_dir, config=None, sender=None)`` with
``run(jobs) -> dict``, ``summary() -> dict`` and ``list_events(limit=50)``.
``jobs`` are the already-decorated tracker rows; this module does not collect,
verify, apply, or read the legacy application database.

Production configuration is created by ``AlertConfig.from_env()`` (or the
``alert_config_from_env`` alias).  It reads only these dedicated variables:
``ARGUS_TRACKER_ALERTS_ENABLED``, ``ARGUS_TRACKER_TRANSPORT``,
``ARGUS_TRACKER_SMTP_HOST``, ``ARGUS_TRACKER_SMTP_PORT``,
``ARGUS_TRACKER_SMTP_USER``, ``ARGUS_TRACKER_SMTP_PASSWORD``,
``ARGUS_TRACKER_FROM``, ``ARGUS_TRACKER_TO`` and
``ARGUS_TRACKER_SENDMAIL_PATH``.  An explicit dict is accepted for tests or
operator setup, but this service never persists the dict or its password.
Inject a sender implementing ``send(AlertMessage)`` for tests; no test sender
uses a real transport.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import smtplib
import socket
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_DELIVERY_NOTE = (
    "SMTP/provider acceptance and the internal delivery ID or Message-ID are "
    "not proof that the message reached the inbox."
)
_EVENT_STATUSES = ("pending", "sending", "sent", "failed", "uncertain")
_BOOL_TRUE = {"1", "true", "yes", "on", "enabled"}
_BOOL_FALSE = {"0", "false", "no", "off", "disabled"}


class AlertDeliveryError(Exception):
    """Base class for a sender that can classify a delivery outcome."""


class ProviderRefusedError(AlertDeliveryError):
    """The provider refused a known pre-send/known response; retry is safe."""


class DeliveryUncertainError(AlertDeliveryError):
    """The transport may have accepted data; never retry automatically."""


# Friendly aliases for integrations that use the operational terminology.
RetryableDeliveryError = ProviderRefusedError
UncertainDeliveryError = DeliveryUncertainError


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """A sender's handoff result, not an inbox-arrival receipt."""

    accepted: bool = True
    delivery_id: str = ""
    uncertain: bool = False
    retryable: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class AlertMessage:
    """One bounded digest handed to the injected or configured sender."""

    event_keys: tuple[str, ...]
    roles: tuple[dict[str, Any], ...]
    subject: str
    body: str
    message_id: str
    delivery_id: str
    from_address: str = ""
    to_address: str = ""


@dataclass(frozen=True, slots=True)
class AlertConfig:
    """Non-persistent delivery settings.

    ``smtp_password`` is intentionally an in-memory setting only.  Production
    callers should build this object with ``from_env`` rather than putting a
    secret in a profile or SQLite database.
    """

    enabled: bool = False
    transport: str = "smtp"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_address: str = ""
    to_address: str = ""
    sendmail_path: str = ""
    smtp_tls: bool = True
    smtp_timeout_seconds: float = 20.0
    retry_backoff_seconds: float = 300.0
    max_attempts: int = 5
    freshness_hours: float = 24.0
    bootstrap_enabled: bool = False
    claim_timeout_seconds: float = 300.0
    message_id_domain: str = "argus.invalid"

    def __post_init__(self) -> None:
        if not isinstance(self.transport, str):
            raise ValueError("transport must be text")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("smtp_port must be between 1 and 65535")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        for name in (
            "smtp_timeout_seconds",
            "retry_backoff_seconds",
            "freshness_hours",
            "claim_timeout_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", self.message_id_domain):
            raise ValueError("message_id_domain contains invalid header characters")
        for name in ("from_address", "to_address", "sendmail_path"):
            if any(character in getattr(self, name) for character in "\r\n"):
                raise ValueError(f"{name} contains header/control characters")

    @classmethod
    def from_dict(cls, values: Mapping[str, Any] | None) -> "AlertConfig":
        if values is None:
            return cls.from_env()
        if isinstance(values, cls):
            return values
        if not isinstance(values, Mapping):
            raise TypeError("alert config must be a mapping or AlertConfig")
        aliases = {
            "from": "from_address",
            "to": "to_address",
            "smtp_from": "from_address",
            "smtp_to": "to_address",
            "smtp_starttls": "smtp_tls",
            "backoff_seconds": "retry_backoff_seconds",
        }
        allowed = {
            field for field in cls.__dataclass_fields__  # type: ignore[attr-defined]
        }
        data: dict[str, Any] = {}
        for key, value in values.items():
            target = aliases.get(str(key), str(key))
            if target in allowed:
                data[target] = value
        for key in ("enabled", "smtp_tls", "bootstrap_enabled"):
            if key in data:
                data[key] = _as_bool(data[key])
        for key in ("smtp_port", "max_attempts"):
            if key in data:
                data[key] = int(data[key])
        for key in (
            "smtp_timeout_seconds",
            "retry_backoff_seconds",
            "freshness_hours",
            "claim_timeout_seconds",
        ):
            if key in data:
                data[key] = float(data[key])
        return cls(**data)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AlertConfig":
        """Build config from only the dedicated ``ARGUS_TRACKER_*`` variables."""

        env = os.environ if environ is None else environ

        def get(*names: str, default: str = "") -> str:
            for name in names:
                value = env.get(name)
                if value is not None:
                    return value
            return default

        port = get("ARGUS_TRACKER_SMTP_PORT", default="587")
        return cls.from_dict(
            {
                "enabled": _as_bool(get("ARGUS_TRACKER_ALERTS_ENABLED", default="false")),
                "transport": get("ARGUS_TRACKER_TRANSPORT", default="smtp").strip().lower(),
                "smtp_host": get("ARGUS_TRACKER_SMTP_HOST"),
                "smtp_port": int(port or 587),
                "smtp_user": get("ARGUS_TRACKER_SMTP_USER"),
                "smtp_password": get("ARGUS_TRACKER_SMTP_PASSWORD"),
                "from_address": get("ARGUS_TRACKER_FROM", "ARGUS_TRACKER_SMTP_FROM"),
                "to_address": get("ARGUS_TRACKER_TO", "ARGUS_TRACKER_SMTP_TO"),
                "sendmail_path": get("ARGUS_TRACKER_SENDMAIL_PATH"),
                "smtp_tls": _as_bool(get("ARGUS_TRACKER_SMTP_TLS", default="true")),
            }
        )


def alert_config_from_env(environ: Mapping[str, str] | None = None) -> AlertConfig:
    """Factory alias used by deployment code without hardcoding a recipient."""

    return AlertConfig.from_env(environ)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE or not text:
        return False
    raise ValueError(f"invalid boolean setting: {value!r}")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _now(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str):
        candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        return _utc(datetime.fromisoformat(candidate))
    raise TypeError("now must be a timezone-aware datetime or ISO timestamp")


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        try:
            return _utc(value)
        except ValueError:
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        return _utc(datetime.fromisoformat(candidate))
    except (TypeError, ValueError):
        return None


def _usable_deadline(value: Any) -> str | None:
    if isinstance(value, datetime):
        return _utc(value).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _sanitize_error(value: Any, secret: str = "") -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    if secret:
        text = text.replace(secret, "<redacted>")
    text = re.sub(
        r"(?i)(?:password|passwd|secret|token|api[_ -]?key)\s*[:=]\s*[^\s,;]+",
        "<redacted>",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()[:500]


def _canonical_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.casefold().rstrip(".")
        port = parsed.port
        if port is not None and not (
            (parsed.scheme.casefold() == "http" and port == 80)
            or (parsed.scheme.casefold() == "https" and port == 443)
        ):
            host = f"{host}:{port}"
        path = parsed.path or "/"
        if path != "/":
            path = path.rstrip("/") or "/"
        query = sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
        )
        return urlunsplit((parsed.scheme.casefold(), host, path, urlencode(query), ""))
    except ValueError:
        return None


def _known_ats_identity(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    segments = [part for part in parsed.path.split("/") if part]
    lowered = [part.casefold() for part in segments]
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        if "jobs" in lowered:
            index = lowered.index("jobs")
            if index and index + 1 < len(segments):
                return f"ats:greenhouse:{segments[index - 1].casefold()}:{segments[index + 1].casefold()}"
        query = dict(parse_qsl(parsed.query, keep_blank_values=False))
        tenant = query.get("for", "").strip().casefold()
        requisition = (query.get("gh_jid") or query.get("token") or "").strip().casefold()
        if tenant and requisition:
            return f"ats:greenhouse:{tenant}:{requisition}"
    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and len(segments) >= 2:
        return f"ats:lever:{segments[0].casefold()}:{segments[1].casefold()}"
    if host == "jobs.ashbyhq.com" and len(segments) >= 2:
        return f"ats:ashby:{segments[0].casefold()}:{segments[1].casefold()}"
    if host in {"apply.workable.com", "jobs.workable.com"} and "j" in lowered:
        index = lowered.index("j")
        if index and index + 1 < len(segments):
            return f"ats:workable:{segments[index - 1].casefold()}:{segments[index + 1].casefold()}"
    if host == "jobs.smartrecruiters.com" and len(segments) >= 2:
        return f"ats:smartrecruiters:{segments[0].casefold()}:{segments[1].casefold()}"
    return None


def _role_key(job: Mapping[str, Any]) -> str:
    for field in (
        "identity_key",
        "canonical_identity",
        "role_identity",
        "ats_identity",
        "canonical_ats_id",
        "canonical_id",
    ):
        value = job.get(field)
        if isinstance(value, str) and value.strip():
            return f"identity:{value.strip()}"
    canonical = _canonical_url(job.get("canonical_url") or job.get("url"))
    known = _known_ats_identity(canonical)
    if known:
        # A known ATS identity is stronger than a local row id: two source
        # records can carry different local aliases for the same posting.
        return known
    if job.get("id") is not None and not isinstance(job.get("id"), bool):
        return f"id:{job['id']}"
    if canonical:
        return f"url:{canonical}"
    fallback = json.dumps(
        {key: job.get(key, "") for key in ("employer", "title", "location", "programme")},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "record:" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def _job_snapshot(job: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "id",
        "identity_key",
        "employer",
        "title",
        "url",
        "location",
        "programme",
        "stage",
        "availability",
        "verified_at",
        "verification_error",
        "match_status",
        "match_reasons",
        "match_unknowns",
        "uk_match",
        "deadline",
        "deadline_text",
        "deadline_basis",
        "posted_at",
        "posted_text",
        "sources",
    )
    snapshot: dict[str, Any] = {}
    for field in fields:
        value = job.get(field)
        if value is not None:
            try:
                json.dumps(value)
                snapshot[field] = value
            except (TypeError, ValueError):
                snapshot[field] = str(value)
    return snapshot


def _uk_role(job: Mapping[str, Any]) -> bool:
    if "uk_match" in job:
        return job.get("uk_match") is True
    country = str(job.get("country", "")).casefold().strip()
    if country in {"uk", "gb", "united kingdom", "england", "scotland", "wales", "northern ireland"}:
        return True
    location = str(job.get("location", ""))
    if re.search(r"\b(?:united kingdom|uk|england|scotland|wales|northern ireland)\b", location, re.I):
        return not bool(re.search(r"\b(?:canada|ontario|kentucky|tennessee|massachusetts)\b", location, re.I))
    cities = (
        "london|belfast|edinburgh|glasgow|manchester|birmingham|bristol|leeds|cardiff|"
        "reading|oxford|cambridge|nottingham|sheffield|liverpool|newcastle|southampton|"
        "aberdeen|bournemouth|basingstoke|guildford|watford|leicester|york"
    )
    return bool(re.search(rf"\b(?:{cities})\b", location, re.I)) and not bool(
        re.search(r"\b(?:canada|ontario|kentucky|tennessee|massachusetts)\b", location, re.I)
    )


def _eligibility(job: Mapping[str, Any], when: datetime, freshness_hours: float) -> tuple[bool, str]:
    if str(job.get("availability", "")).casefold() != "open":
        return False, "availability_not_open"
    if job.get("closed") is True:
        return False, "closed"
    if str(job.get("stage", "")) != "not_applied":
        return False, "stage_not_applied_required"
    status = str(job.get("match_status", "") or "").casefold()
    if status not in {"potential", "review"}:
        return False, "match_excluded_or_unknown"
    if not _uk_role(job):
        return False, "not_uk"
    if job.get("verification_error"):
        return False, "verification_error"
    verified = _parse_timestamp(job.get("verified_at"))
    if verified is None:
        return False, "not_verified"
    age = when - verified
    if age < timedelta(0) or age > timedelta(hours=freshness_hours):
        return False, "verification_stale"
    deadline = _usable_deadline(job.get("deadline"))
    if deadline is not None and date.fromisoformat(deadline) < when.date():
        return False, "expired"
    return True, "eligible"


def _event_token(value: str) -> str:
    return value.replace("\r", "").replace("\n", "")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_roles (
    role_key TEXT PRIMARY KEY,
    last_deadline TEXT,
    last_title TEXT NOT NULL DEFAULT '',
    last_url TEXT NOT NULL DEFAULT '',
    eligible_seen INTEGER NOT NULL DEFAULT 0,
    baseline_suppressed INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_events (
    event_key TEXT PRIMARY KEY,
    role_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','uncertain')),
    attempts INTEGER NOT NULL DEFAULT 0,
    retryable INTEGER NOT NULL DEFAULT 1,
    next_attempt_at REAL,
    created_at TEXT NOT NULL,
    created_at_epoch REAL NOT NULL,
    claimed_at TEXT,
    claimed_at_epoch REAL,
    owner_pid INTEGER,
    sent_at TEXT,
    sent_at_epoch REAL,
    last_error TEXT NOT NULL DEFAULT '',
    message_id TEXT,
    delivery_id TEXT,
    provider_delivery_id TEXT,
    batch_key TEXT,
    blocked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alert_events_claim_idx
    ON alert_events(status, retryable, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS alert_events_batch_idx ON alert_events(batch_key);
"""


class SmtpAlertSender:
    """Standard-library SMTP/TLS transport used only after explicit config."""

    def __init__(self, config: AlertConfig):
        self.config = config

    def _email(self, message: AlertMessage) -> EmailMessage:
        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = message.from_address
        email["To"] = message.to_address
        email["Message-ID"] = message.message_id
        email["X-ARGUS-Delivery-ID"] = message.delivery_id
        email.set_content(message.body)
        return email

    def send(self, message: AlertMessage) -> DeliveryReceipt:
        import ssl
        if not self.config.smtp_tls:
            raise ProviderRefusedError('SMTP delivery requires verified TLS')
        email = self._email(message)
        in_message_submission = False
        client = None
        try:
            client = smtplib.SMTP(self.config.smtp_host, self.config.smtp_port,
                                  timeout=self.config.smtp_timeout_seconds)
            client.ehlo()
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
            if self.config.smtp_user:
                client.login(self.config.smtp_user, self.config.smtp_password)
            in_message_submission = True
            refused = client.send_message(email)
            if not isinstance(refused, dict):
                raise DeliveryUncertainError('SMTP returned an invalid acknowledgement')
            if refused:
                raise ProviderRefusedError('SMTP refused the configured recipient')
            # send_message returned the DATA acknowledgement. QUIT is cleanup,
            # not another delivery verdict, and must never cause a resend.
            return DeliveryReceipt(accepted=True, delivery_id=message.delivery_id)
        except (DeliveryUncertainError, ProviderRefusedError):
            raise
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                smtplib.SMTPDataError, smtplib.SMTPAuthenticationError) as exc:
            raise ProviderRefusedError(type(exc).__name__) from exc
        except (smtplib.SMTPException, OSError) as exc:
            if in_message_submission:
                raise DeliveryUncertainError(type(exc).__name__) from exc
            raise ProviderRefusedError(type(exc).__name__) from exc
        finally:
            if client is not None:
                try:
                    client.quit()
                except (smtplib.SMTPException, OSError):
                    pass
                finally:
                    client.close()


class SendmailAlertSender:
    """Configured sendmail relay; a non-zero exit is a known retryable failure."""

    def __init__(self, config: AlertConfig):
        self.config = config

    def send(self, message: AlertMessage) -> DeliveryReceipt:
        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = message.from_address
        email["To"] = message.to_address
        email["Message-ID"] = message.message_id
        email["X-ARGUS-Delivery-ID"] = message.delivery_id
        email.set_content(message.body)
        try:
            result = subprocess.run(
                [self.config.sendmail_path, "-t", "-oi"],
                input=email.as_bytes(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.config.smtp_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeliveryUncertainError("sendmail timed out") from exc
        except OSError as exc:
            raise ProviderRefusedError(str(exc)) from exc
        if result.returncode:
            detail = result.stderr.decode("utf-8", "replace")[:300]
            raise ProviderRefusedError(f"sendmail exit {result.returncode}: {detail}")
        return DeliveryReceipt(accepted=True, delivery_id=message.delivery_id)


class AlertService:
    """Durable, fail-closed email outbox for decorated tracker roles."""

    def __init__(
        self,
        data_dir: Path,
        config: AlertConfig | Mapping[str, Any] | None = None,
        sender: Any = None,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.outbox_path = self.data_dir / "alerts.sqlite3"
        self.config = AlertConfig.from_dict(config)
        self.sender = sender
        self._init_db()
        self.recover_incomplete()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.outbox_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_db(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
        finally:
            connection.close()

    def _configuration(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {"status": "disabled", "blocked": True, "reason": "disabled"}
        transport = self.config.transport.casefold().strip()
        if self.sender is not None:
            return {"status": "ready", "blocked": False, "reason": "injected_sender"}
        if transport == "smtp":
            missing = []
            if not self.config.smtp_host:
                missing.append("smtp_host")
            if not self.config.from_address:
                missing.append("from_address")
            if not self.config.to_address:
                missing.append("to_address")
            if bool(self.config.smtp_user) != bool(self.config.smtp_password):
                missing.append("smtp_user_and_smtp_password_pair")
            if missing:
                return {
                    "status": "blocked_unconfigured",
                    "blocked": True,
                    "reason": "missing_" + ",".join(missing),
                }
            self.sender = SmtpAlertSender(self.config)
            return {"status": "ready", "blocked": False, "reason": "smtp_tls"}
        if transport == "sendmail":
            missing = [
                name
                for name, value in (
                    ("sendmail_path", self.config.sendmail_path),
                    ("from_address", self.config.from_address),
                    ("to_address", self.config.to_address),
                )
                if not value
            ]
            if missing:
                return {
                    "status": "blocked_unconfigured",
                    "blocked": True,
                    "reason": "missing_" + ",".join(missing),
                }
            self.sender = SendmailAlertSender(self.config)
            return {"status": "ready", "blocked": False, "reason": "sendmail"}
        return {
            "status": "blocked_unconfigured",
            "blocked": True,
            "reason": f"unsupported_transport_{transport or 'empty'}",
        }

    @staticmethod
    def _collapse_jobs(jobs: list[Mapping[str, Any]], when: datetime, freshness: float) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for raw in jobs:
            if not isinstance(raw, Mapping):
                raise TypeError("decorated jobs must be mappings")
            item = dict(raw)
            key = _role_key(item)
            existing = result.get(key)
            if existing is None:
                result[key] = item
                continue
            current_ok = _eligibility(item, when, freshness)[0]
            existing_ok = _eligibility(existing, when, freshness)[0]
            current_verified = _parse_timestamp(item.get("verified_at")) or datetime.min.replace(tzinfo=timezone.utc)
            existing_verified = _parse_timestamp(existing.get("verified_at")) or datetime.min.replace(tzinfo=timezone.utc)
            if (current_ok, current_verified, json.dumps(_job_snapshot(item), sort_keys=True)) > (
                existing_ok,
                existing_verified,
                json.dumps(_job_snapshot(existing), sort_keys=True),
            ):
                result[key] = item
        return result

    def _event_exists(self, connection: sqlite3.Connection, event_key: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM alert_events WHERE event_key = ?", (event_key,)
        ).fetchone() is not None

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_key: str,
        role_key: str,
        kind: str,
        payload: dict[str, Any],
        when: datetime,
    ) -> bool:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO alert_events
               (event_key, role_key, kind, payload, status, attempts, retryable,
                created_at, created_at_epoch)
               VALUES (?, ?, ?, ?, 'pending', 0, 1, ?, ?)""",
            (
                _event_token(event_key),
                role_key,
                kind,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                _iso(when),
                when.timestamp(),
            ),
        )
        return cursor.rowcount == 1

    def _prepare_events(
        self,
        jobs: dict[str, dict[str, Any]],
        when: datetime,
        *,
        bootstrap: bool = False,
    ) -> tuple[int, int]:
        created = 0
        baseline_suppressed = 0
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            initialized_row = connection.execute(
                "SELECT value FROM alert_meta WHERE key = 'initialized'"
            ).fetchone()
            initialized = initialized_row is not None and initialized_row["value"] == "1"
            for role_key, job in sorted(jobs.items()):
                eligible, _reason = _eligibility(job, when, self.config.freshness_hours)
                deadline = _usable_deadline(job.get("deadline"))
                previous = connection.execute(
                    "SELECT * FROM alert_roles WHERE role_key = ?", (role_key,)
                ).fetchone()
                was_eligible = bool(previous and previous["eligible_seen"])
                old_deadline = previous["last_deadline"] if previous else None
                baseline = bool(previous and previous["baseline_suppressed"])
                if not initialized:
                    # Suppress the entire initial inventory, not merely the first
                    # enrichment batch. Later-discovered roles are not baseline.
                    baseline = True
                    if eligible:
                        baseline_suppressed += 1
                    eligible_seen = int(eligible or was_eligible)
                else:
                    eligible_seen = int(eligible or was_eligible)
                    if eligible and not baseline:
                        new_key = f"new:{role_key}"
                        if not self._event_exists(connection, new_key):
                            created += int(
                                self._insert_event(
                                    connection,
                                    event_key=new_key,
                                    role_key=role_key,
                                    kind="new",
                                    payload={"job": _job_snapshot(job), "role_key": role_key},
                                    when=when,
                                )
                            )
                    if eligible and was_eligible and deadline is not None and old_deadline != deadline:
                        change_key = f"deadline-change:{role_key}:{old_deadline or 'none'}:{deadline}"
                        created += int(
                            self._insert_event(
                                connection,
                                event_key=change_key,
                                role_key=role_key,
                                kind="deadline_change",
                                payload={
                                    "job": _job_snapshot(job),
                                    "role_key": role_key,
                                    "old_deadline": old_deadline,
                                    "new_deadline": deadline,
                                },
                                when=when,
                            )
                        )
                    if eligible and deadline is not None:
                        days = (date.fromisoformat(deadline) - when.date()).days
                        if days in {7, 2}:
                            reminder_key = f"reminder:{role_key}:{deadline}:{days}:{when.date().isoformat()}"
                            created += int(
                                self._insert_event(
                                    connection,
                                    event_key=reminder_key,
                                    role_key=role_key,
                                    kind="reminder",
                                    payload={
                                        "job": _job_snapshot(job),
                                        "role_key": role_key,
                                        "deadline": deadline,
                                        "days": days,
                                        "reminder_date": when.date().isoformat(),
                                    },
                                    when=when,
                                )
                            )
                stored_deadline = deadline if deadline is not None else old_deadline
                if previous is None:
                    connection.execute(
                        """INSERT INTO alert_roles
                           (role_key, last_deadline, last_title, last_url,
                            eligible_seen, baseline_suppressed, last_seen_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            role_key,
                            stored_deadline,
                            str(job.get("title", "")),
                            str(job.get("url", "")),
                            eligible_seen,
                            int(baseline),
                            _iso(when),
                        ),
                    )
                else:
                    connection.execute(
                        """UPDATE alert_roles SET last_deadline = ?, last_title = ?,
                           last_url = ?, eligible_seen = ?, baseline_suppressed = ?,
                           last_seen_at = ? WHERE role_key = ?""",
                        (
                            stored_deadline,
                            str(job.get("title", "")),
                            str(job.get("url", "")),
                            eligible_seen,
                            int(baseline),
                            _iso(when),
                            role_key,
                        ),
                    )
            if not initialized:
                connection.execute(
                    "INSERT INTO alert_meta(key, value) VALUES ('initialized', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value = '1'"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return created, baseline_suppressed

    def _event_currently_eligible(
        self,
        row: sqlite3.Row,
        jobs: Mapping[str, Mapping[str, Any]],
        when: datetime,
    ) -> bool:
        job = jobs.get(row["role_key"])
        if job is None or not _eligibility(job, when, self.config.freshness_hours)[0]:
            return False
        payload = json.loads(row["payload"])
        kind = row["kind"]
        if kind == "reminder":
            deadline = _usable_deadline(job.get("deadline"))
            return deadline == payload.get("deadline") and (
                date.fromisoformat(deadline) - when.date()
            ).days == int(payload.get("days", -1))
        if kind == "deadline_change":
            return _usable_deadline(job.get("deadline")) == payload.get("new_deadline")
        return True

    def _claim_batch(
        self,
        jobs: Mapping[str, Mapping[str, Any]],
        when: datetime,
    ) -> list[sqlite3.Row]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            while True:
                retry_batch = connection.execute(
                    """SELECT batch_key FROM alert_events
                       WHERE status = 'failed' AND retryable = 1
                         AND attempts < ? AND next_attempt_at <= ?
                         AND batch_key IS NOT NULL
                       ORDER BY created_at_epoch, event_key LIMIT 1""",
                    (self.config.max_attempts, when.timestamp()),
                ).fetchone()
                if retry_batch:
                    rows = connection.execute(
                        "SELECT * FROM alert_events WHERE batch_key = ? ORDER BY event_key",
                        (retry_batch["batch_key"],),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """SELECT * FROM alert_events
                           WHERE (status = 'pending' OR
                                  (status = 'failed' AND retryable = 1
                                   AND attempts < ? AND next_attempt_at <= ?))
                             AND batch_key IS NULL
                           ORDER BY created_at_epoch, event_key""",
                        (self.config.max_attempts, when.timestamp()),
                    ).fetchall()
                if not rows:
                    connection.commit()
                    return []
                selected: list[sqlite3.Row] = []
                roles: set[str] = set()
                for row in rows:
                    if row["role_key"] not in roles and len(roles) >= 20:
                        continue
                    if not self._event_currently_eligible(row, jobs, when):
                        connection.execute(
                            """UPDATE alert_events SET status = 'failed', retryable = 0,
                               blocked = 1, next_attempt_at = NULL,
                               last_error = ? WHERE event_key = ?""",
                            ("blocked: role no longer eligible at send revalidation", row["event_key"]),
                        )
                        continue
                    payload = json.loads(row["payload"])
                    payload["job"] = _job_snapshot(jobs[row["role_key"]])
                    connection.execute("UPDATE alert_events SET payload = ? WHERE event_key = ?",
                                       (json.dumps(payload, sort_keys=True, ensure_ascii=False), row["event_key"]))
                    selected.append(row)
                    roles.add(row["role_key"])
                if not selected:
                    continue
                event_keys = sorted(row["event_key"] for row in selected)
                batch_key = "digest:" + hashlib.sha256(
                    "\n".join(event_keys).encode("utf-8")
                ).hexdigest()
                message_id = "<argus-tracker-" + hashlib.sha256(
                    batch_key.encode("utf-8")
                ).hexdigest() + "@" + self.config.message_id_domain + ">"
                delivery_id = "argus-delivery-" + hashlib.sha256(
                    batch_key.encode("utf-8")
                ).hexdigest()[:24]
                claimed_at = _iso(when)
                for row in selected:
                    connection.execute(
                        """UPDATE alert_events SET status = 'sending', attempts = attempts + 1,
                           claimed_at = ?, claimed_at_epoch = ?, owner_pid = ?,
                           batch_key = ?, message_id = ?, delivery_id = ?, next_attempt_at = NULL
                           WHERE event_key = ? AND status IN ('pending', 'failed')""",
                        (
                            claimed_at,
                            when.timestamp(),
                            os.getpid(),
                            batch_key,
                            message_id,
                            delivery_id,
                            row["event_key"],
                        ),
                    )
                claimed = connection.execute(
                    "SELECT * FROM alert_events WHERE batch_key = ? ORDER BY event_key",
                    (batch_key,),
                ).fetchall()
                connection.commit()
                return claimed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _receipt(result: Any) -> tuple[str, bool, bool, str]:
        if isinstance(result, DeliveryReceipt):
            result = {'accepted': result.accepted, 'delivery_id': result.delivery_id,
                      'uncertain': result.uncertain, 'retryable': result.retryable,
                      'error': result.error}
        if result is False:
            return '', True, False, 'sender refused the message'
        if not isinstance(result, Mapping):
            return '', False, True, 'sender returned no explicit delivery acknowledgement'
        identifier = str(result.get('delivery_id', ''))
        accepted = result.get('accepted')
        error = str(result.get('error', ''))
        if result.get('uncertain') or accepted not in (True, False) or type(accepted) is not bool:
            return identifier, False, True, error or 'sender acknowledgement was uncertain'
        if accepted is False:
            return identifier, True, False, error or 'sender refused the message'
        if error or result.get('retryable'):
            return identifier, False, True, error or 'sender returned contradictory acknowledgement'
        return identifier, False, False, ''

    def _build_message(self, rows: list[sqlite3.Row]) -> AlertMessage:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = json.loads(row["payload"])
            role = dict(payload.get("job") or {})
            role_key = row["role_key"]
            entry = grouped.setdefault(
                role_key,
                {
                    "role_key": role_key,
                    "employer": role.get("employer", ""),
                    "title": role.get("title", ""),
                    "url": role.get("url", ""),
                    "location": role.get("location", ""),
                    "deadline": role.get("deadline", ""),
                    "match_status": role.get("match_status", ""),
                    "alert_kinds": [],
                    "event_keys": [],
                },
            )
            entry["alert_kinds"].append(row["kind"])
            entry["event_keys"].append(row["event_key"])
        roles = tuple(grouped[key] for key in sorted(grouped))
        event_keys = tuple(sorted(row["event_key"] for row in rows))
        message_id = rows[0]["message_id"]
        delivery_id = rows[0]["delivery_id"]
        subject = f"ARGUS tracker digest: {len(roles)} role{'s' if len(roles) != 1 else ''}"
        lines = [
            f"ARGUS tracker digest for {len(roles)} role(s).",
            _DELIVERY_NOTE,
            "",
        ]
        for role in roles:
            lines.append(
                f"- {role['employer']} — {role['title']}"
                + (f" (deadline {role['deadline']})" if role["deadline"] else "")
            )
            if role["url"]:
                lines.append(f"  {role['url']}")
            lines.append(f"  alert: {', '.join(role['alert_kinds'])}")
        return AlertMessage(
            event_keys=event_keys,
            roles=roles,
            subject=subject,
            body="\n".join(lines),
            message_id=message_id,
            delivery_id=delivery_id,
            from_address=self.config.from_address,
            to_address=self.config.to_address,
        )

    def _finish_batch(
        self,
        batch_key: str,
        *,
        status: str,
        when: datetime,
        error: str = "",
        provider_id: str = "",
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if status == "sent":
                connection.execute(
                    """UPDATE alert_events SET status = 'sent', sent_at = ?, sent_at_epoch = ?,
                       retryable = 0, last_error = '', provider_delivery_id = ?
                       WHERE batch_key = ? AND status = 'sending'""",
                    (_iso(when), when.timestamp(), _sanitize_error(provider_id, self.config.smtp_password), batch_key),
                )
            elif status == "failed":
                rows = connection.execute(
                    "SELECT attempts FROM alert_events WHERE batch_key = ? AND status = 'sending'",
                    (batch_key,),
                ).fetchall()
                next_attempt = when.timestamp() + self.config.retry_backoff_seconds * (
                    2 ** max(0, (max((row["attempts"] for row in rows), default=1) - 1))
                )
                retryable = int(max((row["attempts"] for row in rows), default=1) < self.config.max_attempts)
                connection.execute(
                    """UPDATE alert_events SET status = 'failed', retryable = ?,
                       next_attempt_at = ?, last_error = ? WHERE batch_key = ? AND status = 'sending'""",
                    (
                        retryable,
                        next_attempt if retryable else None,
                        _sanitize_error(error, self.config.smtp_password),
                        batch_key,
                    ),
                )
            elif status == "uncertain":
                connection.execute(
                    """UPDATE alert_events SET status = 'uncertain', retryable = 0,
                       next_attempt_at = NULL, last_error = ?
                       WHERE batch_key = ? AND status = 'sending'""",
                    (
                        _sanitize_error(error, self.config.smtp_password)
                        or "delivery outcome uncertain; manual confirmation required",
                        batch_key,
                    ),
                )
            else:
                raise ValueError(status)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _deliver(self, jobs: Mapping[str, Mapping[str, Any]], when: datetime) -> dict[str, Any]:
        claimed = self._claim_batch(jobs, when)
        if not claimed:
            return {
                "claimed": 0,
                "sent": 0,
                "events_sent": 0,
                "failed": 0,
                "events_failed": 0,
                "uncertain": 0,
                "events_uncertain": 0,
                "roles": 0,
            }
        message = self._build_message(claimed)
        try:
            send = getattr(self.sender, "send", None)
            if callable(send):
                result = send(message)
            elif callable(self.sender):
                result = self.sender(message)
            else:
                raise ProviderRefusedError("configured sender is not callable")
            provider_id, retryable, uncertain, error = self._receipt(result)
            if uncertain:
                raise DeliveryUncertainError(error or "sender reported uncertain delivery")
            if retryable:
                raise ProviderRefusedError(error or "sender refused the message")
        except DeliveryUncertainError as exc:
            self._finish_batch(
                claimed[0]["batch_key"],
                status="uncertain",
                when=when,
                error=str(exc),
            )
            return {
                "claimed": len(claimed),
                "sent": 0,
                "events_sent": 0,
                "failed": 0,
                "events_failed": 0,
                "uncertain": 1,
                "events_uncertain": len(claimed),
                "roles": len(message.roles),
            }
        except ProviderRefusedError as exc:
            self._finish_batch(
                claimed[0]["batch_key"],
                status="failed",
                when=when,
                error=str(exc),
            )
            return {
                "claimed": len(claimed),
                "sent": 0,
                "events_sent": 0,
                "failed": 1,
                "events_failed": len(claimed),
                "uncertain": 0,
                "events_uncertain": 0,
                "roles": len(message.roles),
            }
        except Exception as exc:  # Unknown transport phase is conservatively uncertain.
            self._finish_batch(
                claimed[0]["batch_key"],
                status="uncertain",
                when=when,
                error=f"{type(exc).__name__}: {exc}",
            )
            return {
                "claimed": len(claimed),
                "sent": 0,
                "events_sent": 0,
                "failed": 0,
                "events_failed": 0,
                "uncertain": 1,
                "events_uncertain": len(claimed),
                "roles": len(message.roles),
            }
        self._finish_batch(
            claimed[0]["batch_key"],
            status="sent",
            when=when,
            provider_id=provider_id,
        )
        return {
            "claimed": len(claimed),
            "sent": 1,
            "events_sent": len(claimed),
            "failed": 0,
            "events_failed": 0,
            "uncertain": 0,
            "events_uncertain": 0,
            "roles": len(message.roles),
        }

    def run(
        self,
        jobs: list[dict] | tuple[dict, ...],
        *,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Queue eligible changes and make at most one 20-role digest attempt."""

        when = _now(now)
        raw_jobs = list(jobs)
        current = self._collapse_jobs(raw_jobs, when, self.config.freshness_hours)
        config = self._configuration()
        created, baseline_suppressed = self._prepare_events(current, when)
        delivery = {
            "claimed": 0,
            "sent": 0,
            "events_sent": 0,
            "failed": 0,
            "events_failed": 0,
            "uncertain": 0,
            "events_uncertain": 0,
            "roles": 0,
        }
        if not config["blocked"]:
            delivery = self._deliver(current, when)
        status = "blocked" if config["blocked"] else "idle"
        if delivery["sent"]:
            status = "sent"
        elif delivery["uncertain"]:
            status = "uncertain"
        elif delivery["failed"]:
            status = "retry_scheduled"
        summary = self.summary()
        return {
            "status": status,
            "config_status": config["status"],
            "reason": config["reason"],
            "created": created,
            "baseline_suppressed": baseline_suppressed,
            **delivery,
            "pending": summary["pending"],
            "failed_total": summary["failed"],
            "uncertain_total": summary["uncertain"],
        }

    def bootstrap_digest(
        self,
        jobs: list[dict] | tuple[dict, ...],
        *,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Explicitly queue at most ten verified baseline roles; disabled by default."""

        when = _now(now)
        if not self.config.bootstrap_enabled:
            return {"status": "blocked", "reason": "bootstrap_disabled", "sent": 0, "roles": 0}
        config = self._configuration()
        if config["blocked"]:
            return {"status": "blocked", "reason": config["reason"], "sent": 0, "roles": 0}
        current = self._collapse_jobs(list(jobs), when, self.config.freshness_hours)
        eligible = {
            key: item
            for key, item in current.items()
            if _eligibility(item, when, self.config.freshness_hours)[0]
        }
        selected = dict(sorted(eligible.items())[:10])
        if not selected:
            return {"status": "idle", "reason": "no_verified_roles", "sent": 0, "roles": 0}
        connection = self._connect()
        created = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO alert_meta(key, value) VALUES ('initialized', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'"
            )
            for role_key, item in selected.items():
                connection.execute(
                    """INSERT INTO alert_roles
                       (role_key, last_deadline, last_title, last_url,
                        eligible_seen, baseline_suppressed, last_seen_at)
                       VALUES (?, ?, ?, ?, 1, 1, ?)
                       ON CONFLICT(role_key) DO UPDATE SET eligible_seen = 1,
                       baseline_suppressed = 1, last_seen_at = excluded.last_seen_at""",
                    (
                        role_key,
                        _usable_deadline(item.get("deadline")),
                        str(item.get("title", "")),
                        str(item.get("url", "")),
                        _iso(when),
                    ),
                )
                created += int(
                    self._insert_event(
                        connection,
                        event_key=f"bootstrap:{role_key}",
                        role_key=role_key,
                        kind="bootstrap",
                        payload={"job": _job_snapshot(item), "role_key": role_key},
                        when=when,
                    )
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        delivery = self._deliver(selected, when)
        return {
            "status": "sent" if delivery["sent"] else "idle",
            "reason": "explicit_bootstrap",
            "created": created,
            **delivery,
        }

    # A descriptive alias for callers that prefer the plan's wording.
    send_bootstrap_digest = bootstrap_digest

    def recover_incomplete(self, *, force: bool = False) -> int:
        """Mark abandoned claims uncertain; this never queues them for resend."""

        connection = self._connect()
        recovered = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT event_key, owner_pid, claimed_at_epoch FROM alert_events WHERE status = 'sending'"
            ).fetchall()
            now_epoch = datetime.now(timezone.utc).timestamp()
            for row in rows:
                owner_pid = row["owner_pid"]
                owner_dead = False
                if owner_pid and owner_pid != os.getpid():
                    try:
                        os.kill(int(owner_pid), 0)
                    except (OSError, ProcessLookupError, PermissionError):
                        owner_dead = True
                age = now_epoch - float(row["claimed_at_epoch"] or now_epoch)
                stale = self.config.claim_timeout_seconds >= 0 and age >= self.config.claim_timeout_seconds
                if force or owner_dead or (stale and owner_pid != os.getpid()):
                    connection.execute(
                        """UPDATE alert_events SET status = 'uncertain', retryable = 0,
                           next_attempt_at = NULL, last_error = ?
                           WHERE event_key = ? AND status = 'sending'""",
                        (
                            "delivery interrupted before completion; manual confirmation required",
                            row["event_key"],
                        ),
                    )
                    recovered += 1
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return recovered

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM alert_events ORDER BY created_at_epoch DESC, event_key DESC LIMIT ?",
                (limit,),
            ).fetchall()
            result = []
            for row in rows:
                next_attempt = (
                    datetime.fromtimestamp(row["next_attempt_at"], timezone.utc).isoformat()
                    if row["next_attempt_at"] is not None
                    else None
                )
                result.append(
                    {
                        "event_key": row["event_key"],
                        "role_key": row["role_key"],
                        "kind": row["kind"],
                        "status": row["status"],
                        "attempts": row["attempts"],
                        "retryable": bool(row["retryable"]),
                        "blocked": bool(row["blocked"]),
                        "next_attempt_at": next_attempt,
                        "created_at": row["created_at"],
                        "claimed_at": row["claimed_at"],
                        "sent_at": row["sent_at"],
                        "last_error": row["last_error"],
                        "message_id": row["message_id"],
                        "delivery_id": row["delivery_id"],
                        "provider_delivery_id": row["provider_delivery_id"],
                        "inbox_arrival_proven": False,
                        "delivery_note": _DELIVERY_NOTE,
                    }
                )
            return result
        finally:
            connection.close()

    def summary(self) -> dict[str, Any]:
        config = self._configuration()
        connection = self._connect()
        try:
            counts = {
                status: connection.execute(
                    "SELECT COUNT(*) FROM alert_events WHERE status = ?", (status,)
                ).fetchone()[0]
                for status in _EVENT_STATUSES
            }
            error_row = connection.execute(
                """SELECT last_error FROM alert_events
                   WHERE last_error <> '' ORDER BY created_at_epoch DESC LIMIT 1"""
            ).fetchone()
            return {
                "config_status": config["status"],
                "blocked": bool(config["blocked"]),
                "config_reason": config["reason"],
                "pending": counts["pending"],
                "sending": counts["sending"],
                "sent": counts["sent"],
                "failed": counts["failed"],
                "uncertain": counts["uncertain"],
                "last_error": error_row["last_error"] if error_row else "",
                "success": (
                    config["status"] == "ready"
                    and not counts["pending"]
                    and not counts["uncertain"]
                    and not counts["failed"]
                ),
                "inbox_arrival_proven": False,
                "delivery_note": _DELIVERY_NOTE,
            }
        finally:
            connection.close()


def create_alert_service(
    data_dir: Path,
    *,
    config: AlertConfig | Mapping[str, Any] | None = None,
    sender: Any = None,
) -> AlertService:
    """Factory kept explicit so the parent can choose env or injected config."""

    return AlertService(data_dir, config=config, sender=sender)


__all__ = [
    "AlertConfig",
    "AlertMessage",
    "AlertService",
    "AlertDeliveryError",
    "DeliveryReceipt",
    "DeliveryUncertainError",
    "ProviderRefusedError",
    "RetryableDeliveryError",
    "UncertainDeliveryError",
    "SmtpAlertSender",
    "SendmailAlertSender",
    "alert_config_from_env",
    "create_alert_service",
]
