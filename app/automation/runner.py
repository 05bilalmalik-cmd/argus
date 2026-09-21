from __future__ import annotations

import ipaddress
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.adapters.registry import AdapterRegistry
from app.automation.classifier import Classifier, CompositeClassifier, DeterministicClassifier
from app.automation.host_policy import (
    BlockedRequestImpact,
    authorize_captcha_request,
    canonical_first_party_service_evidence_path,
    classify_blocked_request_impact,
    host_matches_allowlist,
    origin_for_url,
    safe_public_navigation_url,
    safe_public_network_url,
)
from app.automation.receipts import (
    ReceiptEvidence,
    parse_receipt_text,
    receipt_is_correlated,
)
from app.automation.targets import SubmissionTarget, TargetResolution
from app.automation.types import (
    AutomationOutcome,
    FieldAction,
    FillPlan,
    InspectedField,
    Receipt,
    ResolvedFieldValue,
    RunMode,
    SessionState,
)
from app.config import Settings
from app.db import Database
from app.domain.questions import CanonicalKey, Sensitivity
from app.domain.risk import RiskFinding, calculate_risk
from app.domain.states import ApplicationState, validate_transition
from app.domain.targets import TargetKind
from app.models import (
    AnswerEntry,
    Application,
    AutomationRun,
    Document,
    Opportunity,
    QuestionRecord,
    SubmissionIntent,
)
from app.scouting.programmes import ProgrammeFraming, resolve_programme_framing
from app.security.audit import AuditInput, append_audit
from app.security.crypto import CryptoBox
from app.services.answers import AnswerService
from app.services.documents import DocumentService
from app.services.notifications import (
    HumanAttentionEvent,
    NotificationService,
    install_application_notification_observer,
    queue_application_notification,
    runner_owned_navigator_summary,
    select_human_attention_reason,
)
from app.services.profile import ProfileService
from app.services.prefill import PrefillBlocked, row_application_url_allowlist
from app.services.submission_authority import SubmissionAuthorityError, SubmissionAuthorityService

LOGGER = logging.getLogger(__name__)


class SubmissionBlocked(RuntimeError):
    def __init__(self, message: str, *, code: str = "submission_guard"):
        super().__init__(message)
        self.code = code


_DEFAULT_JOURNEY_WAIT_SECONDS = 300
_MIN_JOURNEY_WAIT_SECONDS = 30
_MAX_JOURNEY_WAIT_SECONDS = 900


def _journey_wait_seconds() -> int:
    """How long the synchronous caller waits for the owner thread's real result.

    This was a hardcoded 60s, which silently capped what ARGUS could deliver.
    A journey that finishes inside the budget publishes confirmed readbacks and
    a screenshot; one that overruns falls back to the interim handoff result,
    whose fields are all still at their pre-confirmation default.  Measured on
    real Greenhouse forms: a 9-field form finished in 52-56s and reported 8
    confirmed fills, while 19-, 20- and 28-field forms all hit exactly 60.0s
    and reported zero.  The cap, not the filler, was the reason larger forms
    came back empty.

    This is a patience budget, not a safety boundary -- overrunning it never
    permitted anything, it only discarded a result that was still being
    computed.  Raising the default lets a realistic form finish; the interim
    handoff still catches a genuinely stuck provider page.
    """

    raw = str(os.environ.get("ARGUS_JOURNEY_WAIT_SECONDS", "")).strip()
    if not raw.isdigit():
        return _DEFAULT_JOURNEY_WAIT_SECONDS
    return max(_MIN_JOURNEY_WAIT_SECONDS, min(_MAX_JOURNEY_WAIT_SECONDS, int(raw)))


def _active_handoff_result(snapshot: Any, *, headed: bool) -> dict[str, object] | None:
    """Keep a live headed browser addressable when the request wait expires.

    A slow provider page can outlive the synchronous API request even though
    its owner thread is still alive.  Treating that empty journey result as a
    terminal failure would close the only browser the candidate can inspect.
    This narrow conversion is limited to a headed, non-terminal owner; the
    owner remains authoritative and may later publish its full result.
    """

    if not headed or snapshot is None or not bool(getattr(snapshot, "worker_alive", False)):
        return None
    if getattr(snapshot, "state", None) not in {
        SessionState.ACTIVE,
        SessionState.HUMAN_REQUIRED,
    }:
        return None
    return {
        "state": "NEEDS_USER",
        "reason": "Headed browser handoff remains active after the request wait",
        "risk_level": 3,
        "blocked_reasons": ("human_boundary", "handoff_pending"),
        "human_boundary": dict(getattr(snapshot, "human_boundary", {}) or {}),
    }


# In-process claim guard: prevents two concurrent run() calls for the SAME
# application (e.g. scheduler sweep + manual API submit) from both passing the
# already-submitted check.  Cross-process protection comes from the existing
# runtime lock + sweep lock; this closes the in-process race only.
_CLAIM_GUARD = threading.Lock()
_ACTIVE_CLAIMS: set[str] = set()

_BLOCKED_REQUEST_AUDIT_FIELDS = (
    "host",
    "path",
    "method",
    "resource_type",
    "initiator",
    "is_navigation_request",
    "classification",
    "origin",
    "fatal",
    "record_only",
    "reason",
    "impact",
    "impact_reason",
)

_FIRST_PARTY_REQUEST_AUDIT_FIELDS = (
    "host",
    "path",
    "method",
    "resource_type",
    "manifest_vendor",
    "manifest_permission",
    "delivery",
    "carries_candidate_data",
    "candidate_data_kind",
)

_CAPTCHA_REQUEST_AUDIT_FIELDS = (
    "host",
    "path",
    "method",
    "resource_type",
    "captcha_provider",
    "captcha_permission",
    "delivery",
    "carries_candidate_data",
)
def _query_free_evidence_url(value: object) -> str:
    """Return an HTTP(S) origin/path with credentials, query and fragment removed."""

    try:
        parsed = urlparse(str(value or ""))
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname.casefold().rstrip(".")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
        authority = f"{host}:{port}" if port is not None else host
        return f"{parsed.scheme.casefold()}://{authority}{parsed.path or '/'}"
    except (TypeError, ValueError):
        return ""


def _query_free_blocked_requests(
    value: object,
    *,
    strict_first_party_paths: bool = False,
) -> list[dict[str, object]]:
    """Whitelist bounded blocked-request fields for durable audit evidence."""

    if not isinstance(value, (list, tuple)):
        return []
    output: list[dict[str, object]] = []
    for raw in value[:200]:
        if not isinstance(raw, Mapping):
            continue
        record = {key: raw.get(key) for key in _BLOCKED_REQUEST_AUDIT_FIELDS}
        record["host"] = str(record.get("host") or "").casefold().rstrip(".")[:253]
        path = str(record.get("path") or "").split("?", 1)[0].split("#", 1)[0]
        if strict_first_party_paths:
            canonical_service_path = canonical_first_party_service_evidence_path(
                host=str(record.get("host") or ""),
                path=path,
                resource_type=str(record.get("resource_type") or ""),
            )
            if canonical_service_path is not None:
                path = canonical_service_path
        record["path"] = path[:2048]
        record["initiator"] = _query_free_evidence_url(record.get("initiator"))
        origin = _query_free_evidence_url(record.get("origin"))
        if origin.endswith("/"):
            origin = origin[:-1]
        record["origin"] = origin
        for key in (
            "method",
            "resource_type",
            "classification",
            "reason",
            "impact",
            "impact_reason",
        ):
            record[key] = str(record.get(key) or "")[:240]
        for key in ("is_navigation_request", "fatal", "record_only"):
            item = record.get(key)
            record[key] = item if isinstance(item, bool) else None
        output.append(record)
    return output


def _measured_first_party_path(
    path: str,
    *,
    resource_type: str,
    candidate_data_kind: str,
) -> bool:
    """Match only paths measured for the canonical manifest endpoint type."""

    if candidate_data_kind == "email":
        return path == "/address/validate"
    if candidate_data_kind == "location":
        return path == "/v1/autocomplete"
    if resource_type == "fetch":
        return path == "/users" + "/self"
    if resource_type == "image":
        return path == "/assets/flags-a2kmUSbF.webp"
    return False


def _query_free_first_party_requests(
    value: object,
    *,
    mode: RunMode,
    current_page_url: str,
    resolved_vendor: str | None = None,
) -> list[dict[str, object]] | None:
    """Re-authorize PREFILL evidence against the canonical measured contract."""

    if mode is not RunMode.PREFILL or not isinstance(value, (list, tuple)):
        return None
    output: list[dict[str, object]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        manifest_permission = raw.get("manifest_permission")
        carries_candidate_data = raw.get("carries_candidate_data")
        if manifest_permission is not True or type(carries_candidate_data) is not bool:
            continue
        string_fields = {
            key: raw.get(key)
            for key in (
                "host",
                "path",
                "method",
                "resource_type",
                "manifest_vendor",
                "delivery",
                "candidate_data_kind",
            )
        }
        if not all(isinstance(item, str) for item in string_fields.values()):
            continue
        host = string_fields["host"]
        path = string_fields["path"]
        method = string_fields["method"]
        resource_type = string_fields["resource_type"]
        manifest_vendor = string_fields["manifest_vendor"]
        delivery = string_fields["delivery"]
        candidate_data_kind = string_fields["candidate_data_kind"]

        if method not in {"GET", "HEAD"} or delivery != "blocked":
            continue
        if resolved_vendor is not None and manifest_vendor != resolved_vendor:
            continue
        if not _measured_first_party_path(
            path,
            resource_type=resource_type,
            candidate_data_kind=candidate_data_kind,
        ):
            continue
        decision = classify_blocked_request_impact(
            url=f"https://{host}{path}",
            method=method,
            resource_type=resource_type,
            is_navigation_request=False,
            mode=mode.value,
            policy_classification=(
                "data_bearing" if carries_candidate_data else "other"
            ),
            resolved_vendor=manifest_vendor,
            current_page_url=current_page_url,
        )
        if decision.impact is not BlockedRequestImpact.FIRST_PARTY_SERVICE:
            continue
        if decision.carries_candidate_data is not carries_candidate_data:
            continue
        if (decision.candidate_data_kind or "") != candidate_data_kind:
            continue

        record = {
            "host": host,
            "path": path,
            "method": method,
            "resource_type": resource_type,
            "manifest_vendor": manifest_vendor,
            "manifest_permission": manifest_permission,
            "delivery": delivery,
            "carries_candidate_data": carries_candidate_data,
            "candidate_data_kind": candidate_data_kind,
        }
        output.append({key: record[key] for key in _FIRST_PARTY_REQUEST_AUDIT_FIELDS})
    return output


def _query_free_captcha_requests(
    value: object,
    *,
    mode: RunMode,
    current_page_url: str,
    resolved_vendor: str | None = None,
) -> list[dict[str, object]] | None:
    """Re-authorize permitted CAPTCHA evidence before durable audit storage."""

    if mode is not RunMode.PREFILL or not isinstance(value, (list, tuple)):
        return None
    output: list[dict[str, object]] = []
    for raw in value[:200]:
        if not isinstance(raw, Mapping):
            continue
        if (
            raw.get("captcha_provider") is not True
            or raw.get("captcha_permission") is not True
            or raw.get("delivery") != "permitted"
            or raw.get("carries_candidate_data") is not False
        ):
            continue
        fields_value = {
            key: raw.get(key)
            for key in ("host", "path", "method", "resource_type")
        }
        if not all(isinstance(item, str) for item in fields_value.values()):
            continue
        host = str(fields_value["host"]).casefold().rstrip(".")
        raw_path = str(fields_value["path"])
        if "?" in raw_path or "#" in raw_path:
            continue
        path = raw_path or "/"
        method = str(fields_value["method"]).upper()
        resource_type = str(fields_value["resource_type"]).casefold()
        decision = authorize_captcha_request(
            url=f"https://{host}{path}",
            method=method,
            resource_type=resource_type,
            is_navigation_request=False,
            is_main_frame_navigation=False,
            mode=mode.value,
            resolved_vendor=resolved_vendor,
            current_page_url=current_page_url,
            carries_candidate_data=False,
        )
        if not decision.allowed:
            continue
        record = {
            "host": decision.host,
            "path": decision.path,
            "method": decision.method,
            "resource_type": decision.resource_type,
            "captcha_provider": True,
            "captcha_permission": True,
            "delivery": "permitted",
            "carries_candidate_data": False,
        }
        output.append({key: record[key] for key in _CAPTCHA_REQUEST_AUDIT_FIELDS})
    return output


class AnswerLookup(Protocol):
    def __call__(self, canonical_key: str, label: str) -> object | None: ...


class DocumentLookup(Protocol):
    def __call__(self, canonical_key: str) -> object | None: ...


def _is_loopback(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_builtin_lab_url(settings: Settings, url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not hostname or not _is_loopback(hostname):
        return False
    try:
        port = parsed.port or (80 if parsed.scheme.casefold() == "http" else 443)
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "http"
        and port == settings.port
        and parsed.path.startswith("/lab/ats/")
    )


def _is_builtin_lab_origin(settings: Settings, url: str) -> bool:
    """Allow lab assets/API requests on the configured loopback origin."""

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not hostname or not _is_loopback(hostname):
        return False
    try:
        port = parsed.port or (80 if parsed.scheme.casefold() == "http" else 443)
    except ValueError:
        return False
    return parsed.scheme.casefold() == "http" and port == settings.port


def _domain_matches_allowlist(hostname: str, allowlist: frozenset[str]) -> bool:
    """Compatibility wrapper around the shared exact-host policy."""

    return host_matches_allowlist(hostname, allowlist)


def assert_field_fill_allowed(settings: Settings, url: str) -> None:
    if _is_builtin_lab_url(settings, url):
        return
    parsed = urlparse(url)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise SubmissionBlocked(
            "Destination is not a web URL for field filling",
            code="field_fill_scheme_not_trusted",
        )
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if hostname and not _is_loopback(hostname) and _domain_matches_allowlist(
        hostname, settings.live_domain_allowlist
    ) and safe_public_network_url(url):
        return
    raise SubmissionBlocked(
        "Destination is not trusted for field filling", code="field_fill_domain_not_trusted"
    )


def assert_navigation_allowed(settings: Settings, url: str) -> None:
    """Guard the initial review navigation without requiring field trust."""

    if _is_builtin_lab_url(settings, url):
        return
    if safe_public_navigation_url(url):
        return
    raise SubmissionBlocked(
        "Destination is not safe public HTTPS navigation", code="navigation_guard"
    )


def assert_submission_allowed(settings: Settings, url: str, risk_level: int) -> None:
    if risk_level != 0:
        raise SubmissionBlocked("Automatic submission requires risk level 0")

    parsed = urlparse(url)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise SubmissionBlocked("Submission URL must use HTTP or HTTPS", code="unsafe_scheme")
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not hostname:
        raise SubmissionBlocked("Submission URL does not contain a hostname")

    if _is_loopback(hostname):
        if _is_builtin_lab_url(settings, url):
            return
        raise SubmissionBlocked(
            "Loopback submission is restricted to the built-in ATS laboratory",
            code="loopback_not_lab",
        )

    if not settings.live_submit_enabled:
        raise SubmissionBlocked("Live submission is disabled")
    if not _domain_matches_allowlist(hostname, settings.live_domain_allowlist):
        raise SubmissionBlocked(f"Submission domain {hostname!r} is not allowlisted")
    if not safe_public_network_url(url):
        raise SubmissionBlocked(
            "Submission destination does not resolve to a public address",
            code="dns_guard",
        )


def _network_destination_allowed(settings: Settings, url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.username or parsed.password:
        return False
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not hostname:
        return parsed.scheme.casefold() in {"about", "blob", "data"}
    if parsed.scheme.casefold() not in {"http", "https", "ws", "wss"}:
        return False
    if _is_loopback(hostname):
        try:
            port = parsed.port or (80 if parsed.scheme.casefold() == "http" else 443)
        except ValueError:
            return False
        return parsed.scheme.casefold() == "http" and port == settings.port
    if not safe_public_network_url(url):
        return False
    return settings.live_submit_enabled and _domain_matches_allowlist(
        hostname, settings.live_domain_allowlist
    )


def _origin_key(url: str) -> tuple[str, str, int] | None:
    """Return the browser origin key for an HTTP(S)/WebSocket URL."""

    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.casefold()
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        if scheme not in {"http", "https", "ws", "wss"} or not hostname:
            return None
        port = parsed.port or (443 if scheme in {"https", "wss"} else 80)
    except ValueError:
        return None
    family = "secure" if scheme in {"https", "wss"} else "plain"
    return family, hostname, port


def _canonical_request_url(url: str) -> str:
    """Canonicalize an HTTP request URL without weakening path binding."""

    try:
        parsed = urlparse(url)
        origin = origin_for_url(url)
    except (TypeError, ValueError):
        return ""
    path = parsed.path.rstrip("/") or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{origin}{path}{query}"


def _trusted_network_request_allowed(
    settings: Settings,
    request_url: str,
    page_url: str,
) -> bool:
    """Allow only same-origin or explicitly trusted requests after field trust.

    Review navigation starts from a public URL that may not yet be trusted.
    Once approved values can be filled, every network request must stay on the
    current origin or use an explicitly configured live-domain host.
    """

    lowered = request_url.casefold()
    if lowered.startswith(("about:", "blob:", "data:")):
        return True
    try:
        parsed = urlparse(request_url)
    except ValueError:
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.scheme.casefold() not in {"http", "https", "ws", "wss"}:
        return False

    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not hostname:
        return False
    if _is_builtin_lab_origin(settings, request_url):
        return True
    if _is_loopback(hostname):
        return False
    if not safe_public_network_url(request_url):
        return False

    request_origin = _origin_key(request_url)
    page_origin = _origin_key(page_url)
    if request_origin is not None and request_origin == page_origin:
        return True
    return _domain_matches_allowlist(hostname, settings.live_domain_allowlist)


def _coerce_resolved(value: object | None) -> ResolvedFieldValue | None:
    if value is None:
        return None
    if isinstance(value, ResolvedFieldValue):
        return value
    raw = getattr(value, "value", None)
    source = getattr(value, "source", None)
    if raw is not None and source:
        return ResolvedFieldValue(
            str(raw),
            str(source),
            bool(getattr(value, "sensitive", False)),
        )
    if isinstance(value, tuple) and len(value) >= 2:
        return ResolvedFieldValue(str(value[0]), str(value[1]), bool(value[2]) if len(value) > 2 else False)
    return ResolvedFieldValue(str(value), "resolved_value", False)


def _degree_level_option(value: object, field: InspectedField) -> str | None:
    """Translate a stored full degree title to one unique form option."""

    signal = " ".join(
        (field.question.label, field.question.name, field.question.placeholder)
    ).casefold()
    if "degree" not in signal and "pursu" not in signal:
        return None
    candidate = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    if not candidate:
        return None
    levels = (
        ("bachelor", ("bachelor", "bsc")),
        ("master", ("master", "msc")),
        ("doctor", ("doctor", "phd")),
    )
    tokens = next(
        (tokens for _level, tokens in levels if any(token in candidate for token in tokens)),
        (),
    )
    if not tokens:
        return None
    matches = [
        option
        for option in field.question.options
        if any(token in re.sub(r"[^a-z0-9]+", " ", option.casefold()) for token in tokens)
    ]
    if len(matches) == 1:
        return matches[0]
    # When the option list distinguishes a degree type from a shorter
    # category label, prefer the fully labelled degree option only when it is
    # unique.  Ambiguous lists remain unselected and fail closed downstream.
    labelled = [option for option in matches if "degree" in option.casefold()]
    return labelled[0] if len(labelled) == 1 else None


def _normalise_option_value(value: object, field: InspectedField) -> str:
    if isinstance(value, bool):
        preferred = "Yes" if value else "No"
        for option in field.question.options:
            if option.casefold() == preferred.casefold():
                return option
        return preferred
    text = str(value)
    for option in field.question.options:
        if option.casefold() == text.casefold():
            return option
    degree_option = _degree_level_option(value, field)
    if degree_option is not None:
        return degree_option
    return text


def _field_control_kind(field: InspectedField) -> str:
    """Infer a control's semantic kind without consulting its mapping.

    Labels, provider names, placeholders, and native control types are the
    independent evidence boundary.  The classifier's canonical key is
    deliberately absent: a mistaken mapping must not validate its own value.
    """

    question = field.question
    signal = " ".join(
        (
            question.label,
            question.name,
            question.placeholder,
        )
    ).casefold()
    words = " ".join(re.findall(r"[a-z0-9]+", signal))
    control_types = {
        str(question.field_type or "").casefold(),
        str(field.control_type or "").casefold(),
    }

    if (
        re.search(r"\b(?:gpa|grade|grades)\b", words)
        or "degree classification" in words
        or "academic classification" in words
    ):
        return "grade"
    study_level = any(
        phrase in words
        for phrase in (
            "year of study",
            "study year",
            "academic year",
            "school year",
            "year in school",
            "year in university",
            "year at university",
        )
    )
    explicit_date_language = bool(
        re.search(r"\b(?:date|month|end|graduat\w*|complet\w*|finish\w*)\b", words)
    )
    calendar_year_language = bool(re.search(r"\byear\b", words)) and not study_level
    if (
        control_types.intersection({"date", "datetime", "datetime-local", "month"})
        or explicit_date_language
        or calendar_year_language
    ):
        return "date"
    if study_level:
        return "study_level"
    if "email" in control_types or re.search(r"\be mail\b|\bemail\b", words):
        return "email"
    if "tel" in control_types or re.search(r"\b(?:phone|telephone|mobile)\b", words):
        return "phone"
    if "url" in control_types or re.search(
        r"\b(?:url|website|portfolio|linkedin|github)\b", words
    ):
        return "url"
    if re.search(r"\b(?:university|institution|college|school)\b", words):
        return "university"
    if (
        "field of study" in words
        or re.search(r"\b(?:discipline|subject|course|major|specialism)\b", words)
    ):
        return "discipline"
    if re.search(r"\b(?:degree|qualification)\b", words):
        return "degree"
    if (
        re.search(r"\b(?:first|given|forename|last|family|surname|full|legal|preferred) name\b", words)
        or words in {"name", "candidate name"}
        or question.name.casefold() in {
            "first_name",
            "firstname",
            "last_name",
            "lastname",
            "full_name",
            "fullname",
        }
    ):
        return "name"
    return "unknown"


def _known_profile_categories(
    value: str,
    profile_values: Mapping[str, object],
) -> set[str]:
    target = value.strip().casefold()
    if not target:
        return set()
    categories: set[str] = set()
    for key, candidate in profile_values.items():
        if candidate is None or str(candidate).strip().casefold() != target:
            continue
        key_words = str(key).casefold().replace("-", "_")
        if key_words.startswith("identity.") or any(
            marker in key_words
            for marker in ("first_name", "last_name", "full_name", "preferred_name")
        ):
            categories.add("name")
        if any(marker in key_words for marker in ("university", "institution", "college", "school")):
            categories.add("university")
        if "degree" in key_words and not any(
            marker in key_words for marker in ("grade", "classification")
        ):
            categories.add("degree")
        if any(
            marker in key_words
            for marker in ("discipline", "field_of_study", "subject", "course", "major")
        ):
            categories.add("discipline")
        if any(marker in key_words for marker in ("date", "year", "month")):
            categories.add("date")
        if "email" in key_words:
            categories.add("email")
        if any(marker in key_words for marker in ("phone", "telephone", "mobile")):
            categories.add("phone")
        if any(marker in key_words for marker in ("linkedin", "url", "website", "portfolio")):
            categories.add("url")
    return categories


def _plausible_grade(value: str) -> bool:
    text = value.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d+)?%", text):
        return 0 <= float(text[:-1]) <= 100
    if re.fullmatch(r"\d{1,3}(?:\.\d+)?", text):
        return 0 <= float(text) <= 100
    if re.fullmatch(r"[12]:[12]", text):
        return True
    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    return normalized in {
        "first",
        "first class",
        "first class honours",
        "first class honors",
        "upper second",
        "upper second class",
        "lower second",
        "lower second class",
        "distinction",
        "merit",
        "pass",
    }


def _plausible_date(value: str) -> bool:
    text = value.strip()
    if re.fullmatch(r"\d{4}", text):
        return 1900 <= int(text) <= 2100
    for date_format in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%Y-%m",
        "%m/%Y",
        "%B %Y",
        "%b %Y",
    ):
        try:
            parsed = datetime.strptime(text, date_format)
        except ValueError:
            continue
        return 1900 <= parsed.year <= 2100
    return False


def _is_native_month_control(field: InspectedField) -> bool:
    """Detect a native ``type=month`` control without consulting its mapping.

    A year-only value (``2028``) is plausible as a calendar year but the
    browser month widget only accepts ``YYYY-MM``; passing anything else
    throws at fill time. This gate runs BEFORE the adapter fill so the
    pipeline escalates instead of attempting the fill.
    """
    return "month" in {
        str(field.question.field_type or "").casefold(),
        str(field.control_type or "").casefold(),
    }


_MONTH_VALUE_RE = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])")


def _authoritative_graduation_year(profile_values: Mapping[str, object]) -> int | None:
    """Require agreeing canonical evidence; a programme tier only constrains it."""
    years: set[int] = set()
    for key in (
        CanonicalKey.GRADUATION_YEAR.value,
        CanonicalKey.EDUCATION_END_YEAR.value,
    ):
        candidate = profile_values.get(key)
        if candidate is None:
            continue
        text = str(candidate).strip()
        if re.fullmatch(r"[0-9]{4}", text):
            year = int(text)
        elif (month_match := _MONTH_VALUE_RE.fullmatch(text)) is not None:
            year = int(month_match.group(1))
        else:
            return None
        if not 1900 <= year <= 2100:
            return None
        years.add(year)
    if len(years) != 1:
        return None
    year = next(iter(years))
    tier = profile_values.get("guard.programme_graduation_tier")
    if tier is not None:
        tier_text = str(tier).strip()
        if not re.fullmatch(r"[0-9]{4}", tier_text) or int(tier_text) != year:
            return None
    return year


def _plausible_study_level(value: str) -> bool:
    """Accept only the closed, common study-level forms used by the guard."""

    normalized = " ".join(value.strip().casefold().split())
    return normalized in {
        "year 1",
        "year 2",
        "year 3",
        "year 4",
        "year 5",
        "1st year",
        "2nd year",
        "3rd year",
        "4th year",
        "5th year",
        "first year",
        "second year",
        "third year",
        "fourth year",
        "fifth year",
        "final year",
        "penultimate year",
    }


def _plausible_name(value: str) -> bool:
    text = value.strip()
    return (
        1 <= len(text) <= 120
        and any(character.isalpha() for character in text)
        and all(
            character.isalpha() or character in " -'.\N{RIGHT SINGLE QUOTATION MARK}\N{MODIFIER LETTER APOSTROPHE}"
            for character in text
        )
    )


def _plausible_email(value: str) -> bool:
    text = value.strip()
    if len(text) > 254 or text.count("@") != 1:
        return False
    local, domain = text.rsplit("@", 1)
    if (
        not local
        or len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
    ):
        return False
    labels = domain.split(".")
    return len(labels) >= 2 and all(
        label
        and len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and bool(re.fullmatch(r"[A-Za-z0-9-]+", label))
        for label in labels
    )


def _plausible_phone(value: str) -> bool:
    text = value.strip()
    if not re.fullmatch(r"\+?[0-9][0-9\s().-]{5,}[0-9]", text):
        return False
    digits = "".join(character for character in text if character.isdigit())
    return 7 <= len(digits) <= 15 and len(set(digits)) > 1


def _whatwg_ipv4_number_component(label: str) -> bool:
    """Recognise decimal, leading-zero octal, and ``0x`` IPv4 numbers."""

    lowered = label.casefold()
    if lowered.startswith("0x"):
        return bool(re.fullmatch(r"0x[0-9a-f]+", lowered))
    if len(lowered) >= 2 and lowered.startswith("0"):
        return bool(re.fullmatch(r"0[0-7]+", lowered))
    return bool(re.fullmatch(r"[0-9]+", lowered))


def _plausible_public_dns_host(host: str) -> bool:
    if not host or len(host) > 253 or "." not in host:
        return False
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    labels = host.split(".")
    if all(label.isdigit() for label in labels) or all(
        _whatwg_ipv4_number_component(label) for label in labels
    ):
        return False
    if labels[-1].casefold() in {
        "corp",
        "home",
        "internal",
        "lan",
        "local",
        "localdomain",
        "localhost",
        "onion",
    }:
        return False
    return all(
        1 <= len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and bool(re.fullmatch(r"[A-Za-z0-9-]+", label))
        for label in labels
    )


def _plausible_url(value: str) -> bool:
    text = value.strip()
    if any(character.isspace() for character in text):
        return False
    try:
        parsed = urlparse(text)
        parsed_port = parsed.port
        host = parsed.hostname or ""
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and parsed.username is None
        and parsed.password is None
        and (parsed_port is None or 1 <= parsed_port <= 65535)
        and _plausible_public_dns_host(host)
    )


def _resolved_value_is_plausible(
    value: str,
    field: InspectedField,
    profile_values: Mapping[str, object],
) -> tuple[bool, str]:
    kind = _field_control_kind(field)
    options = field.question.options
    if options and not any(value.casefold() == option.casefold() for option in options):
        return False, "option"

    categories = _known_profile_categories(value, profile_values)
    if kind == "grade":
        if categories.intersection({"degree", "discipline", "university", "name"}):
            return False, kind
        return _plausible_grade(value), kind
    if kind == "date":
        if categories.intersection(
            {"name", "university", "degree", "discipline", "email", "phone", "url"}
        ):
            return False, kind
        return _plausible_date(value), kind
    if kind == "study_level":
        if options:
            return True, kind
        if categories.intersection(
            {"name", "university", "degree", "discipline", "date", "email", "phone", "url"}
        ):
            return False, kind
        return _plausible_study_level(value), kind
    if kind == "name":
        if categories.intersection(
            {"date", "email", "phone", "url", "degree", "discipline", "university"}
        ):
            return False, kind
        return _plausible_name(value), kind
    if kind == "email":
        return _plausible_email(value), kind
    if kind == "phone":
        return _plausible_phone(value), kind
    if kind == "url":
        return _plausible_url(value), kind
    if kind == "university":
        if categories.intersection({"name", "date", "degree", "discipline", "email", "phone", "url"}):
            return False, kind
        return bool(value.strip()), kind
    if kind == "degree":
        if categories.intersection({"name", "date", "university", "email", "phone", "url"}):
            return False, kind
        return bool(value.strip()), kind
    if kind == "discipline":
        if categories.intersection({"name", "date", "university", "degree", "email", "phone", "url"}):
            return False, kind
        return bool(value.strip()), kind
    return True, kind


_PROGRAMME_GRADUATION_DERIVED_KEYS = frozenset(
    {
        CanonicalKey.GRADUATION_YEAR.value,
        CanonicalKey.EDUCATION_END_MONTH.value,
        CanonicalKey.EDUCATION_END_YEAR.value,
    }
)

# These fields are factual profile projections.  An answer-bank callback is
# intentionally not consulted when the stored profile has no value: a
# free-form answer that merely looks plausible is not evidence for a
# candidate's education, country, or public handle.
_PROFILE_EVIDENCE_ONLY_KEYS = frozenset(
    {
        CanonicalKey.COUNTRY,
        CanonicalKey.UNIVERSITY,
        CanonicalKey.DEGREE,
        CanonicalKey.STUDY_LEVEL,
        CanonicalKey.EDUCATION_START_YEAR,
        CanonicalKey.EDUCATION_END_YEAR,
        CanonicalKey.GRADUATION_YEAR,
        CanonicalKey.GITHUB,
    }
)

_LEGAL_EVIDENCE_ONLY_KEYS = frozenset(
    {
        CanonicalKey.WORK_AUTHORISATION,
        CanonicalKey.SPONSORSHIP,
    }
)

# SAFETY (fail closed): legal/eligibility declarations whose stored answers
# carry no jurisdiction scope must never auto-fill.  The stored profile and
# answer-bank wording is UK-scoped with no country metadata, and the
# opportunity record has no reliable jurisdiction field, so an auto-answer
# could assert a false statement to a foreign employer.  Criminal-record
# declarations are additionally never auto-answered under any circumstance.
_LEGAL_DECLARATION_GATED_KEYS = frozenset(
    {
        CanonicalKey.WORK_AUTHORISATION,
        CanonicalKey.SPONSORSHIP,
        CanonicalKey.CRIMINAL_RECORD,
    }
)


def build_fill_plan(
    fields: list[InspectedField],
    classifier: Classifier,
    profile_values: dict[str, object],
    *,
    answer_lookup: AnswerLookup,
    document_lookup: DocumentLookup,
    adapter_name: str,
    step_evidence: dict[str, object] | None = None,
) -> FillPlan:
    actions: list[FieldAction] = []
    findings: list[RiskFinding] = []

    if adapter_name == "generic":
        findings.append(
            RiskFinding("unknown_ats", 1, "Generic ATS adapter requires review")
        )

    # Form-readiness contract (forensic root cause 1): an empty inspected
    # field set is NOT a zero-risk form.  Without explicit provider evidence
    # that this step legitimately needs no fields, zero controls means the
    # application form itself was never found — fail closed at level 4.
    if not fields:
        from app.domain.risk import (
            FormReadiness,
            application_form_not_found_finding,
        )

        readiness = FormReadiness(
            step_requires_no_fields=bool(
                (step_evidence or {}).get("step_requires_no_fields")
            ),
            step_name=str((step_evidence or {}).get("step_name", "")),
        )
        if not readiness.zero_field_step_is_legitimate():
            findings.append(application_form_not_found_finding(adapter_name, readiness))

    for field in fields:
        mapping = classifier.classify(field.question)
        key = mapping.canonical_key
        resolved: ResolvedFieldValue | None = None

        if (
            profile_values.get(CanonicalKey.PROGRAMME_GRADUATION_CONFLICT.value) is True
            and key.value in _PROGRAMME_GRADUATION_DERIVED_KEYS
        ):
            stored_year = profile_values.get("guard.programme_graduation_stored")
            tier_year = profile_values.get("guard.programme_graduation_tier")
            if stored_year not in (None, "") and tier_year not in (None, ""):
                conflict_reason = (
                    f"Stored graduation year {stored_year} conflicts with programme "
                    f"tier year {tier_year}; human review required"
                )
            else:
                conflict_reason = (
                    "Stored graduation evidence conflicts with the programme tier; "
                    "human review required"
                )
            findings.append(
                RiskFinding(
                    "programme_tier_graduation_conflict",
                    2 if field.question.required else 1,
                    conflict_reason,
                )
            )
            actions.append(
                FieldAction(
                    field,
                    mapping,
                    None,
                    "graduation_tier_guard",
                    "blocked" if field.question.required else "omitted",
                )
            )
            continue

        if key in {CanonicalKey.ASSESSMENT, CanonicalKey.CAPTCHA}:
            code = "assessment_handoff" if key is CanonicalKey.ASSESSMENT else "captcha_handoff"
            findings.append(RiskFinding(code, 3, mapping.reason))
            actions.append(FieldAction(field, mapping, None, "handoff", "blocked"))
            continue

        if key is CanonicalKey.LEGAL_ATTESTATION:
            findings.append(
                RiskFinding("legal_attestation", 3, "Legal attestation requires explicit human action")
            )
            actions.append(FieldAction(field, mapping, None, "human", "blocked"))
            continue

        if key is CanonicalKey.DEMOGRAPHIC:
            findings.append(
                RiskFinding("sensitive_demographic", 3, "Sensitive demographic question requires user policy")
            )
            actions.append(FieldAction(field, mapping, None, "human", "blocked"))
            continue

        if key is CanonicalKey.UNKNOWN:
            if field.question.required:
                findings.append(
                    RiskFinding("unknown_required_field", 2, f"Unmapped required field: {field.question.label}")
                )
                status = "blocked"
            else:
                findings.append(
                    RiskFinding("unknown_optional_field", 1, f"Unmapped optional field: {field.question.label}")
                )
                status = "omitted"
            actions.append(FieldAction(field, mapping, None, "unmapped", status))
            continue

        if key in _LEGAL_DECLARATION_GATED_KEYS:
            # Fail closed: a legal/eligibility declaration is never
            # auto-filled, because the stored answer is not known to apply
            # to this posting's jurisdiction.  Fall into the `missing`
            # branch below so the field is left blank for the human exactly
            # like any other question with no approved answer: a required
            # declaration blocks with `approved_legal_answer_missing`.
            resolved = None
        elif key in {CanonicalKey.CV, CanonicalKey.COVER_LETTER}:
            resolved = _coerce_resolved(document_lookup(key.value))
        elif key is CanonicalKey.MOTIVATION:
            resolved = _coerce_resolved(answer_lookup(key.value, field.question.label))
        elif key.value in profile_values:
            resolved = ResolvedFieldValue(
                _normalise_option_value(profile_values[key.value], field),
                "approved_profile",
                mapping.sensitivity in {Sensitivity.LEGAL, Sensitivity.SENSITIVE},
            )
        elif key in _PROFILE_EVIDENCE_ONLY_KEYS or key in _LEGAL_EVIDENCE_ONLY_KEYS:
            # Missing profile/legal evidence is a deliberate blank.  In
            # particular, legal declarations and eligibility answers must not
            # be populated by an arbitrary answer_lookup implementation.
            resolved = None
        else:
            resolved = _coerce_resolved(answer_lookup(key.value, field.question.label))

        if resolved is None or resolved.value == "":
            if field.question.required:
                level = 3 if mapping.sensitivity is Sensitivity.LEGAL else 2
                code = "approved_legal_answer_missing" if level == 3 else "required_answer_missing"
                findings.append(
                    RiskFinding(code, level, f"No approved value for {field.question.label}")
                )
                status = "blocked"
            else:
                findings.append(
                    RiskFinding("optional_value_missing", 1, f"Optional field omitted: {field.question.label}")
                )
                status = "omitted"
            actions.append(FieldAction(field, mapping, None, "missing", status))
            continue

        value = _normalise_option_value(resolved.value, field)
        if _is_native_month_control(field):
            # SAFETY (fail closed): a native month control accepts ONLY an
            # exact, explicitly approved, calendar-valid YYYY-MM value. A
            # year-only value must NEVER be padded with a guessed month, and
            # malformed/whitespace/junk/bool-derived values cannot resolve.
            # Graduation-derived months additionally require year consistency
            # against the framed profile evidence.
            month_match = _MONTH_VALUE_RE.fullmatch(value)
            month_year: int | None = None
            if month_match is not None and 1900 <= int(month_match.group(1)) <= 2100:
                month_year = int(month_match.group(1))
            else:
                month_match = None
            if month_match is None:
                requirement = "required" if field.question.required else "optional"
                findings.append(
                    RiskFinding(
                        "graduation_month_missing",
                        2 if field.question.required else 1,
                        (
                            "Native month control requires an explicit calendar-valid "
                            f"YYYY-MM value for {requirement} field "
                            f"({field.question.label}); year-only or malformed values "
                            "cannot be completed without fabricating a month — "
                            "human review required"
                        ),
                    )
                )
                actions.append(
                    FieldAction(
                        field,
                        mapping,
                        None,
                        "month_guard",
                        "blocked" if field.question.required else "omitted",
                    )
                )
                continue
            if key.value in _PROGRAMME_GRADUATION_DERIVED_KEYS:
                authoritative = _authoritative_graduation_year(profile_values)
                if authoritative is None or month_year != authoritative:
                    findings.append(
                        RiskFinding(
                            "graduation_month_mismatch",
                            2 if field.question.required else 1,
                            (
                                "Month value year does not match the framed graduation-year "
                                f"evidence for {field.question.label}; human review required"
                            ),
                        )
                    )
                    actions.append(
                        FieldAction(
                            field,
                            mapping,
                            None,
                            "month_guard",
                            "blocked" if field.question.required else "omitted",
                        )
                    )
                    continue
        if (
            field.control_type.casefold() == "checkbox"
            and key is CanonicalKey.EDUCATION_SUBJECT
        ):
            option_label = field.question.option_label.strip()
            if not option_label:
                findings.append(
                    RiskFinding(
                        "unknown_required_field" if field.question.required else "unknown_optional_field",
                        2 if field.question.required else 1,
                        "Subject checkbox option label was not independently inspected",
                    )
                )
                actions.append(
                    FieldAction(
                        field,
                        mapping,
                        None,
                        "unmapped",
                        "blocked" if field.question.required else "omitted",
                    )
                )
                continue
            if " ".join(value.casefold().split()) != " ".join(option_label.casefold().split()):
                actions.append(FieldAction(field, mapping, None, "option_not_selected", "omitted"))
                continue
        plausible, control_kind = _resolved_value_is_plausible(
            value,
            field,
            profile_values,
        )
        if not plausible:
            requirement = "required" if field.question.required else "optional"
            findings.append(
                RiskFinding(
                    "implausible_field_value",
                    2 if field.question.required else 1,
                    (
                        f"Resolved value rejected for {requirement} {control_kind} field; "
                        "human review required"
                    ),
                )
            )
            actions.append(
                FieldAction(
                    field,
                    mapping,
                    None,
                    "plausibility_guard",
                    "blocked" if field.question.required else "omitted",
                )
            )
            continue
        actions.append(FieldAction(field, mapping, value, resolved.source, "resolved"))

    return FillPlan(tuple(actions), calculate_risk(findings))


def redact_answer_preview(action: FieldAction) -> str:
    if action.value is None:
        return ""
    if action.mapping.sensitivity in {Sensitivity.LEGAL, Sensitivity.SENSITIVE}:
        return "[REDACTED]"
    if action.mapping.canonical_key in {CanonicalKey.CV, CanonicalKey.COVER_LETTER}:
        return f"[APPROVED FILE: {Path(action.value).name}]"
    return f"[CONFIGURED: {len(action.value)} chars]"


def _normalise_identity(text: str) -> str:
    tokens = re.sub(r"[^a-z0-9]+", " ", text.casefold()).split()
    canonical: list[str] = []
    initials: list[str] = []
    for token in tokens:
        if len(token) == 1:
            initials.append(token)
            continue
        if initials:
            canonical.append("".join(initials))
            initials = []
        canonical.append(token)
    if initials:
        canonical.append("".join(initials))
    return " ".join(canonical)


def _identity_phrase_present(expected: str, *candidates: str) -> bool:
    """Match a complete canonical phrase on a destination page or URL."""

    expected_tokens = tuple(_normalise_identity(expected).split())
    if not expected_tokens:
        return True
    width = len(expected_tokens)
    for candidate in candidates:
        candidate_tokens = tuple(_normalise_identity(candidate).split())
        if any(
            candidate_tokens[index : index + width] == expected_tokens
            for index in range(len(candidate_tokens) - width + 1)
        ):
            return True
    return False


def _destination_findings(page: Page, opportunity: Opportunity) -> list[RiskFinding]:
    identity = page.evaluate(
        """() => {
          const marked = document.querySelector('[data-employer], [data-role]');
          return {
            employer: marked?.getAttribute('data-employer') || document.body.getAttribute('data-employer') || '',
            role: marked?.getAttribute('data-role') || document.body.getAttribute('data-role') || '',
            visible: (document.title + ' ' + (document.body?.innerText || '')).slice(0, 20000),
          };
        }"""
    )
    expected_employer = _normalise_identity(opportunity.employer)
    expected_role = _normalise_identity(opportunity.role_title)
    marked_employer = _normalise_identity(str(identity.get("employer", "")))
    marked_role = _normalise_identity(str(identity.get("role", "")))
    visible = _normalise_identity(str(identity.get("visible", "")))
    url_hint = _normalise_identity(page.url)
    findings: list[RiskFinding] = []

    if marked_employer and marked_employer != expected_employer:
        findings.append(
            RiskFinding("destination_employer_mismatch", 4, "Page employer marker does not match queued opportunity")
        )
    elif expected_employer and not _identity_phrase_present(
        expected_employer, visible, url_hint
    ):
        findings.append(
            RiskFinding("destination_employer_unverified", 4, "Queued employer is not visible on destination page")
        )

    if marked_role and marked_role != expected_role:
        findings.append(
            RiskFinding("destination_role_mismatch", 4, "Page role marker does not match queued opportunity")
        )
    elif expected_role and not _identity_phrase_present(expected_role, visible, url_hint):
        findings.append(
            RiskFinding("destination_role_unverified", 4, "Queued role is not recognisable on destination page")
        )
    return findings


class _OwnerThreadJourney:
    """Playwright journey executed exclusively by a Navigator owner thread.

    The object is constructed on the request thread with immutable target
    evidence and plain candidate data. Its methods receive a page only from
    ``HeadedSessionWorker``; no page/context/browser escapes this boundary.
    """

    def __init__(
        self,
        runner: "AutomationRunner",
        *,
        application_id: str,
        opportunity: Opportunity,
        resolution: TargetResolution,
        mode: RunMode,
        profile_values: Mapping[str, object],
        answers: Mapping[str, object],
        documents: Mapping[str, object],
        document_manifest: Mapping[str, object] | None = None,
        answer_manifest: list[Mapping[str, object]] | None = None,
        before_click: Callable[[], None] | None = None,
    ) -> None:
        self.runner = runner
        self.application_id = application_id
        self.opportunity = opportunity
        self.resolution = resolution
        self.mode = mode
        self.profile_values = dict(profile_values)
        self.answers = dict(answers)
        self.documents = dict(documents)
        self.document_manifest = dict(document_manifest or {})
        self.answer_manifest = [dict(item) for item in (answer_manifest or [])]
        self.before_click = before_click
        # This binding is populated only after FINAL_REVIEW and durable
        # PREPARED intent creation.  It contains scalar evidence only; no
        # database session or Playwright handle crosses the owner boundary.
        self.submission_binding: dict[str, object] = {}
        self._click_boundary_crossed = False
        # Registry selection is resolution-based, never based on raw URL/DOM.
        self.adapter = runner.registry.detect(resolution)
        self.adapter_name = self.adapter.name
        self.entered = False
        self.step_index = 0
        self.last_scope: Any | None = None
        self.last_fields: list[InspectedField] = []
        self.last_evidence: dict[str, object] = {}
        self.last_plan: FillPlan | None = None
        self.last_receipt_evidence: dict[str, object] = {}
        self.final_handle: Any | None = None
        self.final_target: SubmissionTarget | None = None
        self.final_scope: Any | None = None
        self.target_error = ""
        self._human_stage = ""
        # Read only by the owner-thread route guard while one approved
        # provider-managed CV/cover-letter upload is being initiated.
        self.active_document_upload: dict[str, object] = {}
        # Installed by the Navigator owner immediately before PREFILL. The
        # probe is owner-thread-only and returns scalar refusal evidence; it
        # never exposes a browser object or changes route authorization.
        self._egress_fatal_probe: Callable[[], str] | None = None
        # Parallel egress records list set by the navigator alongside the
        # probe.  Contains the full _egress_records from the navigator's
        # route guard, so _egress_stop_result can include them in the
        # receipt and audit event for observability without changing any
        # allow/deny decision.
        self._egress_fatal_records: list[dict[str, object]] = []

    @staticmethod
    def _visible_identity(page: Any) -> tuple[str, str, str]:
        try:
            raw = page.evaluate(
                """() => ({
                  employer: document.querySelector('[data-employer], [data-company]')?.getAttribute('data-employer')
                    || document.querySelector('[data-employer], [data-company]')?.getAttribute('data-company') || '',
                  role: document.querySelector('[data-role], [data-job-title]')?.getAttribute('data-role')
                    || document.querySelector('[data-role], [data-job-title]')?.getAttribute('data-job-title') || '',
                  visible: ((document.title || '') + ' ' + (document.body?.innerText || '')).slice(0, 20000)
                })"""
            ) or {}
            return str(raw.get("employer", "")), str(raw.get("role", "")), str(raw.get("visible", ""))
        except Exception:  # noqa: BLE001
            return "", "", ""

    @staticmethod
    def _phrase(expected: str, *candidates: str) -> bool:
        tokens = re.sub(r"[^a-z0-9]+", " ", expected.casefold()).split()
        if not tokens:
            return True
        width = len(tokens)
        for candidate in candidates:
            actual = re.sub(r"[^a-z0-9]+", " ", str(candidate).casefold()).split()
            if any(actual[index : index + width] == tokens for index in range(len(actual) - width + 1)):
                return True
        return False

    def _identity_verified(self, page: Any) -> bool:
        employer, role, visible = self._visible_identity(page)
        if hasattr(self.adapter, "verify_destination_identity"):
            try:
                return bool(
                    self.adapter.verify_destination_identity(
                        page,
                        expected_employer=self.opportunity.employer,
                        expected_role=self.opportunity.role_title,
                    )
                )
            except Exception:  # noqa: BLE001
                return False
        return self._phrase(self.opportunity.employer, employer, visible) and self._phrase(
            self.opportunity.role_title, role, visible
        )

    def _boundary(self, page: Any) -> tuple[bool, str]:
        detector = getattr(self.adapter, "detect_human_boundary", None)
        if detector is None:
            return False, ""
        try:
            return detector(page)
        except Exception as exc:  # noqa: BLE001 - fail closed
            return True, f"human-boundary-scan-failed:{type(exc).__name__}"

    def _lookups(self):
        def answer_lookup(key: str, _label: str) -> object | None:
            return self.answers.get(key)

        def document_lookup(key: str) -> object | None:
            return self.documents.get(key)

        return answer_lookup, document_lookup

    def _scope(self, page: Any) -> Any:
        current = getattr(self.adapter, "current_scope", None)
        if current is not None:
            try:
                return current(page)
            except Exception:  # noqa: BLE001
                pass
        return page

    def _result(
        self,
        state: str,
        *,
        reason: str = "",
        risk_level: int = 0,
        blocked_reasons: tuple[str, ...] = (),
        manifest: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "state": state,
            "reason": reason,
            "risk_level": int(risk_level),
            "blocked_reasons": tuple(blocked_reasons),
            "adapter": self.adapter_name,
            "step_index": self.step_index,
            "manifest": dict(manifest or {}),
            "target_status": self.resolution.kind.value,
            "source_url": self.resolution.source_url,
            "application_url": self.resolution.final_url,
            "resolution_evidence": dict(self.resolution.evidence),
        }

    def _human_result(
        self,
        reason: str,
        *,
        stage: str,
        prefilled_fields: list[str] | None = None,
        failed_fields: list[str] | None = None,
        failed_field_reasons: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        self._human_stage = stage
        manifest = self._handoff_field_manifest(
            prefilled_fields=prefilled_fields,
            failed_fields=failed_fields,
            failed_field_reasons=failed_field_reasons,
        )
        manifest.update(
            {
                "application_id": self.application_id,
                "employer": self.opportunity.employer,
                "role": self.opportunity.role_title,
                "step_index": self.step_index,
                "human_boundary": reason,
                "stage": stage,
            }
        )
        return self._result(
            "HUMAN_REQUIRED",
            reason=reason,
            risk_level=3,
            blocked_reasons=("human_boundary",),
            manifest=manifest,
        )

    @staticmethod
    def _safe_handoff_label(value: object) -> str:
        """Keep page-authored field labels bounded and free of candidate values."""

        label = " ".join(str(value or "").split())
        label = re.sub(r"\b[^\s@]+@[^\s@]+\b", "[redacted field label]", label)
        label = re.sub(r"(?<!\w)\+?[0-9][0-9 ().-]{6,}[0-9](?!\w)", "[redacted field label]", label)
        return label[:120].rstrip()

    def _pending_handoff_fields(
        self,
        *,
        failed_field_reasons: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Project only labels and generic reasons for unresolved controls."""

        plan = self.last_plan
        if plan is None:
            return ()
        failure_map = {
            str(key): str(value)
            for key, value in (failed_field_reasons or {}).items()
            if str(key) and str(value)
        }
        details: list[dict[str, object]] = []
        for action in plan.actions:
            field_identity = action.field.question.name or action.field.question.label
            failure_code = failure_map.get(field_identity)
            if action.status == "resolved" and action.value is not None and not failure_code:
                continue
            if action.source == "option_not_selected":
                continue
            label = self._safe_handoff_label(
                action.field.question.label or action.field.question.name
            )
            if not label:
                continue
            if failure_code == "no_matching_option":
                reason_code = failure_code
                reason = "no matching option was offered by the widget; review required"
            elif failure_code == "ambiguous_option":
                reason_code = failure_code
                reason = "multiple offered options matched; review required"
            elif failure_code == "commit_failed":
                reason_code = failure_code
                reason = "the widget did not commit the offered option; review required"
            elif failure_code == "restoration_unverified":
                reason_code = failure_code
                reason = "selection could not be restored or verified; reselect an option"
            elif failure_code:
                reason_code = failure_code
                reason = "approved value could not be committed; review required"
            elif action.source == "plausibility_guard":
                reason_code = "plausibility_rejected"
                reason = "plausibility check rejected the stored value"
            elif action.source == "graduation_tier_guard":
                reason_code = "programme_tier_graduation_conflict"
                reason = "stored graduation evidence conflicts with programme tier"
            elif (
                action.mapping.canonical_key is CanonicalKey.LEGAL_ATTESTATION
            ):
                reason_code = "legal_declaration_human_required"
                reason = "no exact stored-profile match"
            elif action.mapping.sensitivity is Sensitivity.LEGAL:
                reason_code = "legal_answer_missing"
                reason = "no exact stored-profile match"
            elif action.source == "missing":
                reason_code = "approved_value_missing"
                reason = "no approved stored answer"
            elif action.source == "unmapped":
                if action.mapping.canonical_key is CanonicalKey.UNKNOWN and action.mapping.reason != "No deterministic mapping":
                    reason_code = "candidate_answer_required"
                    reason = f"{action.mapping.reason}; ARGUS will not invent or infer it"
                else:
                    reason_code = "no_approved_mapping"
                    reason = "no approved mapping"
            elif action.source == "handoff":
                reason_code = "human_handoff_required"
                reason = "requires human completion"
            else:
                reason_code = "human_review_required"
                reason = "requires human review"
            details.append(
                {
                    "label": label,
                    "reason": reason,
                    "reason_code": reason_code,
                }
            )
        return tuple(details[:32])

    def _handoff_field_manifest(
        self,
        *,
        prefilled_fields: list[str] | None = None,
        failed_fields: list[str] | None = None,
        failed_field_reasons: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        """Attach safe unresolved-field evidence to every human handoff."""

        pending_fields = self._pending_handoff_fields(
            failed_field_reasons=failed_field_reasons,
        )
        failed_reason_codes = {
            str(value)
            for value in (failed_field_reasons or {}).values()
            if str(value)
        }
        return {
            "prefilled_fields": list(prefilled_fields or []),
            "failed_fields": list(failed_fields or []),
            "blank_fields": [item["label"] for item in pending_fields],
            # Keep the original two-column handoff contract stable for
            # existing clients; machine-readable codes live beside it.
            "blank_field_reasons": [
                {"label": item["label"], "reason": item["reason"]}
                for item in pending_fields
            ],
            "blank_field_reason_codes": [
                {"label": item["label"], "reason_code": item["reason_code"]}
                for item in pending_fields
            ],
            "failed_field_reasons": [
                dict(item)
                for item in pending_fields
                if str(item.get("reason_code") or "") in failed_reason_codes
            ],
            "plausibility_rejected_fields": [
                item["label"]
                for item in pending_fields
                if item["reason_code"] == "plausibility_rejected"
            ],
            "submission": "not_clicked",
        }

    def set_egress_fatal_probe(self, probe: Callable[[], str]) -> None:
        """Bind the owner worker's fail-closed PREFILL interruption probe."""

        self._egress_fatal_probe = probe

    def set_egress_fatal_records(self, records: list[dict[str, object]]) -> None:
        """Bind the owner worker's egress records for receipt/audit observability.

        These records are produced by the navigator's route guard and include
        both fatal and tolerable (record_only) blocked requests.  The data is
        included in the receipt_json and audit event without changing any
        allow/deny decision.
        """

        self._egress_fatal_records = list(records)

    def _egress_stop_result(
        self,
        *,
        stage: str,
        prefilled_fields: list[str] | None = None,
        failed_fields: list[str] | None = None,
        failed_field_reasons: Mapping[str, str] | None = None,
    ) -> dict[str, object] | None:
        probe = getattr(self, "_egress_fatal_probe", None)
        if probe is None or self.mode is not RunMode.PREFILL:
            return None
        try:
            fatal_reason = str(probe() or "").strip()
        except Exception:  # noqa: BLE001 - an opaque guard is itself unsafe
            fatal_reason = "egress_probe_failed"
        if not fatal_reason:
            return None
        manifest = self._handoff_field_manifest(
            prefilled_fields=prefilled_fields,
            failed_fields=failed_fields,
            failed_field_reasons=failed_field_reasons,
        )
        manifest["egress_stage"] = stage
        # Include egress records for receipt/audit observability.
        # fatal_requests: requests blocked with FATAL impact.
        # tolerable_requests: requests blocked with TOLERABLE or
        #   FIRST_PARTY_SERVICE impact (record_only).
        records = getattr(self, "_egress_fatal_records", [])
        fatal_requests = [r for r in records if r.get("fatal")]
        tolerable_requests = [r for r in records if not r.get("fatal")]
        if fatal_requests:
            manifest["egress_fatal_requests"] = fatal_requests
        if tolerable_requests:
            manifest["egress_tolerable_requests"] = tolerable_requests
        return self._result(
            "NEEDS_USER",
            reason="Filling stopped after a functional or unclassified request was blocked",
            risk_level=3,
            blocked_reasons=("egress_functional_or_unknown_blocked",),
            manifest=manifest,
        )

    def _build_plan(self, fields: list[InspectedField], evidence: dict[str, object]) -> FillPlan:
        answers, documents = self._lookups()
        return build_fill_plan(
            fields,
            self.runner.classifier,
            dict(self.profile_values),
            answer_lookup=answers,
            document_lookup=documents,
            adapter_name=self.adapter_name,
            # Full current-step evidence is deliberately passed on every
            # Workday iteration, including readiness/root/fingerprint data.
            step_evidence=dict(evidence),
        )

    @staticmethod
    def _action_values(plan: FillPlan) -> dict[str, str]:
        return {
            action.field.question.name: action.value
            for action in plan.actions
            if action.value is not None and action.status == "resolved" and action.field.question.name
        }
    def _blank_required_fields(
        self,
        fields: list[InspectedField],
        scope: Any,
    ) -> list[tuple[str, str]]:
        """Return (label, reason_code) for each REQUIRED field that is blank.

        Comboboxes use a sibling readback because react-select keeps the
        search input empty after a selection.  All other controls read the
        native value attribute.  A combobox whose committed readback is
        empty is treated as blank regardless of what the search input shows.
        """
        blanks: list[tuple[str, str]] = []
        for field in fields:
            q = field.question
            if not q.required:
                continue
            label = q.label or q.name or "unknown"
            ct = str(field.control_type).casefold()
            if ct == "combobox":
                try:
                    locator = scope.locator(field.selector).first
                    committed = locator.evaluate("""element => {
                      let node = element;
                      for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                        const selected = node.querySelector?.('.select__single-value');
                        if (selected && (selected.textContent || '').trim()) {
                          return (selected.textContent || '').trim();
                        }
                      }
                      return element.value || '';
                    }""")
                except Exception:
                    committed = ""
                if not committed or not str(committed).strip():
                    blanks.append((label, "required_field_blank"))
            elif ct == "file":
                try:
                    locator = scope.locator(field.selector).first
                    has_value = bool(locator.evaluate("el => !!(el.value || (el.files && el.files.length))"))
                except Exception:
                    has_value = False
                if not has_value:
                    blanks.append((label, "required_field_blank"))
            elif ct in ("checkbox", "radio"):
                try:
                    locator = scope.locator(field.selector)
                    checked = (
                        any(locator.nth(index).is_checked() for index in range(locator.count()))
                        if ct == "radio" else bool(locator.first.is_checked())
                    )
                except Exception:
                    checked = False
                if not checked:
                    blanks.append((label, "required_field_blank"))
            elif ct == "select":
                try:
                    locator = scope.locator(field.selector).first
                    selected = locator.evaluate("el => el.value || ''")
                except Exception:
                    selected = ""
                if not str(selected).strip():
                    blanks.append((label, "required_field_blank"))
            else:
                if not field.value_attribute or not str(field.value_attribute).strip():
                    blanks.append((label, "required_field_blank"))
        return blanks



    def _fill_resolved_action(self, scope: Any, action: FieldAction) -> None:
        mapping = getattr(action, "mapping", None)
        canonical_key = getattr(mapping, "canonical_key", None)
        key = str(getattr(canonical_key, "value", canonical_key) or "")
        upload_manifest: dict[str, object] = {}
        if key in {CanonicalKey.CV.value, CanonicalKey.COVER_LETTER.value}:
            upload_manifest = dict(self.document_manifest.get(key) or {})
            if upload_manifest:
                upload_manifest["path"] = str(action.value or "")
        self.active_document_upload = upload_manifest
        try:
            self.adapter.fill(scope, action.field, str(action.value or ""))
            if upload_manifest:
                try:
                    scope.wait_for_timeout(1_000)
                except Exception:  # noqa: BLE001 - request dispatch already occurred or failed
                    pass
        finally:
            self.active_document_upload = {}

    @staticmethod
    def _field_inventory_signature(
        fields: list[InspectedField], evidence: Mapping[str, object]
    ) -> tuple[object, ...]:
        """Return only structural evidence for the current step.

        Values, readiness booleans, and validation text change as a field is
        filled.  They must not make every verification re-inspection look like
        a newly-rendered step.  Root/step identity and the ordered control
        inventory, however, are safety boundaries: any change forces a fresh
        readiness, risk, mapping, fill, and verification pass before Next can
        be clicked.
        """

        inventory = tuple(
            (
                str(field.selector),
                str(field.question.name),
                str(field.question.label),
                str(field.control_type),
            )
            for field in fields
        )
        return (
            str(evidence.get("root_selector") or ""),
            str(evidence.get("root_token") or evidence.get("form_identity") or ""),
            str(evidence.get("step_name") or ""),
            str(evidence.get("step_marker") or evidence.get("step_id") or ""),
            inventory,
        )

    def _inspect(self, page: Any) -> tuple[list[InspectedField], dict[str, object], Any]:
        inspect = getattr(self.adapter, "inspect_with_evidence", None)
        if inspect is None:
            raise RuntimeError("Adapter does not expose inspect_with_evidence")
        scope = self._scope(page)
        fields, evidence = inspect(scope)
        evidence = dict(evidence or {})
        evidence["step_index"] = self.step_index
        self.last_scope = scope
        self.last_fields = list(fields)
        self.last_evidence = evidence
        return self.last_fields, evidence, scope

    def _verify_after_fill(
        self,
        scope: Any,
        fields: list[InspectedField],
        values: dict[str, str],
    ) -> bool:
        verify = getattr(self.adapter, "verify_step", None)
        if verify is None:
            return True
        return bool(verify(scope, fields, values))

    def _target_for_final(self, scope: Any) -> SubmissionTarget | None:
        handle = getattr(self.adapter, "_last_form_handle", None)
        if handle is None:
            return None
        target_builder = getattr(self.adapter, "submission_target", None)
        if target_builder is None:
            return None
        try:
            target = target_builder(scope, handle)
        except Exception as exc:  # noqa: BLE001 - ambiguity is a user-visible refusal
            self.target_error = str(exc)
            return None
        if target is not None:
            self.final_handle = handle
            self.final_target = target
            self.final_scope = scope
        return target

    @staticmethod
    def _expected_final_url(target: SubmissionTarget) -> str:
        """Return only an explicit, bound final URL for receipt proof.

        The form action is immutable evidence captured from the verified
        current FormHandle. The observed page URL is never promoted to an
        expected final URL. Providers may supply a stricter explicit value in
        ``SubmissionTarget.evidence`` (for example, a receipt host).
        """

        evidence = target.evidence
        explicit = evidence.get("expected_final_url") if isinstance(evidence, Mapping) else None
        candidate = explicit if explicit is not None else target.form_action
        if not isinstance(candidate, str) or not candidate.strip():
            return ""
        canonical = _canonical_request_url(candidate)
        return candidate if canonical else ""

    def _final_manifest(self, target: SubmissionTarget) -> dict[str, object]:
        target_binding = self._target_binding(target)
        return {
            "application_id": self.application_id,
            "employer": self.opportunity.employer,
            "role": self.opportunity.role_title,
            "requisition": target.requisition or str(self.resolution.evidence.get("requisition") or ""),
            "provider": target.provider,
            "application_url": self.resolution.final_url,
            "form_identity": str(self.resolution.evidence.get("form_identity") or ""),
            "provider_step": target.provider_step,
            "destination": target.destination,
            "form_action": target.form_action,
            "expected_final_url": self._expected_final_url(target),
            "method": target.method,
            "root_selector": target.root_selector,
            "control_selector": target.control_selector,
            "control_fingerprint": target.control_fingerprint,
            "page_id": target.page_id,
            "frame_url": target.frame_url,
            "target_fingerprint": target_binding["target_fingerprint"],
            "documents": [
                self.document_manifest[key] for key in sorted(self.document_manifest)
            ],
            "answers": [dict(item) for item in self.answer_manifest],
            "submission": "awaiting_exact_confirmation",
        }

    @staticmethod
    def _target_binding(target: SubmissionTarget) -> dict[str, object]:
        material = {
            "page_id": target.page_id,
            "frame_url": target.frame_url,
            "root_selector": target.root_selector,
            "control_selector": target.control_selector,
            "control_fingerprint": target.control_fingerprint,
            "form_action": target.form_action,
            "method": target.method.upper(),
            "destination": target.destination,
            "provider": target.provider,
            "provider_step": target.provider_step,
        }
        canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
        material["target_fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return material

    @staticmethod
    def _scope_url(scope: Any, fallback: str = "") -> str:
        raw = getattr(scope, "url", "")
        raw = raw() if callable(raw) else raw
        return str(raw or fallback or "")

    @staticmethod
    def _scope_text(scope: Any) -> str:
        try:
            body = scope.locator("body")
            return str(body.inner_text(timeout=1_000) or "")
        except Exception:  # noqa: BLE001 - receipt evidence fails closed
            return ""

    @staticmethod
    def _receipt_snapshot(scope: Any, *, url: str) -> Receipt | None:
        return parse_receipt_text(_OwnerThreadJourney._scope_text(scope), url)

    @staticmethod
    def _evidence(**values: object) -> ReceiptEvidence:
        # Task6a's ReceiptEvidence is deliberately scalar and may gain
        # additional binding fields.  Filtering by declared fields keeps this
        # owner code compatible during rolling upgrades while still supplying
        # every strict field present in the active contract.
        declared = {item.name for item in fields(ReceiptEvidence)}
        return ReceiptEvidence(**{key: value for key, value in values.items() if key in declared})

    def _submission_evidence(
        self,
        scope: Any,
        target: SubmissionTarget,
        *,
        baseline_url: str,
        before: Receipt | None,
        response: Any | None = None,
        page_crashed: bool = False,
        timed_out: bool = False,
        expected_final_url: str = "",
    ) -> ReceiptEvidence:
        binding = self._target_binding(target)
        intent = dict(self.submission_binding)
        intent_id = str(intent.get("id") or intent.get("intent_id") or "")
        intent_nonce = str(intent.get("nonce") or intent.get("intent_nonce") or "")
        request = getattr(response, "request", None) if response is not None else None
        request = request() if callable(request) else request
        request_url = str(getattr(request, "url", "") or "") if request is not None else ""
        request_method = str(getattr(request, "method", "") or "") if request is not None else ""
        response_url = str(getattr(response, "url", "") or "") if response is not None else ""
        try:
            response_status = int(getattr(response, "status", 0) or 0) if response is not None else None
        except (TypeError, ValueError):
            response_status = None
        request_id = str(id(request)) if request is not None else ""
        scope_url = self._scope_url(scope, baseline_url)
        current_url = scope_url
        # A same-page form POST can expose its receipt in the mutating
        # response while Playwright has not yet committed a navigation URL.
        # The response URL is safe to use only when it is the exact bound
        # submission destination; an unrelated response never becomes final
        # navigation evidence.
        if (
            response_url
            and _canonical_request_url(response_url)
            == _canonical_request_url(expected_final_url)
        ):
            current_url = response_url
        receipt = self._receipt_snapshot(scope, url=current_url)
        reference = receipt.reference if receipt is not None else ""
        confirmation_text = receipt.confirmation_text if receipt is not None else ""
        before_reference = before.reference if before is not None else ""
        before_confirmation = before.confirmation_text if before is not None else ""
        request_matches = (
            _canonical_request_url(request_url) == _canonical_request_url(target.form_action)
            if request_url
            else None
        )
        response_matches = (
            _canonical_request_url(response_url) == _canonical_request_url(request_url)
            if response_url and request_url
            else None
        )
        navigation_matches = (
            _canonical_request_url(current_url) == _canonical_request_url(expected_final_url)
            if current_url
            else None
        )
        status_ok = response_status is not None and 200 <= response_status < 300
        return self._evidence(
            url_before_click=baseline_url,
            baseline_captured=True,
            page_id=target.page_id,
            frame_url=target.frame_url,
            root_selector=target.root_selector,
            control_selector=target.control_selector,
            control_fingerprint=target.control_fingerprint,
            form_action=target.form_action,
            target_action=target.form_action,
            target_method=target.method.upper(),
            method=target.method.upper(),
            bound_target_fingerprint=str(binding["target_fingerprint"]),
            target_fingerprint=str(binding["target_fingerprint"]),
            submission_target_fingerprint=str(binding["target_fingerprint"]),
            bound_target_id=str(binding["target_fingerprint"]),
            target_url=target.destination,
            target_destination=target.destination,
            destination=target.destination,
            provider=target.provider,
            target_provider=target.provider,
            bound_provider=target.provider,
            bound_page_id=target.page_id,
            bound_frame_url=target.frame_url,
            bound_root_selector=target.root_selector,
            submission_control_selector=target.control_selector,
            submission_control_fingerprint=target.control_fingerprint,
            bound_control_fingerprint=target.control_fingerprint,
            bound_target=binding["target_fingerprint"],
            bound_intent_id=intent_id,
            intent_id=intent_id,
            bound_intent_nonce=intent_nonce,
            intent_nonce=intent_nonce,
            request_url=request_url,
            request_method=request_method,
            request_id=request_id,
            response_request_id=request_id,
            response_url=response_url,
            response_status=response_status,
            final_url=current_url,
            navigation_url=current_url,
            navigation_kind="final" if current_url != baseline_url else "same_page",
            dom_had_reference=bool(reference),
            dom_confirmation_text_present=bool(confirmation_text),
            reference_seen_before_click=bool(reference and reference == before_reference),
            reference=reference,
            provider_reference=reference,
            confirmation_text=confirmation_text,
            dom_confirmation_text=confirmation_text,
            request_target_matches=request_matches,
            response_target_matches=response_matches,
            navigation_target_matches=navigation_matches,
            provider_success=bool(receipt is not None and status_ok),
            request_succeeded=bool(request_matches) if request is not None else None,
            response_succeeded=status_ok if response is not None else None,
            page_crashed=page_crashed,
            timed_out=timed_out,
            post_click_uncertain=page_crashed or timed_out,
        )

    def receipt_evidence_payload(self) -> dict[str, object]:
        """Return scalar receipt/binding evidence for owner exception paths."""

        payload = dict(self.last_receipt_evidence)
        if self.final_target is not None:
            payload.setdefault("bound_target", dict(self._target_binding(self.final_target)))
        if self.submission_binding:
            payload.setdefault("bound_intent", dict(self.submission_binding))
        return payload

    def _run_steps(self, page: Any) -> dict[str, object]:
        for _ in range(32):
            egress_stop = self._egress_stop_result(stage="before_inspect")
            if egress_stop is not None:
                return egress_stop
            found, reason = self._boundary(page)
            if found:
                return self._human_result(reason or "human boundary detected", stage="before_inspect")
            fields, evidence, scope = self._inspect(page)
            try:
                network_signal = page.evaluate(
                    r"""() => {
                      const form = document.querySelector('form');
                      const marker = form?.getAttribute('data-script-exfiltration') === 'true';
                      const inline = Array.from(document.scripts || []).some(script => {
                        const text = script.textContent || '';
                        return /(?:fetch\s*\(\s*['"]https?:\/\/|navigator\.sendBeacon\s*\(\s*['"]https?:\/\/|(?:window\.)?location(?:\.href)?\s*=\s*['"]https?:\/\/)/i.test(text);
                      });
                      return {blocked: marker || inline};
                    }"""
                ) or {}
            except Exception:  # noqa: BLE001 - inability to inspect is fail-closed below
                network_signal = {}
            if network_signal.get("blocked"):
                return self._result(
                    "NEEDS_USER",
                    reason="Submission-time scripted network egress requires human review",
                    risk_level=3,
                    blocked_reasons=("submission_network_guard",),
                )
            # A form can be structurally recognisable while its action is off
            # the verified application origin. Surface that as a target guard
            # (human review) rather than allowing root binding failure to blur
            # the exact egress reason.
            if evidence.get("root_found") is False and evidence.get("page_root_found") is True:
                form_action = str(evidence.get("form_action") or "").strip()
                if form_action:
                    action_url = urljoin(str(getattr(page, "url", "") or ""), form_action)
                    action_origin = _origin_key(action_url)
                    page_origin = _origin_key(str(getattr(page, "url", "") or ""))
                    if action_origin is not None and action_origin != page_origin:
                        return self._result(
                            "NEEDS_USER",
                            reason="Submission target is outside the verified application origin",
                            risk_level=3,
                            blocked_reasons=("submission_target_guard",),
                        )
            if not evidence.get("root_found") and not fields:
                return self._result(
                    "BLOCKED",
                    reason="Verified application root was not found",
                    risk_level=4,
                    blocked_reasons=("application_root_unverified",),
                )
            inventory_signature = self._field_inventory_signature(fields, evidence)
            plan = self._build_plan(fields, evidence)
            self.last_plan = plan
            risk = plan.risk.level
            blocked = tuple(plan.risk.blocking_codes)
            if evidence.get("root_found") is False and evidence.get("page_root_found") is True:
                risk = max(risk, 4)
                blocked = tuple(dict.fromkeys((*blocked, "application_root_unverified")))
            if any(code == "assessment_handoff" for code in blocked):
                return self._result(
                    "NEEDS_OA",
                    reason="Online assessment requires human completion",
                    risk_level=max(risk, 3),
                    blocked_reasons=blocked,
                )
            if risk >= 4:
                return self._result(
                    "BLOCKED",
                    reason="Automation evidence is insufficient",
                    risk_level=risk,
                    blocked_reasons=blocked,
                )
            if risk > 0 and self.mode is RunMode.PREFILL:
                # Prefill is an explicit visible-browser action. Populate
                # only values that already resolved from approved profile,
                # answer-bank, or document evidence, then hand the same form
                # to the user for every unresolved/legal/sensitive control.
                # No Next or Submit control is clicked on this path.
                values: dict[str, str] = {}
                prefilled_fields: list[str] = []
                failed_fields: list[str] = []
                failed_field_reasons: dict[str, str] = {}
                for action in plan.actions:
                    if action.value is None or action.status != "resolved":
                        continue
                    field_identity = action.field.question.name or action.field.question.label
                    try:
                        self._fill_resolved_action(scope, action)
                    except Exception as exc:  # noqa: BLE001 - preserve the visible handoff
                        failed_fields.append(field_identity)
                        failed_field_reasons[field_identity] = str(
                            getattr(exc, "reason_code", "prefill_failed") or "prefill_failed"
                        )
                        egress_stop = self._egress_stop_result(
                            stage="partial_prefill",
                            prefilled_fields=prefilled_fields,
                            failed_fields=failed_fields,
                            failed_field_reasons=failed_field_reasons,
                        )
                        if egress_stop is not None:
                            return egress_stop
                        continue
                    prefilled_fields.append(field_identity)
                    egress_stop = self._egress_stop_result(
                        stage="partial_prefill",
                        prefilled_fields=prefilled_fields,
                        failed_fields=failed_fields,
                        failed_field_reasons=failed_field_reasons,
                    )
                    if egress_stop is not None:
                        return egress_stop
                    if action.field.question.name:
                        values[action.field.question.name] = action.value
                found, reason = self._boundary(page)
                if found:
                    return self._human_result(
                        reason or "human boundary detected",
                        stage="after_partial_prefill",
                        prefilled_fields=prefilled_fields,
                        failed_fields=failed_fields,
                        failed_field_reasons=failed_field_reasons,
                    )
                if values and not self._verify_after_fill(scope, fields, values):
                    manifest = self._handoff_field_manifest(
                        prefilled_fields=prefilled_fields,
                        failed_fields=failed_fields,
                        failed_field_reasons=failed_field_reasons,
                    )
                    return self._result(
                        "NEEDS_USER",
                        reason="Prefilled values failed browser verification",
                        risk_level=max(risk, 3),
                        blocked_reasons=tuple(
                            dict.fromkeys((*blocked, "field_verification_failed"))
                        ),
                        manifest=manifest,
                    )
                if failed_fields:
                    manifest = self._handoff_field_manifest(
                        prefilled_fields=prefilled_fields,
                        failed_fields=failed_fields,
                        failed_field_reasons=failed_field_reasons,
                    )
                    return self._result(
                        "NEEDS_USER",
                        reason="Approved fields were prefilled; some dropdown values need human selection",
                        risk_level=max(risk, 3),
                        blocked_reasons=tuple(
                            dict.fromkeys((*blocked, "prefill_field_failed"))
                        ),
                        manifest=manifest,
                    )
                manifest = self._handoff_field_manifest(
                    prefilled_fields=prefilled_fields,
                    failed_fields=[],
                    failed_field_reasons=failed_field_reasons,
                )
                return self._result(
                    "NEEDS_USER",
                    reason="Approved fields were prefilled; human review is required for the remaining questions",
                    risk_level=risk,
                    blocked_reasons=blocked,
                    manifest=manifest,
                )
            if risk > 0 and self.mode is RunMode.SUBMIT:
                return self._result(
                    "NEEDS_USER",
                    reason="Approved mapping or human review is required",
                    risk_level=risk,
                    blocked_reasons=blocked,
                )

            # REVIEW and DRY_RUN intentionally stop after read-only planning;
            # they never fill, validate by mutation, advance, or submit.
            if self.mode in {RunMode.INSPECT, RunMode.REVIEW, RunMode.DRY_RUN}:
                return self._result(
                    "ACTIVE",
                    reason="Read-only inspection complete",
                    risk_level=risk,
                    blocked_reasons=blocked,
                )

            values = self._action_values(plan)
            prefilled_fields: list[str] = []
            failed_fields: list[str] = []
            failed_field_reasons: dict[str, str] = {}
            for action in plan.actions:
                if action.value is not None and action.status == "resolved":
                    field_identity = (
                        action.field.question.name or action.field.question.label
                    )
                    try:
                        self._fill_resolved_action(scope, action)
                    except Exception as exc:  # noqa: BLE001 - one control is not the form
                        egress_stop = self._egress_stop_result(
                            stage="fill",
                            prefilled_fields=prefilled_fields,
                            failed_fields=[*failed_fields, field_identity],
                        )
                        if egress_stop is not None:
                            return egress_stop
                        # One control refusing a value is not grounds to abandon
                        # the whole form.  The bare ``raise`` that used to live
                        # here aborted the run before any later field was
                        # attempted and before the manifest existed, so a CV
                        # whose provider-side readback failed silently cost the
                        # user every field after it and left the receipt with
                        # nothing to confirm against.  Record the failure, keep
                        # filling, and stop at NEEDS_USER below -- this path can
                        # never reach READY_TO_SUBMIT while a failure is open.
                        failed_fields.append(field_identity)
                        failed_field_reasons[field_identity] = str(
                            getattr(exc, "reason_code", "prefill_failed")
                            or "prefill_failed"
                        )
                        continue
                    prefilled_fields.append(field_identity)
                    egress_stop = self._egress_stop_result(
                        stage="fill",
                        prefilled_fields=prefilled_fields,
                    )
                    if egress_stop is not None:
                        return egress_stop
            if failed_fields:
                # Stop before validation/advance: a form with a known-unfilled
                # control is a human handoff, not a completed fill.
                return self._result(
                    "NEEDS_USER",
                    reason=(
                        "Approved fields were filled; some controls could not be "
                        "completed automatically and need human selection"
                    ),
                    risk_level=max(risk, 3),
                    blocked_reasons=tuple(
                        dict.fromkeys((*blocked, "prefill_field_failed"))
                    ),
                    manifest=self._handoff_field_manifest(
                        prefilled_fields=prefilled_fields,
                        failed_fields=failed_fields,
                        failed_field_reasons=failed_field_reasons,
                    ),
                )
            found, reason = self._boundary(page)
            if found:
                return self._human_result(
                    reason or "human boundary detected",
                    stage="after_fill",
                    prefilled_fields=prefilled_fields,
                )
            if not self._verify_after_fill(scope, fields, values):
                manifest = self._handoff_field_manifest(
                    prefilled_fields=prefilled_fields,
                )
                return self._result(
                    "NEEDS_USER",
                    reason="Filled values failed browser validity or visible-error checks",
                    risk_level=3,
                    blocked_reasons=("field_verification_failed",),
                    manifest=manifest,
                )

            # Re-inspection is mandatory after every fill because conditional
            # fields can replace the root or add a human boundary.
            refreshed_fields, refreshed_evidence, refreshed_scope = self._inspect(page)
            found, reason = self._boundary(page)
            if found:
                return self._human_result(
                    reason or "human boundary detected",
                    stage="after_reinspect",
                    prefilled_fields=prefilled_fields,
                )
            refreshed_signature = self._field_inventory_signature(
                refreshed_fields, refreshed_evidence
            )
            if refreshed_signature != inventory_signature:
                # Conditional controls can appear after a value is filled (or
                # a provider can replace the root under the same step ID).
                # Re-enter the complete inspection/readiness/risk/mapping/fill
                # cycle.  In particular, never advance using the old plan.
                fields, evidence, scope = (
                    refreshed_fields,
                    refreshed_evidence,
                    refreshed_scope,
                )
                continue
            scope = refreshed_scope

            final_probe = getattr(self.adapter, "is_final_submit_visible", None)
            is_final = (
                bool(final_probe(scope))
                if final_probe is not None
                else bool(refreshed_evidence.get("submit_present"))
            )
            if is_final:
                found, reason = self._boundary(page)
                if found:
                    return self._human_result(
                        reason or "human boundary detected",
                        stage="before_final_activation",
                        prefilled_fields=prefilled_fields,
                    )
                target = self._target_for_final(scope)
                if target is None:
                    return self._result(
                        "NEEDS_USER",
                        reason=self.target_error or "No visible submission control found",
                        risk_level=4,
                        blocked_reasons=("submission_target_guard",),
                    )
                blank_required = self._blank_required_fields(refreshed_fields, scope)
                if blank_required:
                    blank_labels = [item[0] for item in blank_required]
                    reason_codes = {
                        label: code for label, code in blank_required
                    }
                    manifest = self._handoff_field_manifest(
                        prefilled_fields=prefilled_fields,
                        failed_fields=blank_labels,
                        failed_field_reasons=reason_codes,
                    )
                    return self._result(
                        "NEEDS_USER",
                        reason=(
                            "Required fields are blank and await human completion: "
                            + ", ".join(blank_labels)
                        ),
                        risk_level=3,
                        blocked_reasons=("required_field_blank",),
                        manifest=manifest,
                    )
                return self._result(
                    "FINAL_REVIEW",
                    risk_level=0,
                    manifest=self._final_manifest(target),
                )

            if self.adapter_name not in {"workday", "smartrecruiters", "workable"}:
                # Greenhouse/Lever forms have no implicit Next state. Probe
                # the current verified handle once so absent/ambiguous final
                # controls produce a truthful handoff instead of a generic
                # step-transition failure.
                target = self._target_for_final(scope)
                if target is None:
                    return self._result(
                        "NEEDS_USER",
                        reason=self.target_error or "No visible submission control found",
                        risk_level=4,
                        blocked_reasons=("submission_target_guard",),
                    )
                found, reason = self._boundary(page)
                if found:
                    return self._human_result(
                        reason or "human boundary detected",
                        stage="before_final_activation",
                        prefilled_fields=prefilled_fields,
                    )
                blank_required = self._blank_required_fields(refreshed_fields, scope)
                if blank_required:
                    blank_labels = [item[0] for item in blank_required]
                    reason_codes = {
                        label: code for label, code in blank_required
                    }
                    manifest = self._handoff_field_manifest(
                        prefilled_fields=prefilled_fields,
                        failed_fields=blank_labels,
                        failed_field_reasons=reason_codes,
                    )
                    return self._result(
                        "NEEDS_USER",
                        reason=(
                            "Required fields are blank and await human completion: "
                            + ", ".join(blank_labels)
                        ),
                        risk_level=3,
                        blocked_reasons=("required_field_blank",),
                        manifest=manifest,
                    )
                return self._result(
                    "FINAL_REVIEW",
                    risk_level=0,
                    manifest=self._final_manifest(target),
                )

            # PREFILL must never call advance_one_step regardless of risk.
            if self.mode is RunMode.PREFILL:
                return self._result(
                    "NEEDS_USER",
                    reason="Prefill complete without advancing — human review is required for the remaining steps",
                    risk_level=0,
                    blocked_reasons=(),
                )

            advance = getattr(self.adapter, "advance_one_step", None)
            if advance is None or not advance(scope):
                return self._result(
                    "NEEDS_USER",
                    reason="One-step validation did not prove a changed Workday step",
                    risk_level=3,
                    blocked_reasons=("step_transition_unproven",),
                )
            egress_stop = self._egress_stop_result(
                stage="after_next",
                prefilled_fields=prefilled_fields,
            )
            if egress_stop is not None:
                return egress_stop
            # The adapter has already proved that exactly one new step
            # rendered. Record that transition before the post-Next boundary
            # scan so a CAPTCHA on the new step is attributed correctly.
            self.step_index += 1
            found, reason = self._boundary(page)
            if found:
                return self._human_result(
                    reason or "human boundary detected",
                    stage="after_next",
                    prefilled_fields=prefilled_fields,
                )
        return self._result(
            "NEEDS_USER",
            reason="Application exceeded the bounded step budget",
            risk_level=3,
            blocked_reasons=("step_budget_exceeded",),
        )

    def prepare(self, page: Page) -> Mapping[str, object]:
        # The owner worker navigates only to resolution.final_url. Confirm the
        # actual origin remains the verified application origin before any
        # adapter entry click.
        try:
            identity_findings = _destination_findings(page, self.opportunity)
        except Exception as exc:  # noqa: BLE001 - identity proof is fail-closed
            return self._result(
                "BLOCKED",
                reason=f"Destination identity inspection failed: {type(exc).__name__}",
                risk_level=4,
                blocked_reasons=("destination_identity_unverified",),
            )
        explicit_identity_mismatches = tuple(
            finding.code
            for finding in identity_findings
            if finding.code in {"destination_employer_mismatch", "destination_role_mismatch"}
        )
        if explicit_identity_mismatches:
            return self._result(
                "BLOCKED",
                reason="Destination identity does not match the queued opportunity",
                risk_level=4,
                blocked_reasons=explicit_identity_mismatches,
            )
        if not self._identity_verified(page):
            return self._result(
                "BLOCKED",
                reason="Verified employer/role identity is missing on destination",
                risk_level=4,
                blocked_reasons=("destination_identity_unverified",),
            )
        if self.adapter_name == "workday":
            entered = bool(getattr(self.adapter, "enter_application_flow")(page))
        else:
            entered = bool(getattr(self.adapter, "enter_application_flow", lambda _page: True)(page))
        self.entered = entered
        if not entered:
            return self._result(
                "BLOCKED",
                reason="Verified application entry could not be adopted",
                risk_level=4,
                blocked_reasons=("application_entry_unresolved",),
            )
        return self._run_steps(page)

    def resume(self, page: Page) -> Mapping[str, object]:
        # Continue never repeats Apply/Apply Manually and never submits. It
        # rescans the existing context and resumes exactly at the held step.
        return self._run_steps(page)

    def submit(self, page: Page, confirmation_id: str) -> Mapping[str, object]:
        if confirmation_id != self.application_id:
            return {"state": "FINAL_REVIEW", "reason": "Exact application confirmation mismatch"}
        found, reason = self._boundary(page)
        if found:
            return self._human_result(reason or "human boundary detected", stage="before_final_activation")
        scope = self._scope(page)
        handle = self.final_handle
        target = self.final_target
        if handle is None or target is None or self.final_scope is not scope:
            return {"state": "FAILED", "reason": "Bound final target is stale or from another scope"}
        current = getattr(self.adapter, "submission_target", lambda *_args: None)(scope, handle)
        if current is None or current != target:
            return {"state": "FAILED", "reason": "Bound final target changed before activation"}
        try:
            # The browser's data-bearing request is the exact form action;
            # guard that URL rather than an observed/final navigation URL.
            assert_submission_allowed(self.runner.settings, target.form_action, 0)
        except SubmissionBlocked as exc:
            return {
                "state": "NEEDS_USER",
                "reason": str(exc),
                "risk_level": 3,
                "blocked_reasons": ("submission_target_guard",),
                "click_boundary_crossed": False,
            }
        expected_final_url = self._expected_final_url(target)
        if not expected_final_url:
            return {
                "state": "NEEDS_USER",
                "reason": "Verified expected final URL is missing or malformed",
                "risk_level": 3,
                "blocked_reasons": ("submission_target_guard",),
                "click_boundary_crossed": False,
            }
        # The exact adapter-returned control selector is the only click path.
        # Capture the immutable baseline before the CAS click boundary. A
        # static confirmation DOM (including a same-page forged message) is
        # never a receipt, and a missing response is an unknown outcome.
        response = None
        blocked_requests: list[str] = []
        expect_response = getattr(page, "expect_response", None)
        if expect_response is None:
            return {
                "state": "NEEDS_USER",
                "reason": "Submission response observation is unavailable",
                "risk_level": 3,
                "blocked_reasons": ("submission_target_guard",),
                "click_boundary_crossed": False,
            }
        target_action_url = _canonical_request_url(target.form_action)
        expected_final_key = _canonical_request_url(expected_final_url)
        if not target_action_url or not expected_final_key:
            return {
                "state": "NEEDS_USER",
                "reason": "Verified submission action/final URL is malformed",
                "risk_level": 3,
                "blocked_reasons": ("submission_target_guard",),
                "click_boundary_crossed": False,
            }
        baseline_url = self._scope_url(scope, str(getattr(page, "url", "") or ""))
        baseline_receipt = self._receipt_snapshot(scope, url=baseline_url)
        baseline_evidence = self._submission_evidence(
            scope,
            target,
            baseline_url=baseline_url,
            before=baseline_receipt,
            expected_final_url=expected_final_url,
        )
        bound_target = self._target_binding(target)
        # Keep the verified final expectation alongside the exact target
        # binding.  Receipt parsing ignores unknown advisory keys, while the
        # runner/service pass this value explicitly to strict correlation.
        bound_target["expected_final_url"] = expected_final_url
        bound_intent = dict(self.submission_binding)
        self.last_receipt_evidence = {
            "before": asdict(baseline_evidence),
            "bound_target": dict(bound_target),
            "bound_intent": dict(bound_intent),
            "expected_final_url": expected_final_url,
        }

        def is_bound_mutation(candidate: Any) -> bool:
            try:
                candidate_url = _canonical_request_url(str(getattr(candidate, "url", "") or ""))
                request = getattr(candidate, "request", None)
                request = request() if callable(request) else request
                request_url = _canonical_request_url(str(getattr(request, "url", "") or ""))
                method = str(getattr(request, "method", "") or "").casefold()
                return (
                    method in {"post", "put", "patch", "delete"}
                    and request_url == target_action_url
                    and candidate_url == target_action_url
                )
            except Exception:  # noqa: BLE001 - an uninspectable response is not proof
                return False

        def record_request(request: Any) -> None:
            try:
                request_url = str(getattr(request, "url", "") or "")
                if request_url and _origin_key(request_url) != _origin_key(target_action_url):
                    blocked_requests.append(request_url)
            except Exception:  # noqa: BLE001 - failed-request evidence is advisory
                return

        def record_failed_request(request: Any) -> None:
            record_request(request)

        try:
            on_request_failed = getattr(page, "on", None)
            on_request = getattr(page, "on", None)
            remove_request_failed = getattr(page, "remove_listener", None)
            if on_request_failed is not None:
                on_request_failed("requestfailed", record_failed_request)
            if on_request is not None:
                on_request("request", record_request)
            control = scope.locator(target.control_selector)
            click_kwargs: dict[str, object] = {
                "timeout": 10_000,
                "no_wait_after": True,
            }
            # Resolve a supported call shape before the durable CAS boundary
            # where possible.  If a callable cannot be inspected, keep the
            # strict call and treat any post-CAS exception as UNKNOWN; never
            # retry a click after the external side-effect boundary.
            try:
                parameters = inspect.signature(control.click).parameters
                accepts_kwargs = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                if not accepts_kwargs:
                    for key in tuple(click_kwargs):
                        if key not in parameters:
                            click_kwargs.pop(key)
            except (TypeError, ValueError):
                pass
            with expect_response(
                is_bound_mutation,
                timeout=2_000,
            ) as response_info:
                if self.before_click is not None:
                    # The callback performs a durable CAS PREPARED -> CLICKED
                    # in its own short-lived session. From this point every
                    # outcome is post-click uncertainty unless strict receipt
                    # evidence proves confirmation.
                    try:
                        self.before_click()
                    except Exception as exc:  # noqa: BLE001 - no click crossed the boundary
                        return {
                            "state": "UNKNOWN",
                            "reason": f"Submission intent boundary is uncertain: {type(exc).__name__}",
                            "risk_level": 3,
                            "blocked_reasons": ("submission_unknown",),
                            "submission_unknown": True,
                            "click_boundary_crossed": False,
                        }
                    # The durable CAS completed; any subsequent exception is
                    # post-boundary uncertainty, even if Playwright reports
                    # that it could not dispatch the click.
                    self._click_boundary_crossed = True
                control.click(**click_kwargs)
            response = response_info.value
        except Exception:  # noqa: BLE001 - no correlated response means unknown
            if blocked_requests and not self._click_boundary_crossed:
                return {
                    "state": "NEEDS_USER",
                    "reason": "A submission-time request was blocked by the network egress guard",
                    "risk_level": 3,
                    "blocked_reasons": ("submission_network_guard",),
                    "click_boundary_crossed": False,
                }
            return {
                "state": "UNKNOWN",
                "reason": "Submission click issued without a correlated mutating response",
                "submission_unknown": True,
                "click_boundary_crossed": self._click_boundary_crossed,
                "receipt_evidence": self.receipt_evidence_payload(),
            }
        finally:
            if remove_request_failed is not None:
                try:
                    remove_request_failed("requestfailed", record_failed_request)
                except Exception:  # noqa: BLE001 - listener cleanup is best-effort
                    pass
                try:
                    remove_request_failed("request", record_request)
                except Exception:  # noqa: BLE001 - listener cleanup is best-effort
                    pass
        try:
            status = int(getattr(response, "status", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        # Redirects are not a successful submission receipt.  They can hide a
        # login/interstitial hop or an unobserved second request, so only an
        # exact 2xx response may proceed to strict receipt correlation.
        if status < 200 or status >= 300:
            return {
                "state": "UNKNOWN",
                "reason": "Submission response was not successful",
                "submission_unknown": True,
                "click_boundary_crossed": self._click_boundary_crossed,
            }
        try:
            scope.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:  # noqa: BLE001 - receipt correlation decides outcome
            pass
        after_evidence = self._submission_evidence(
            scope,
            target,
            baseline_url=baseline_url,
            before=baseline_receipt,
            response=response,
            expected_final_url=expected_final_url,
        )
        self.last_receipt_evidence["after"] = asdict(after_evidence)
        if not receipt_is_correlated(
            baseline_evidence,
            after_evidence,
            bound_target=bound_target,
            bound_intent=bound_intent,
            expected_final_url=expected_final_url,
        ):
            return {
                "state": "UNKNOWN",
                "reason": "Submission click issued without a correlated receipt",
                "submission_unknown": True,
                "click_boundary_crossed": self._click_boundary_crossed,
                "receipt_evidence": {
                    "before": asdict(baseline_evidence),
                    "after": asdict(after_evidence),
                    "bound_target": dict(bound_target),
                    "bound_intent": dict(bound_intent),
                    "expected_final_url": expected_final_url,
                },
            }
        receipt = self._receipt_snapshot(scope, url=after_evidence.final_url)
        return {
            "state": "CONFIRMED",
            "risk_level": 0,
            "blocked_reasons": (),
            "adapter": self.adapter_name,
            "click_boundary_crossed": self._click_boundary_crossed,
            "receipt": {
                "reference": receipt.reference if receipt is not None else "",
                "url": receipt.url if receipt is not None else after_evidence.final_url,
                "confirmation_text": receipt.confirmation_text if receipt is not None else "",
            },
            "receipt_evidence": {
                "before": asdict(baseline_evidence),
                "after": asdict(after_evidence),
                "bound_target": dict(bound_target),
                "bound_intent": dict(bound_intent),
                "expected_final_url": expected_final_url,
            },
            "expected_final_url": expected_final_url,
            "manifest": self._final_manifest(target) | {"submission": "clicked"},
        }


def _handoff_next_action(result: Mapping[str, object]) -> str:
    """Build bounded, plain-language CAPTCHA handoff copy from safe evidence."""

    manifest_value = result.get("manifest")
    manifest = manifest_value if isinstance(manifest_value, Mapping) else {}
    signal = " ".join(
        str(result.get(key) or "")
        for key in ("state", "reason", "blocked_reasons")
    )
    signal += " " + str(manifest.get("human_boundary") or "")
    captcha_requests = manifest.get("captcha_requests")
    if isinstance(captcha_requests, (list, tuple)) and captcha_requests:
        signal += " captcha"
    if "captcha" not in signal.casefold():
        return ""

    rendered: list[str] = []
    details = manifest.get("blank_field_reasons")
    if isinstance(details, (list, tuple)):
        for item in details[:32]:
            if not isinstance(item, Mapping):
                continue
            label = " ".join(str(item.get("label") or "").split())[:72]
            reason = " ".join(str(item.get("reason") or "").split())[:88]
            if label and reason:
                rendered.append(f"{label}: {reason}")
    prefix = "CAPTCHA needs solving"
    if rendered:
        prefix += ". Fields requiring review: " + "; ".join(rendered)
    else:
        prefix += ". Review any remaining blank fields"
    suffix = ". Press Submit yourself"
    return (prefix[: max(0, 240 - len(suffix))].rstrip(" ;,.") + suffix)[:240]


class AutomationRunner:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        crypto: CryptoBox,
        *,
        classifier: Classifier | None = None,
        registry: AdapterRegistry | None = None,
        handoff_manager=None,
        notifier: NotificationService | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.crypto = crypto
        self.classifier = classifier or CompositeClassifier(DeterministicClassifier())
        self.registry = registry or AdapterRegistry()
        self._handoff_manager = handoff_manager
        self._notifier = notifier or NotificationService.from_settings(settings)
        if self._notifier.enabled:
            install_application_notification_observer(database, self._notifier)
        # One AutomationRunner instance owns at most one in-flight Navigator
        # journey.  Keep an explicit lease so every exception after browser
        # creation can close the exact newly-created owner before it escapes
        # the request.  Review/human handoffs clear this lease only after the
        # session has intentionally been published to the caller.
        self._active_navigator_lease: tuple[object, str, bool] | None = None

    def _cleanup_active_navigator_lease(self, reason: str) -> None:
        """Close a failed pre-click journey without touching older sessions."""

        lease = self._active_navigator_lease
        self._active_navigator_lease = None
        if lease is None:
            return
        navigator, navigator_session_id, owned_navigator = lease
        try:
            navigator.close(navigator_session_id, reason=reason)
            wait_for_cleanup = getattr(navigator, "wait_for_cleanup", None)
            if callable(wait_for_cleanup):
                wait_for_cleanup(navigator_session_id, timeout=5)
        finally:
            if owned_navigator:
                navigator.shutdown()

    @staticmethod
    def _resolution_from_opportunity(opportunity: Opportunity) -> TargetResolution:
        """Load the same fail-closed persisted target contract as Navigator."""

        from app.services.navigator import (
            PersistedTargetResolutionError,
            _load_verified_target_from_opportunity,
        )

        try:
            return _load_verified_target_from_opportunity(opportunity)
        except PersistedTargetResolutionError as exc:
            raise SubmissionBlocked(str(exc), code=exc.code) from exc

    def _prepare_submission_intent(
        self,
        session: Session,
        application: Application,
        journey: _OwnerThreadJourney,
    ) -> SubmissionIntent:
        """Durably arm one exact final-click attempt before queueing it."""

        from app.services.submission_intents import SubmissionIntentService

        target = journey.final_target
        if target is None:
            raise SubmissionBlocked(
                "No exact final submission target is available",
                code="submission_target_guard",
            )
        manifest = journey._final_manifest(target)
        intent = SubmissionIntentService(session).prepare_intent(
            application_id=application.id,
            attempt_id=str(uuid.uuid4()),
            manifest=manifest,
            destination_url=target.destination,
            submission_control_selector=target.control_selector,
        )
        journey.submission_binding = {
            "id": str(intent.id),
            "nonce": str(intent.nonce),
            "attempt_id": str(intent.attempt_id),
            **journey._target_binding(target),
            "expected_final_url": journey._expected_final_url(target),
        }
        return intent

    def _owner_mark_submission_intent(self, intent_id: str, status: str) -> None:
        """Update intent state from the Navigator owner thread.

        The request thread never lends its SQLAlchemy session to Playwright.
        The owner callback opens a short independent session using only the
        immutable intent id, preserving both thread boundaries.
        """

        from app.services.submission_intents import SubmissionIntentService

        with self.database.SessionLocal() as intent_session:
            intent = intent_session.get(SubmissionIntent, intent_id)
            if intent is None:
                raise RuntimeError("Submission intent no longer exists")
            service = SubmissionIntentService(intent_session)
            if status == SubmissionIntent.CLICKED:
                service.mark_clicked(intent)
            elif status == SubmissionIntent.UNKNOWN:
                service.mark_unknown(intent)
            elif status == SubmissionIntent.FAILED_LOCAL:
                service.mark_failed_local(intent)
            else:
                raise ValueError(f"Unknown submission-intent status: {status}")

    def _mark_submission_intent_local(
        self,
        session: Session,
        intent: SubmissionIntent | None,
        status: str,
    ) -> str:
        if intent is None:
            return ""
        from app.services.submission_intents import SubmissionIntentService

        # Never act on the request thread's stale PREPARED object: the owner
        # callback may have durably committed CLICKED between result creation
        # and this reconciliation. Refresh a separate row and use only the
        # monotonic service transitions.
        with self.database.SessionLocal() as intent_session:
            current = intent_session.get(SubmissionIntent, str(intent.id))
            if current is None:
                raise RuntimeError("Submission intent no longer exists")
            service = SubmissionIntentService(intent_session)
            current_status = str(current.status)
            if status == SubmissionIntent.UNKNOWN:
                if current_status in {"PREPARED", SubmissionIntent.PENDING, SubmissionIntent.CLICKED}:
                    service.mark_unknown(current)
            elif status == SubmissionIntent.FAILED_LOCAL:
                if current_status in {"PREPARED", SubmissionIntent.PENDING}:
                    service.mark_failed_local(current)
                elif current_status == SubmissionIntent.CLICKED:
                    # A stale pre-click view must never downgrade a durable
                    # click into a retryable local failure.
                    service.mark_unknown(current)
            elif status == SubmissionIntent.CLICKED:
                if current_status in {"PREPARED", SubmissionIntent.PENDING}:
                    service.mark_clicked(current)
            elif status == SubmissionIntent.CONFIRMED:
                raise ValueError("CONFIRMED requires strict receipt evidence")
            else:
                raise ValueError(f"Unknown submission-intent status: {status}")
            actual_status = str(current.status)
        try:
            session.refresh(intent)
        except Exception:  # noqa: BLE001 - persistence truth is in the fresh session
            pass
        return actual_status

    def _confirm_submission_intent(
        self,
        intent_id: str,
        result: Mapping[str, object],
    ) -> None:
        """Persist CONFIRMED only through strict exact-receipt correlation."""

        from app.services.submission_intents import SubmissionIntentService

        evidence = result.get("receipt_evidence")
        if not isinstance(evidence, Mapping):
            raise RuntimeError("Strict receipt evidence is missing")
        before_raw = evidence.get("before")
        after_raw = evidence.get("after")
        bound_target = evidence.get("bound_target")
        if not isinstance(before_raw, Mapping) or not isinstance(after_raw, Mapping):
            raise RuntimeError("Strict receipt baseline or post-click evidence is missing")
        declared = {item.name for item in fields(ReceiptEvidence)}
        before = ReceiptEvidence(
            **{key: value for key, value in before_raw.items() if key in declared}
        )
        after = ReceiptEvidence(
            **{key: value for key, value in after_raw.items() if key in declared}
        )
        if not isinstance(bound_target, Mapping):
            raise RuntimeError("Exact submission target binding is missing")
        expected_final_url = result.get("expected_final_url")
        if not isinstance(expected_final_url, str) or not expected_final_url.strip():
            raise RuntimeError("Verified expected final URL evidence is missing")
        with self.database.SessionLocal() as intent_session:
            intent = intent_session.get(SubmissionIntent, intent_id)
            if intent is None:
                raise RuntimeError("Submission intent no longer exists")
            bound_intent = {
                "id": str(intent.id),
                "nonce": str(intent.nonce),
                "attempt_id": str(intent.attempt_id),
            }
            SubmissionIntentService(intent_session).confirm_with_receipt(
                intent,
                before,
                after,
                bound_target=dict(bound_target),
                bound_intent=bound_intent,
                expected_final_url=expected_final_url,
            )

    def _finalize_confirmed_receipt(
        self,
        session: Session,
        application: Application,
        submission_intent: SubmissionIntent,
        result: Mapping[str, object],
        receipt: Receipt | None,
    ) -> None:
        """Atomically persist application confirmation and intent CONFIRMED.

        FINAL RECEIPT PERSISTENCE: the strict correlated receipt, the
        ``CLICKED -> CONFIRMED`` intent compare-and-set, and the application
        terminal confirmation (state, reference, applied date) commit in one
        request-session transaction, or none of them is durable. A crash
        before the commit therefore never leaves ``CONFIRMATION_VERIFIED``
        with intent ``CLICKED``; the durable row stays ``CLICKED`` and
        non-replayable. A crash after the commit finds both ``CONFIRMED``.

        This runs strictly after browser execution has finished (the journey
        result is already in ``result``), so no DB write transaction is held
        across browser execution. No independent session is opened here:
        opening a second SQLite writer while this session holds pending
        writes would risk a lock wait. The legacy independent-session
        ``_confirm_submission_intent`` helper is retained for other seams
        but must not be used on this path.
        """

        from app.services.submission_intents import SubmissionIntentService

        if submission_intent is None:
            raise RuntimeError("Submission intent is missing for confirmation")
        evidence = result.get("receipt_evidence")
        if not isinstance(evidence, Mapping):
            raise RuntimeError("Strict receipt evidence is missing")
        before_raw = evidence.get("before")
        after_raw = evidence.get("after")
        bound_target = evidence.get("bound_target")
        if not isinstance(before_raw, Mapping) or not isinstance(after_raw, Mapping):
            raise RuntimeError("Strict receipt baseline or post-click evidence is missing")
        declared = {item.name for item in fields(ReceiptEvidence)}
        before = ReceiptEvidence(
            **{key: value for key, value in before_raw.items() if key in declared}
        )
        after = ReceiptEvidence(
            **{key: value for key, value in after_raw.items() if key in declared}
        )
        if not isinstance(bound_target, Mapping):
            raise RuntimeError("Exact submission target binding is missing")
        expected_final_url = result.get("expected_final_url")
        if not isinstance(expected_final_url, str) or not expected_final_url.strip():
            raise RuntimeError("Verified expected final URL evidence is missing")
        # Re-read the owner-committed CLICKED status in this session. The
        # request-session identity map still holds the stale PREPARED object
        # from before the owner callback; without this refresh the CAS would
        # expect the wrong status.
        try:
            session.refresh(submission_intent, attribute_names=["status"])
        except Exception as exc:
            raise RuntimeError("Submission intent no longer exists") from exc
        if submission_intent is None or str(submission_intent.application_id) != str(
            application.id
        ):
            raise RuntimeError("Submission intent is missing or bound to another application")
        bound_intent = {
            "id": str(submission_intent.id),
            "nonce": str(submission_intent.nonce),
            "attempt_id": str(submission_intent.attempt_id),
        }
        # Strict receipt validation + CLICKED -> CONFIRMED CAS + application
        # terminal confirmation share one SAVEPOINT inside the request
        # transaction. A validation/CAS/transition failure rolls back only
        # this savepoint, preserving the outer AutomationRun writes for the
        # truthful UNKNOWN reconciliation below.
        with session.begin_nested():
            SubmissionIntentService(session).confirm_with_receipt_pending(
                submission_intent,
                before,
                after,
                bound_target=dict(bound_target),
                bound_intent=bound_intent,
                expected_final_url=expected_final_url,
            )
            # Application terminal confirmation joins the same transaction.
            if ApplicationState(application.state) == ApplicationState.FILLING:
                self._transition(session, application, ApplicationState.READY_TO_SUBMIT)
            if ApplicationState(application.state) == ApplicationState.READY_TO_SUBMIT:
                self._transition(session, application, ApplicationState.SUBMITTED)
            if ApplicationState(application.state) == ApplicationState.SUBMITTED:
                self._transition(session, application, ApplicationState.CONFIRMATION_VERIFIED)
            application.submission_reference = receipt.reference if receipt else ""
            application.applied_at = datetime.now(timezone.utc)
            session.flush()
        # One durable commit for receipt + intent + application together
        # (alongside the already-flushed AutomationRun row).
        session.commit()

    def _approved_answers(
        self,
        session: Session,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Return usable answers and a plaintext-free durable identity list."""

        answers: dict[str, object] = {}
        manifest: list[dict[str, object]] = []
        for entry in session.scalars(
            select(AnswerEntry).where(AnswerEntry.approved.is_(True))
        ).all():
            if entry.answer_ciphertext:
                try:
                    plaintext = self.crypto.decrypt(entry.answer_ciphertext)
                    answers[entry.canonical_key] = ResolvedFieldValue(
                        plaintext,
                        "answer_bank",
                        bool(entry.sensitive),
                    )
                except Exception:  # noqa: BLE001 - unavailable answers remain unmapped
                    continue
                manifest.append(
                    {
                        "id": str(entry.id),
                        "canonical_key": str(entry.canonical_key),
                        # Bind the encrypted stored record, not the plaintext.
                        # Fernet tokens change whenever an answer is replaced,
                        # while this digest does not enable plaintext guessing.
                        "sha256": hashlib.sha256(
                            entry.answer_ciphertext.encode("utf-8")
                        ).hexdigest(),
                        "approved": True,
                        "sensitive": bool(entry.sensitive),
                    }
                )
        manifest.sort(
            key=lambda item: (str(item["canonical_key"]), str(item["id"]))
        )
        return answers, manifest

    def _answer_manifest(self, session: Session) -> list[dict[str, object]]:
        return self._approved_answers(session)[1]

    def _approved_inputs(
        self,
        session: Session,
        application: Application,
        framing: ProgrammeFraming,
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        list[dict[str, object]],
    ]:
        profile = ProfileService(session, self.crypto).get_automation_data(framing)
        answers, answer_manifest = self._approved_answers(session)
        for key in (
            CanonicalKey.GRADUATION_YEAR.value,
            CanonicalKey.EDUCATION_END_YEAR.value,
        ):
            candidate = profile.get(key)
            if candidate is None and key in answers:
                answer = answers[key]
                candidate = answer.value if isinstance(answer, ResolvedFieldValue) else answer
            if re.fullmatch(r"\d{4}", str(candidate or "").strip()) and int(str(candidate).strip()) != framing.graduation_year:
                profile[CanonicalKey.PROGRAMME_GRADUATION_CONFLICT.value] = True
                profile.setdefault("guard.programme_graduation_stored", int(str(candidate).strip()))
                profile.setdefault("guard.programme_graduation_tier", framing.graduation_year)
                break
        documents: dict[str, object] = {}
        document_manifest: dict[str, object] = {}
        document_service = DocumentService(session, self.settings.documents_dir)
        for key, document_id in (
            (CanonicalKey.CV.value, application.selected_cv_id),
            (CanonicalKey.COVER_LETTER.value, application.selected_cover_letter_id),
        ):
            if not document_id:
                continue
            document = session.get(Document, document_id)
            if document is None:
                raise SubmissionBlocked(
                    f"Selected {key} document no longer exists",
                    code="submission_document_changed",
                )
            if not document.approved:
                raise SubmissionBlocked(
                    f"Selected {key} document is no longer approved",
                    code="submission_document_changed",
                )
            if not document_service.verify(document):
                raise SubmissionBlocked(
                    f"Selected {key} document bytes or stored hash changed",
                    code="submission_document_changed",
                )
            if key == CanonicalKey.CV.value:
                if not document_service.cv_matches_variant(
                    document, framing.cv_variant_tag
                ):
                    raise SubmissionBlocked(
                        "Selected CV does not match the opportunity programme framing",
                        code="programme_framing_mismatch",
                    )
            documents[key] = ResolvedFieldValue(document.path, "approved_document", False)
            document_manifest[key] = {
                "id": str(document.id),
                "kind": key,
                "sha256": str(document.sha256),
                "approved": True,
            }
        return dict(profile), answers, documents, document_manifest, answer_manifest

    def _document_manifest(self, session: Session, application: Application) -> dict[str, object]:
        """Re-derive every selected document or refuse before the click boundary.

        A selected document is load-bearing submission input.  Silently
        omitting a missing, unapproved, or byte-mismatched selection would
        turn the user's reviewed manifest into a different application.
        """
        manifest: dict[str, object] = {}
        verifier = DocumentService(session, self.settings.documents_dir)
        for key, document_id in (
            (CanonicalKey.CV.value, application.selected_cv_id),
            (CanonicalKey.COVER_LETTER.value, application.selected_cover_letter_id),
        ):
            if not document_id:
                continue
            document = session.get(Document, document_id)
            if document is None:
                raise SubmissionBlocked(
                    f"Selected {key} document no longer exists",
                    code="submission_document_changed",
                )
            if not document.approved:
                raise SubmissionBlocked(
                    f"Selected {key} document is no longer approved",
                    code="submission_document_changed",
                )
            if not verifier.verify(document):
                raise SubmissionBlocked(
                    f"Selected {key} document bytes or stored hash changed after review",
                    code="submission_document_changed",
                )
            manifest[key] = {"id": str(document.id), "kind": key, "sha256": str(document.sha256), "approved": True}
        return manifest

    def _revalidate_submission_state(
        self,
        session: Session,
        *,
        application_id: str,
        resolution: TargetResolution,
        journey: _OwnerThreadJourney,
    ) -> Application:
        """Re-read every mutable input that can affect the final click."""

        session.expire_all()
        current_application = session.get(Application, application_id)
        if current_application is None:
            raise SubmissionBlocked(
                "Application disappeared before submission",
                code="submission_state_changed",
            )
        current_state = ApplicationState(current_application.state)
        if current_state not in {
            ApplicationState.FILLING,
            ApplicationState.READY_TO_SUBMIT,
        }:
            raise SubmissionBlocked(
                f"Application state changed before submission: {current_state.value}",
                code="submission_state_changed",
            )
        try:
            current_eligibility = json.loads(
                current_application.eligibility_json or "{}"
            )
            current_conflict = json.loads(current_application.conflict_json or "{}")
        except (TypeError, ValueError) as exc:
            raise SubmissionBlocked(
                "Eligibility or conflict evidence changed before submission",
                code="submission_state_changed",
            ) from exc
        if (
            isinstance(current_eligibility, Mapping)
            and current_eligibility.get("eligible") is False
        ) or (
            isinstance(current_conflict, Mapping)
            and current_conflict.get("blocked") is True
        ):
            raise SubmissionBlocked(
                "Eligibility or conflict now blocks submission",
                code="submission_state_changed",
            )
        if (
            current_application.risk_is_assessed
            and current_application.effective_risk_level != 0
        ):
            raise SubmissionBlocked(
                "Persisted risk is no longer zero",
                code="submission_state_changed",
            )
        current_opportunity = session.get(
            Opportunity,
            current_application.opportunity_id,
        )
        if current_opportunity is None:
            raise SubmissionBlocked(
                "Verified application target disappeared",
                code="submission_target_changed",
            )
        current_resolution = self._resolution_from_opportunity(current_opportunity)
        if (
            current_resolution.final_url != resolution.final_url
            or current_resolution.provider != resolution.provider
            or dict(current_resolution.evidence) != dict(resolution.evidence)
        ):
            raise SubmissionBlocked(
                "Persisted application target changed after review",
                code="submission_target_changed",
            )
        current_documents = self._document_manifest(session, current_application)
        if current_documents != journey.document_manifest:
            raise SubmissionBlocked(
                "Selected documents changed after review",
                code="submission_document_changed",
            )
        current_answers = self._answer_manifest(session)
        if current_answers != journey.answer_manifest:
            raise SubmissionBlocked(
                "Approved answers changed after review",
                code="submission_answer_changed",
            )
        return current_application

    def _revalidate_review_target(
        self,
        session: Session,
        *,
        application_id: str,
        resolution: TargetResolution,
    ) -> tuple[Application, TargetResolution]:
        """Re-read the persisted target before REVIEW/PREFILL promotion.

        The browser journey is intentionally long-lived.  A target can be
        invalidated by a concurrent source-resolution attempt while that
        journey is reaching ``FINAL_REVIEW``.  Never convert that stale
        browser result into ``READY_TO_SUBMIT``: expire the request-session
        view, load the current opportunity, and require the full persisted
        target contract to remain byte-for-byte equivalent to the one used
        to open the browser.
        """

        session.expire_all()
        current_application = session.get(Application, application_id)
        if current_application is None:
            raise SubmissionBlocked(
                "Application disappeared before review promotion",
                code="review_target_changed",
            )
        current_state = ApplicationState(current_application.state)
        if current_state not in {
            ApplicationState.FILLING,
            ApplicationState.READY_TO_SUBMIT,
        }:
            raise SubmissionBlocked(
                f"Application state changed before review promotion: {current_state.value}",
                code="review_target_changed",
            )
        current_opportunity = session.get(Opportunity, current_application.opportunity_id)
        if current_opportunity is None:
            raise SubmissionBlocked(
                "Application target disappeared before review promotion",
                code="review_target_changed",
            )
        try:
            current_resolution = self._resolution_from_opportunity(current_opportunity)
        except SubmissionBlocked as exc:
            # Keep the resolver's code for the UI/audit path while exposing a
            # runner-specific boundary to callers.  A persisted BLOCKED
            # target is a hard stop; an unresolved target needs a fresh human
            # resolution attempt.
            raise SubmissionBlocked(
                str(exc),
                code=(
                    "review_target_blocked"
                    if str(getattr(current_opportunity, "target_status", ""))
                    == TargetKind.BLOCKED.value
                    else "review_target_unresolved"
                ),
            ) from exc
        if current_resolution != resolution:
            raise SubmissionBlocked(
                "Persisted application target changed before review promotion",
                code="review_target_changed",
            )
        return current_application, current_resolution

    def _owner_authorize_and_mark_click(
        self,
        *,
        application_id: str,
        review_session_id: str,
        authority_id: str,
        intent_id: str,
        resolution: TargetResolution,
        journey: _OwnerThreadJourney,
    ) -> None:
        """Atomically consume authority and mark CLICKED on the owner thread.

        ``_OwnerThreadJourney.submit`` calls this only after its last human-
        boundary and exact-target scan and immediately before the one bound
        control click.  Both durable row transitions share one transaction;
        any pre-click refusal leaves the authority unconsumed.
        """

        from app.services.submission_intents import SubmissionIntentService

        target = journey.final_target
        if target is None:
            raise SubmissionBlocked(
                "No exact final submission target is available",
                code="submission_target_guard",
            )
        destination_origin = origin_for_url(target.destination)
        if not destination_origin:
            raise SubmissionBlocked(
                "Submission destination origin is invalid",
                code="submission_origin_guard",
            )
        with self.database.SessionLocal() as owner_session:
            self._revalidate_submission_state(
                owner_session,
                application_id=application_id,
                resolution=resolution,
                journey=journey,
            )
            intent = owner_session.get(SubmissionIntent, intent_id)
            if intent is None or str(intent.application_id) != str(application_id):
                raise SubmissionBlocked(
                    "Submission intent is missing or bound to another application",
                    code="submission_intent_invalid",
                )
            try:
                SubmissionAuthorityService(owner_session).consume(
                    authority_id,
                    application_id=application_id,
                    session_id=review_session_id,
                    manifest=journey._final_manifest(target),
                    destination_origin=destination_origin,
                )
                # This CAS commits both the authority consumption and the
                # PREPARED -> CLICKED transition as one database transaction.
                SubmissionIntentService(owner_session).mark_clicked(intent)
            except SubmissionAuthorityError as exc:
                raise SubmissionBlocked(
                    str(exc), code="submission_authority_invalid"
                ) from exc

    def _navigator_for_run(self, headed: bool):
        from app.services.navigator import ApplicationNavigator, service_navigator_for

        navigator = self._handoff_manager
        if navigator is None:
            # API/scheduler callers normally pass the manager explicitly. The
            # database binding closes the remaining construction seam for CLI
            # and adapter callers so a human handoff is still owned by the
            # application service rather than a short-lived runner object.
            navigator = service_navigator_for(self.database)
        if navigator is not None:
            return navigator, False
        # ARGUS_FORCE_HEADLESS: when truthy, the browser launches headless even
        # if headed was requested. This keeps the e2e synthetic suite from
        # opening visible windows while preserving the real --headed behaviour.
        force_headless = os.environ.get("ARGUS_FORCE_HEADLESS", "")
        headless = not headed if not force_headless else True
        return (
            ApplicationNavigator(
                self.database,
                self.settings,
                headless=headless,
            ),
            True,
        )

    def _run_claimed_owner(
        self,
        session: Session,
        application: Application,
        application_id: str,
        mode: RunMode,
        headed: bool,
        session_id: str = "",
        authority_id: str = "",
    ) -> AutomationOutcome:
        # This check precedes AutomationRun creation and, critically, browser
        # creation. ``opportunity.url`` is never a fallback navigation target.
        opportunity = session.get(Opportunity, application.opportunity_id)
        if opportunity is None:
            raise KeyError(f"Opportunity not found: {application.opportunity_id}")
        framing = resolve_programme_framing(opportunity.programme_group)
        if framing is None:
            current_state = ApplicationState(application.state)
            if current_state in self._resume_states():
                if current_state in {
                    ApplicationState.BLOCKED,
                    ApplicationState.NEEDS_OA,
                }:
                    raise SubmissionBlocked(
                        "Blocked or assessment-stage applications require human re-evaluation",
                        code="resume_not_allowed",
                    )
                if not (mode in {RunMode.REVIEW, RunMode.PREFILL} and headed):
                    raise SubmissionBlocked(
                        "Application requires an explicit visible review or prefill before resume",
                        code="resume_not_allowed",
                    )
            elif current_state not in {
                ApplicationState.PACKAGE_PREPARED,
                ApplicationState.FILLING,
                ApplicationState.READY_TO_SUBMIT,
            }:
                raise SubmissionBlocked(
                    f"Application is not prepared for automation: {application.state}",
                    code="resume_not_allowed",
                )
            if current_state is ApplicationState.FAILED_RETRYABLE:
                self._resume_for_headed_review(session, application)
            handoff_state = ApplicationState(application.state)
            if handoff_state in {
                ApplicationState.PACKAGE_PREPARED,
                ApplicationState.FILLING,
                ApplicationState.READY_TO_SUBMIT,
            }:
                self._transition(session, application, ApplicationState.NEEDS_USER)
            application.next_action = "Resolve programme framing"
            session.flush()
            return AutomationOutcome(
                state=ApplicationState.NEEDS_USER.value,
                risk_level=3,
                adapter="none",
                blocked_reasons=("programme_framing_required",),
                source_url=opportunity.url,
            )
        if mode is RunMode.SUBMIT and (not session_id or not authority_id):
            # A direct/CLI/scheduler submit must fail before AutomationRun
            # creation, input filling, or browser ownership.  Review/PREFILL
            # is the sole path that can produce these exact durable bindings.
            raise SubmissionBlocked(
                "Submit requires an exact Navigator session and authority",
                code="submission_authority_required",
            )
        resolution = self._resolution_from_opportunity(opportunity)
        prefill_allowlist: frozenset[str] | None = None
        if mode is RunMode.PREFILL:
            try:
                # The candidate-started PREFILL path is scoped to the exact
                # application URL stored on this row.  The Navigator receives
                # this transient set; it is never written to Settings or the
                # database.
                prefill_allowlist = row_application_url_allowlist(
                    opportunity.application_url
                )
            except PrefillBlocked as exc:
                raise SubmissionBlocked(str(exc), code=exc.code) from exc
        if ApplicationState(application.state) == ApplicationState.PACKAGE_PREPARED:
            self._transition(session, application, ApplicationState.FILLING)
        elif ApplicationState(application.state) in self._resume_states():
            # BLOCKED and NEEDS_OA are human-only. They may not jump straight
            # back into an owner-thread application journey.
            if ApplicationState(application.state) in {
                ApplicationState.BLOCKED,
                ApplicationState.NEEDS_OA,
            }:
                raise SubmissionBlocked(
                    "Blocked or assessment-stage applications require human re-evaluation",
                    code="resume_not_allowed",
                )
            if not (mode in {RunMode.REVIEW, RunMode.PREFILL} and headed):
                raise SubmissionBlocked(
                    "Application requires an explicit visible review or prefill before resume",
                    code="resume_not_allowed",
                )
            self._resume_for_headed_review(session, application)
        elif ApplicationState(application.state) not in {
            ApplicationState.FILLING,
            ApplicationState.READY_TO_SUBMIT,
        }:
            raise SubmissionBlocked(
                f"Application is not prepared for automation: {application.state}",
                code="resume_not_allowed",
            )

        run = AutomationRun(
            application_id=application.id,
            mode=mode.value,
            state=ApplicationState.FILLING.value,
            started_at=datetime.now(timezone.utc),
        )
        session.add(run)
        session.flush()
        trace_path = self.settings.traces_dir / f"{run.id}.zip"
        screenshot_path = self.settings.screenshots_dir / f"{run.id}.png"
        (
            profile_values,
            answers,
            documents,
            document_manifest,
            answer_manifest,
        ) = self._approved_inputs(session, application, framing)
        journey = _OwnerThreadJourney(
            self,
            application_id=application.id,
            opportunity=opportunity,
            resolution=resolution,
            mode=mode,
            profile_values=profile_values,
            answers=answers,
            documents=documents,
            document_manifest=document_manifest,
            answer_manifest=answer_manifest,
        )
        navigator, owned_navigator = self._navigator_for_run(headed)
        submission_intent: SubmissionIntent | None = None
        summary = runner_owned_navigator_summary(
            {
                "employer": opportunity.employer,
                "role": opportunity.role_title,
                "source_url": resolution.source_url,
                "application_url": resolution.final_url,
                "target_status": resolution.kind.value,
                "provider": resolution.provider,
                "trace_path": str(trace_path),
                "screenshot_path": str(screenshot_path),
            }
        )
        session_snapshot = navigator.start(
            application.id,
            mode,
            headed=headed,
            resolution=resolution,
            summary=summary,
            journey_executor=journey,
            run_allowlist=prefill_allowlist,
        )
        self._active_navigator_lease = (
            navigator,
            session_snapshot.session_id,
            owned_navigator,
        )
        navigator.run_journey(session_snapshot.session_id)
        result = dict(
            navigator.wait_for_journey_result(
                session_snapshot.session_id, timeout=_journey_wait_seconds()
            )
        )
        if not result and mode in {RunMode.REVIEW, RunMode.PREFILL}:
            # The headed owner may still be filling or waiting at a provider
            # boundary after the synchronous request budget expires.  Preserve
            # that exact browser for the human rather than closing it as a
            # synthetic FAILED result.
            pending_handoff = _active_handoff_result(
                navigator.get(session_snapshot.session_id),
                headed=headed,
            )
            if pending_handoff is not None:
                result = pending_handoff
        if mode is RunMode.SUBMIT and str(result.get("state") or "").upper() in {
            "CONFIRMED",
            "SUBMITTED",
            "UNKNOWN",
        }:
            raise SubmissionBlocked(
                "Navigator returned a post-click state before authority execution",
                code="submission_authority_bypass",
            )
        if result.get("state") == "FINAL_REVIEW" and mode is RunMode.SUBMIT:
            target = journey.final_target
            if target is None:
                raise SubmissionBlocked("No exact final target is available", code="submission_target_guard")
            # The owner thread must rescan CAPTCHA/human boundaries and the
            # exact final target/control before the durable token is touched.
            # An empty/late acknowledgement is a refusal, never permission.
            preflight_snapshot = navigator.preflight_submission(
                session_snapshot.session_id,
                timeout=5,
            )
            preflight = dict(
                navigator.wait_for_preflight(
                    session_snapshot.session_id,
                    timeout=0,
                )
            )
            if preflight.get("preflight_passed") is not True:
                human = (
                    preflight_snapshot.state is SessionState.HUMAN_REQUIRED
                    or bool(preflight_snapshot.human_boundary)
                )
                result = {
                    "state": "NEEDS_USER" if human else "BLOCKED",
                    "reason": str(
                        preflight.get("reason")
                        or preflight_snapshot.reason
                        or "Final submission preflight was not acknowledged"
                    ),
                    "risk_level": 3 if human else 4,
                    "blocked_reasons": (
                        "human_boundary" if human else "submission_target_changed",
                    ),
                    "click_boundary_crossed": False,
                    "human_boundary": dict(preflight_snapshot.human_boundary),
                }
            else:
                # Re-read all mutable evidence before creating a PREPARED
                # intent. The owner callback repeats this check after the
                # final CAPTCHA/target scan and immediately before the click.
                current_application = self._revalidate_submission_state(
                    session,
                    application_id=application.id,
                    resolution=resolution,
                    journey=journey,
                )
                application = current_application
                destination_origin = origin_for_url(target.destination)
                if not destination_origin:
                    raise SubmissionBlocked(
                        "Submission destination origin is invalid",
                        code="submission_origin_guard",
                    )
                try:
                    SubmissionAuthorityService(session).validate(
                        authority_id,
                        application_id=current_application.id,
                        session_id=session_id,
                        manifest=journey._final_manifest(target),
                        destination_origin=destination_origin,
                    )
                except SubmissionAuthorityError as exc:
                    raise SubmissionBlocked(
                        str(exc), code="submission_authority_invalid"
                    ) from exc
                submission_intent = self._prepare_submission_intent(
                    session,
                    current_application,
                    journey,
                )
                intent_id = str(submission_intent.id)
                journey.before_click = lambda intent_id=intent_id: self._owner_authorize_and_mark_click(
                    application_id=current_application.id,
                    review_session_id=session_id,
                    authority_id=authority_id,
                    intent_id=intent_id,
                    resolution=resolution,
                    journey=journey,
                )
                navigator.confirm_submission(
                    session_snapshot.session_id,
                    confirmation_id=current_application.id,
                )
                navigator.wait_for_terminal(session_snapshot.session_id, timeout=30)
                result = dict(navigator.journey_result(session_snapshot.session_id))
        elif result.get("state") == "FINAL_REVIEW":
            # REVIEW/PREFILL has reached a bound final target but must not
            # click.  Re-read the persisted target before allowing the
            # browser result to promote the application to READY_TO_SUBMIT;
            # source resolution can invalidate the target while this journey
            # is still running.
            if mode in {RunMode.REVIEW, RunMode.PREFILL}:
                try:
                    application, resolution = self._revalidate_review_target(
                        session,
                        application_id=application.id,
                        resolution=resolution,
                    )
                except SubmissionBlocked as exc:
                    blocked = exc.code == "review_target_blocked"
                    result = {
                        "state": "BLOCKED" if blocked else "NEEDS_USER",
                        "reason": str(exc),
                        "risk_level": 4 if blocked else 3,
                        "blocked_reasons": (
                            "review_target_blocked"
                            if blocked
                            else "review_target_unresolved",
                        ),
                        "click_boundary_crossed": False,
                    }

        state_text = str(result.get("state") or "FAILED").upper()
        adapter_name = str(result.get("adapter") or journey.adapter_name)
        try:
            risk_level = int(result.get("risk_level", 4))
        except (TypeError, ValueError):
            risk_level = 4
        blocked_reasons = tuple(str(item) for item in result.get("blocked_reasons", ()))
        error = str(result.get("reason") or "")
        manifest_value = result.get("manifest")
        question_failure_codes: dict[str, str] = {}
        if isinstance(manifest_value, Mapping):
            raw_failure_details = manifest_value.get("failed_field_reasons")
            if isinstance(raw_failure_details, (list, tuple)):
                for item in raw_failure_details:
                    if not isinstance(item, Mapping):
                        continue
                    label = str(item.get("label") or "").strip()
                    code = str(item.get("reason_code") or "").strip()
                    if label and code:
                        question_failure_codes[label] = code
        first_party_value = result.get("first_party_requests")
        captcha_value = result.get("captcha_requests")
        if not isinstance(first_party_value, (list, tuple)) and isinstance(
            manifest_value, Mapping
        ):
            first_party_value = manifest_value.get("first_party_requests")
        if not isinstance(captcha_value, (list, tuple)) and isinstance(
            manifest_value, Mapping
        ):
            captcha_value = manifest_value.get("captcha_requests")
        phase21_prefill_enabled = (
            mode is RunMode.PREFILL
            and bool(
                getattr(
                    self.settings,
                    "egress_impact_classification_enabled",
                    True,
                )
            )
        )
        first_party_requests = (
            _query_free_first_party_requests(
                first_party_value,
                mode=mode,
                current_page_url=resolution.final_url,
                resolved_vendor=resolution.provider,
            )
            if phase21_prefill_enabled
            else None
        )
        captcha_requests = (
            _query_free_captcha_requests(
                captcha_value,
                mode=mode,
                current_page_url=resolution.final_url,
                resolved_vendor=resolution.provider,
            )
            if phase21_prefill_enabled
            else None
        )
        blocked_requests = _query_free_blocked_requests(
            result.get("blocked_requests"),
            strict_first_party_paths=phase21_prefill_enabled,
        )
        egress_notice = (
            str(manifest_value.get("egress_notice") or "")[:240]
            if isinstance(manifest_value, Mapping)
            else ""
        )
        click_boundary_crossed = bool(result.get("click_boundary_crossed"))
        receipt_payload = result.get("receipt")
        receipt = (
            Receipt(
                confirmation_text=str(receipt_payload.get("confirmation_text", "")),
                url=str(receipt_payload.get("url", "")),
                reference=str(receipt_payload.get("reference", "")),
            )
            if isinstance(receipt_payload, Mapping)
            else None
        )
        if state_text == "CONFIRMED" and (
            mode is not RunMode.SUBMIT
            or submission_intent is None
            or not click_boundary_crossed
        ):
            raise SubmissionBlocked(
                "Confirmation lacks the durable authority/intent click boundary",
                code="submission_authority_bypass",
            )
        if submission_intent is not None and state_text != "CONFIRMED":
            # Release the request-session run/application flush before the
            # fresh intent CAS. The owner callback may already have committed
            # CLICKED, and a second SQLite writer must not wait on this stale
            # transaction while we reconcile the post-click truth.
            session.commit()
            if click_boundary_crossed:
                # Once the owner callback crossed the durable boundary, every
                # result that is not a strict confirmation is post-click
                # uncertainty. Never downgrade it to FAILED_LOCAL.
                self._mark_submission_intent_local(
                    session,
                    submission_intent,
                    SubmissionIntent.UNKNOWN,
                )
                if state_text != "UNKNOWN":
                    state_text = "UNKNOWN"
                    blocked_reasons = tuple(
                        dict.fromkeys((*blocked_reasons, "submission_unknown"))
                    )
            elif state_text == "UNKNOWN":
                # No owner callback means no click was armed. A fresh-session
                # CAS makes this a reusable local refusal only while the row
                # is still PREPARED; a concurrent CLICKED row becomes UNKNOWN.
                actual = self._mark_submission_intent_local(
                    session,
                    submission_intent,
                    SubmissionIntent.FAILED_LOCAL,
                )
                if actual == SubmissionIntent.UNKNOWN:
                    state_text = "UNKNOWN"
            elif state_text in {"NEEDS_USER", "ACTIVE", "FINAL_REVIEW"}:
                actual = self._mark_submission_intent_local(
                    session,
                    submission_intent,
                    SubmissionIntent.FAILED_LOCAL,
                )
                if actual == SubmissionIntent.UNKNOWN:
                    state_text = "UNKNOWN"
            else:
                actual = self._mark_submission_intent_local(
                    session,
                    submission_intent,
                    SubmissionIntent.FAILED_LOCAL,
                )
                if actual == SubmissionIntent.UNKNOWN:
                    state_text = "UNKNOWN"
        try:
            session.refresh(application)
        except Exception:  # noqa: BLE001 - the request session may already track the row
            pass
        current_state = ApplicationState(application.state)
        handoff_action = _handoff_next_action(result)
        if handoff_action:
            application.next_action = handoff_action
        elif blocked_requests:
            # ``next_action`` is the durable Needs-You summary for both an
            # incomplete handoff and a successful FINAL_REVIEW handoff. Keep
            # the explicit visual-degradation notice at the end so truncating
            # a long provider reason can never hide it.
            if egress_notice:
                reason_prefix = error
                if reason_prefix.endswith(egress_notice):
                    reason_prefix = reason_prefix[: -len(egress_notice)].rstrip(" ;")
                prefix_budget = max(0, 240 - len(egress_notice) - 2)
                reason_prefix = reason_prefix[:prefix_budget].rstrip(" ;")
                application.next_action = (
                    f"{reason_prefix}; {egress_notice}"
                    if reason_prefix
                    else egress_notice[:240]
                )
            else:
                application.next_action = (
                    error or "Review blocked browser requests"
                )[:240]
        if state_text == "BLOCKED":
            if current_state in {ApplicationState.FILLING, ApplicationState.READY_TO_SUBMIT}:
                self._transition(session, application, ApplicationState.BLOCKED)
        elif state_text == "NEEDS_OA":
            if current_state == ApplicationState.FILLING:
                self._transition(session, application, ApplicationState.NEEDS_OA)
        elif state_text in {"NEEDS_USER", "HUMAN_REQUIRED", "ACTIVE"}:
            if current_state in {ApplicationState.FILLING, ApplicationState.READY_TO_SUBMIT}:
                self._transition(session, application, ApplicationState.NEEDS_USER)
        elif state_text == "FINAL_REVIEW":
            if current_state == ApplicationState.FILLING:
                self._transition(session, application, ApplicationState.READY_TO_SUBMIT)
        elif state_text == "CONFIRMED":
            try:
                # Atomic finalization: strict receipt + intent CONFIRMED +
                # application terminal confirmation commit together in the
                # request session. No independent writer is opened here.
                self._finalize_confirmed_receipt(
                    session,
                    application,
                    submission_intent,
                    result,
                    receipt,
                )
            except Exception as exc:  # noqa: BLE001 - click already crossed boundary
                # The employer-side effect may be real even when local state
                # persistence fails. Preserve the one-shot intent as unknown
                # and make the uncertainty visible rather than retryable.
                error = (
                    f"Receipt persistence failed after submission click: "
                    f"{type(exc).__name__}: {exc}"
                )
                blocked_reasons = tuple(
                    dict.fromkeys((*blocked_reasons, "submission_unknown"))
                )
                try:
                    self._mark_submission_intent_local(
                        session,
                        submission_intent,
                        SubmissionIntent.UNKNOWN,
                    )
                except Exception:
                    # Keep the application marker authoritative even if the
                    # intent row itself cannot be flushed in this transaction.
                    pass
                application.state = ApplicationState.SUBMISSION_UNKNOWN.value
                state_text = "UNKNOWN"
        elif state_text == "UNKNOWN":
            if current_state in {ApplicationState.FILLING, ApplicationState.READY_TO_SUBMIT}:
                self._transition(session, application, ApplicationState.SUBMISSION_UNKNOWN)
        else:
            if current_state == ApplicationState.FILLING:
                self._transition(session, application, ApplicationState.FAILED_RETRYABLE)

        run.adapter = adapter_name
        run.state = application.state
        run.error = error
        run.trace_path = str(trace_path) if trace_path.exists() else ""
        run.screenshot_path = str(screenshot_path) if screenshot_path.exists() else ""
        if hasattr(application, "mark_risk_assessed"):
            application.mark_risk_assessed(risk_level, source=f"automation:{mode.value}:{adapter_name}")
        if hasattr(run, "mark_risk_assessed"):
            run.mark_risk_assessed(risk_level, source=f"automation:{mode.value}:{adapter_name}")
        # Populate receipt_json with per-field manifest for machine-readable
        # audit of what was filled, left blank, failed, or deferred.
        # Each field entry: label, canonical_key, outcome, required, reason_code,
        # and readback_confirmed (True/False) for anything claimed filled.
        # No candidate answer VALUES appear in the receipt.
        if mode in {RunMode.PREFILL, RunMode.REVIEW, RunMode.SUBMIT}:
            receipt_manifest: dict[str, object] = {}
            field_entries: list[dict[str, object]] = []

            # Build a set of prefilled field labels from the manifest
            manifest_prefilled: set[str] = set()
            manifest_failed: set[str] = set()
            manifest_blank: dict[str, str] = {}
            manifest_blank_codes: dict[str, str] = {}
            if isinstance(manifest_value, Mapping):
                raw_prefilled = manifest_value.get("prefilled_fields")
                if isinstance(raw_prefilled, (list, tuple)):
                    manifest_prefilled = {str(f) for f in raw_prefilled}
                raw_failed = manifest_value.get("failed_fields")
                if isinstance(raw_failed, (list, tuple)):
                    manifest_failed = {str(f) for f in raw_failed}
                raw_blank = manifest_value.get("blank_field_reasons")
                if isinstance(raw_blank, (list, tuple)):
                    manifest_blank = {
                        str(b.get("label", "")): str(b.get("reason", ""))
                        for b in raw_blank if isinstance(b, Mapping)
                    }
                raw_blank_codes = manifest_value.get("blank_field_reason_codes")
                if isinstance(raw_blank_codes, (list, tuple)):
                    manifest_blank_codes = {
                        str(b.get("label", "")): str(b.get("reason_code", ""))
                        for b in raw_blank_codes if isinstance(b, Mapping)
                    }

            # Use the plan's actions as the authoritative source of per-field
            # data (canonical_key, required, outcome).
            plan = getattr(journey, "last_plan", None)
            if plan is not None:
                for action in getattr(plan, "actions", []):
                    field = getattr(action, "field", None)
                    if field is None:
                        continue
                    q = getattr(field, "question", None)
                    if q is None:
                        continue
                    label = str(q.label or q.name or "unknown")[:200]
                    # The fill loop records a field as ``name or label`` while
                    # this receipt keys on ``label or name``.  A control with a
                    # name attribute is therefore stored under one identity and
                    # looked up under the other, so no field could ever match
                    # and everything reported readback_unconfirmed.  Match on
                    # either identity, in the producer's order.
                    field_identity = str(q.name or q.label or "unknown")[:200]
                    identities = {label, field_identity}
                    canonical_key = "unknown"
                    mapping = getattr(action, "mapping", None)
                    if mapping is not None:
                        ck = getattr(mapping, "canonical_key", None)
                        if ck is not None:
                            canonical_key = str(getattr(ck, "value", ck) or "unknown")
                    required = bool(q.required)
                    action_status = str(action.status or "unmapped")
                    action_value = action.value
                    action_source = getattr(action, "source", "")
                    outcome = "unknown"
                    reason_code = ""
                    readback_confirmed = False

                    if action_value is not None and action_status == "resolved":
                        # "resolved" means a value was CHOSEN, not that it
                        # reached the page.  Only a read-back proves the field
                        # actually carries it, so an unconfirmed write is
                        # reported as failed rather than filled.  Calling it
                        # "filled" here is what let three runs report a blank
                        # Country/LinkedIn/visa field as complete.
                        if identities & manifest_prefilled:
                            outcome = "filled"
                            readback_confirmed = True
                        else:
                            outcome = "failed"
                            reason_code = "readback_unconfirmed"
                            readback_confirmed = False
                    elif action_status == "unmapped":
                        outcome = "deferred_to_human"
                        reason_code = "no_approved_mapping"
                        if label in manifest_blank_codes:
                            reason_code = manifest_blank_codes.get(label, "no_approved_mapping")
                    elif action_status == "failed":
                        outcome = "failed"
                        reason_code = "prefill_failed"
                        if label in manifest_blank_codes:
                            reason_code = manifest_blank_codes.get(label, "prefill_failed")
                    elif action_status == "handoff":
                        outcome = "deferred_to_human"
                        reason_code = "human_handoff_required"
                    elif action_status == "plausibility_guard":
                        outcome = "failed"
                        reason_code = "plausibility_rejected"
                    elif action_source == "option_not_selected":
                        outcome = "deferred_to_human"
                        reason_code = "no_matching_option"
                    else:
                        outcome = "blank"
                        if identities & set(manifest_blank):
                            reason_code = manifest_blank_codes.get(label, "blank")
                        elif identities & manifest_failed:
                            outcome = "failed"
                            reason_code = "prefill_failed"
                        else:
                            reason_code = "unfilled"

                    field_entries.append({
                        "label": label,
                        "canonical_key": canonical_key,
                        "outcome": outcome,
                        "required": required,
                        "reason_code": reason_code if outcome != "filled" else "",
                        "readback_confirmed": readback_confirmed if outcome == "filled" else False,
                    })

            if field_entries:
                receipt_manifest["fields"] = field_entries
            # Always include run-level metadata (no PII values)
            receipt_manifest["mode"] = mode.value
            receipt_manifest["adapter"] = adapter_name
            receipt_manifest["state"] = state_text
            receipt_manifest["risk_level"] = risk_level
            receipt_manifest["blocked_reasons"] = list(blocked_reasons)
            run.receipt_json = json.dumps(receipt_manifest, default=str)

        run.finished_at = datetime.now(timezone.utc)
        if journey.last_plan is not None:
            self._record_questions(
                session,
                run,
                journey.last_plan,
                failed_field_reasons=question_failure_codes,
            )
        session.flush()
        run_finished_details: dict[str, object] = {
            "application_id": application.id,
            "mode": mode.value,
            "state": application.state,
            "adapter": adapter_name,
            "risk_level": risk_level,
            "blocked_reasons": list(blocked_reasons),
            "blocked_requests": blocked_requests,
            "egress_notice": egress_notice,
            "receipt_reference": receipt.reference if receipt else "",
            "error": error,
            "source_url": resolution.source_url,
            "application_url": resolution.final_url,
        }
        if first_party_requests is not None:
            run_finished_details["first_party_requests"] = first_party_requests
        if captcha_requests is not None:
            run_finished_details["captcha_requests"] = captcha_requests
        append_audit(
            session,
            AuditInput(
                "automation",
                "automation.run_finished",
                "automation_run",
                run.id,
                run_finished_details,
            ),
        )
        # The journey result can remain NEEDS_USER/NEEDS_OA while the
        # Navigator lifecycle is HUMAN_REQUIRED.  Inspect the owner snapshot
        # before cleanup so a human-only boundary is not silently closed or
        # stranded after this request returns.
        try:
            handoff_snapshot = self._settled_handoff_snapshot(
                navigator,
                session_snapshot.session_id,
                state_text,
            )
        except Exception:  # noqa: BLE001 - result fallback remains fail-closed
            handoff_snapshot = None
        human_boundary = dict(result.get("human_boundary") or {}) if isinstance(
            result.get("human_boundary"), Mapping
        ) else {}
        if handoff_snapshot is not None:
            human_boundary = dict(
                getattr(handoff_snapshot, "human_boundary", {}) or human_boundary
            )
        review_ready = bool(
            mode is RunMode.PREFILL
            and state_text == "FINAL_REVIEW"
            and handoff_snapshot is not None
            and getattr(handoff_snapshot, "state", None) is SessionState.FINAL_REVIEW
        )
        handoff_persisted = bool(
            review_ready
            or
            state_text in {"HUMAN_REQUIRED", "NEEDS_USER", "NEEDS_OA"}
            or human_boundary
            or (
                handoff_snapshot is not None
                and getattr(handoff_snapshot, "state", None) is SessionState.HUMAN_REQUIRED
            )
        )
        if not handoff_persisted:
            navigator.close(session_snapshot.session_id, reason="journey completed")
        elif owned_navigator:
            # A direct AutomationRunner call without the application's shared
            # manager still needs a live, addressable owner for Continue/
            # Cancel/status.  Retain it on the runner and expose its session
            # id in the outcome; no second browser is created.
            self._handoff_manager = navigator
            owned_navigator = False
        if owned_navigator:
            navigator.shutdown()
        # Normal completion either fully closed the worker or deliberately
        # published its exact final-review/human handoff session.  The outer
        # exception guard must no longer treat it as an abandoned lease.
        self._active_navigator_lease = None
        return AutomationOutcome(
            state=application.state,
            risk_level=risk_level,
            adapter=adapter_name,
            blocked_reasons=blocked_reasons,
            receipt=receipt,
            trace_path=run.trace_path,
            screenshot_path=run.screenshot_path,
            run_id=run.id,
            source_url=resolution.source_url,
            application_url=resolution.final_url,
            target_status=resolution.kind.value,
            resolution_evidence=dict(resolution.evidence),
            handoff_session_id=(
                session_snapshot.session_id if handoff_persisted else ""
            ),
            human_boundary=human_boundary,
        )

    @staticmethod
    def _settled_handoff_snapshot(navigator, session_id: str, state_text: str):  # noqa: ANN001
        """Observe the owner state that corresponds to the returned journey.

        The journey-result event and final lifecycle-state event are emitted
        by the same owner, but the request thread must not turn a transient
        manager snapshot into cleanup of a valid final-review session.  A
        bounded wait is used only when the journey itself proved FINAL_REVIEW;
        every other result remains an immediate, fail-closed read.
        """

        if str(state_text).upper() == "FINAL_REVIEW":
            wait_for_state = getattr(navigator, "wait_for_state", None)
            if callable(wait_for_state):
                return wait_for_state(
                    session_id,
                    SessionState.FINAL_REVIEW,
                    timeout=1,
                )
        return navigator.get(session_id)

    @staticmethod
    def _resume_states() -> frozenset[ApplicationState]:
        """States a headed review handoff may resume from.

        These are the human-boundary states the Needs-You page lists.  A
        headed re-run must be able to re-enter FILLING from them, otherwise
        "Finish in browser" fails closed and no browser ever opens.
        """
        return frozenset(
            {
                ApplicationState.NEEDS_USER,
                ApplicationState.NEEDS_OA,
                ApplicationState.BLOCKED,
                ApplicationState.FAILED_RETRYABLE,
            }
        )

    def _resume_for_headed_review(
        self,
        session: Session | None,
        application: Application,
        *,
        transition=None,
    ) -> None:
        """Walk a terminal handoff state back into FILLING via allowed edges.

        Only used for headed review runs (the user explicitly asked to finish
        in the visible browser).  Submitted/terminal-success states are never
        resumable.  Every step goes through self._transition (audited and
        state-machine-validated); ``transition`` is injectable for tests.
        """
        step = transition or (
            lambda s, a, t: self._transition(s, a, t)
        )
        current = ApplicationState(application.state)
        if current == ApplicationState.FILLING:
            return
        if current in {
            ApplicationState.SUBMITTED,
            ApplicationState.CONFIRMATION_VERIFIED,
            ApplicationState.OA_PENDING,
            ApplicationState.INTERVIEW,
            ApplicationState.OFFER,
        }:
            raise SubmissionBlocked(
                "Application has already been submitted",
                code="already_submitted",
            )
        if current not in self._resume_states():
            raise SubmissionBlocked(
                f"Application cannot be resumed from {current.value}",
                code="resume_not_allowed",
            )
        if current is ApplicationState.NEEDS_OA:
            # NEEDS_OA only exits forward (OA_PENDING/INTERVIEW/REJECTED);
            # REJECTED is terminal with no outgoing edges, so the state
            # machine offers no legal path back to FILLING.  Fail loudly:
            # the user must re-queue this one from the Applications page.
            raise SubmissionBlocked(
                "Assessment-stage applications cannot resume automation; "
                "re-queue the application manually",
                code="resume_not_allowed",
            )
        if current is ApplicationState.BLOCKED:
            step(session, application, ApplicationState.ELIGIBILITY_CHECKED)
            step(session, application, ApplicationState.QUEUED)
            step(session, application, ApplicationState.PACKAGE_PREPARED)
        elif current is ApplicationState.FAILED_RETRYABLE:
            step(session, application, ApplicationState.FILLING)
            return  # FAILED_RETRYABLE -> FILLING is a direct legal edge
        elif current is ApplicationState.NEEDS_USER:
            step(session, application, ApplicationState.PACKAGE_PREPARED)
        step(session, application, ApplicationState.FILLING)

    def _claim_for_automation(self, session: Session, application: Application) -> None:
        """Claim exclusive in-process automation rights for one application.

        Raises SubmissionBlocked when another run holds the claim or the
        application has already been submitted.  Fail closed: errors propagate.
        """
        with _CLAIM_GUARD:
            if application.id in _ACTIVE_CLAIMS:
                raise SubmissionBlocked(
                    "Another automation run is active for this application",
                    code="automation_claim_conflict",
                )
            # Re-read fresh state via a short-lived separate session so a stale
            # in-memory object cannot mask a concurrent committed submission.
            with self.database.SessionLocal() as db_session:
                fresh = db_session.get(Application, application.id)
            if fresh is None or ApplicationState(fresh.state) in {
                ApplicationState.SUBMITTED,
                ApplicationState.CONFIRMATION_VERIFIED,
                ApplicationState.OA_PENDING,
                ApplicationState.INTERVIEW,
                ApplicationState.OFFER,
            }:
                raise SubmissionBlocked(
                    "Application has already been submitted",
                    code="already_submitted",
                )
            _ACTIVE_CLAIMS.add(application.id)

    @staticmethod
    def _transition(session: Session, application: Application, target: ApplicationState) -> None:
        current = ApplicationState(application.state)
        if current == target:
            return
        validate_transition(current, target)
        application.state = target.value
        session.flush()
        append_audit(
            session,
            AuditInput(
                "automation",
                "application.state_changed",
                "application",
                application.id,
                {"from": current.value, "to": target.value},
            ),
        )

    @staticmethod
    def _record_questions(
        session: Session,
        run: AutomationRun,
        plan: FillPlan,
        *,
        failed_field_reasons: Mapping[str, str] | None = None,
    ) -> None:
        failure_map = {
            str(key): str(value)
            for key, value in (failed_field_reasons or {}).items()
            if str(key) and str(value)
        }
        failure_reason_text = {
            "no_matching_option": "no matching option was offered by the widget; review required",
            "ambiguous_option": "multiple offered options matched; review required",
            "commit_failed": "the widget did not commit the offered option; review required",
            "control_unavailable": "the combobox was unavailable; review required",
            "restoration_unverified": "selection could not be restored or verified; reselect an option",
        }
        for action in plan.actions:
            label = " ".join(str(action.field.question.label or "").split())
            field_identity = action.field.question.name or action.field.question.label
            failure_code = failure_map.get(field_identity) or failure_map.get(label)
            failed = bool(failure_code)
            session.add(
                QuestionRecord(
                    run_id=run.id,
                    label=action.field.question.label,
                    field_type=action.field.question.field_type,
                    canonical_key=action.mapping.canonical_key.value,
                    confidence=action.mapping.confidence,
                    sensitivity=action.mapping.sensitivity.value,
                    answer_source="prefill_failed" if failed else action.source,
                    answer_preview="" if failed else redact_answer_preview(action),
                    mapping_status="blocked" if failed else action.status,
                    reason=(
                        failure_reason_text.get(
                            failure_code or "",
                            "approved value could not be committed; review required",
                        )
                        if failed
                        else action.mapping.reason
                    ),
                )
            )
        session.flush()

    def _document_lookup(self, session: Session, application: Application) -> Callable[[str], ResolvedFieldValue | None]:
        document_service = DocumentService(session, self.settings.documents_dir)

        def lookup(key: str) -> ResolvedFieldValue | None:
            document_id = (
                application.selected_cv_id
                if key == CanonicalKey.CV.value
                else application.selected_cover_letter_id
                if key == CanonicalKey.COVER_LETTER.value
                else None
            )
            if not document_id:
                return None
            document = session.get(Document, document_id)
            if document is None or not document.approved or not document_service.verify(document):
                return None
            return ResolvedFieldValue(document.path, "approved_document", False)

        return lookup

    def run(
        self,
        application_id: str,
        mode: RunMode,
        headed: bool = False,
        session_id: str = "",
        authority_id: str = "",
    ) -> AutomationOutcome:
        self.settings.ensure_directories()
        notification_event: HumanAttentionEvent | None = None
        claimed_application_id = ""
        # Claim before any state mutation: the in-process claim set plus a
        # fresh committed-state re-read close the concurrent-run race for one
        # application (cross-process protection comes from the runtime lock
        # and the sweep lock).  Fail closed on any claim error.
        try:
            with self.database.session_scope() as session:
                application = session.get(Application, application_id)
                if application is None:
                    raise KeyError(f"Application not found: {application_id}")
                self._claim_for_automation(session, application)
                claimed_application_id = application.id
                try:
                    outcome = self._run_claimed(
                        session,
                        application,
                        application_id,
                        mode,
                        headed,
                        session_id,
                        authority_id,
                    )
                    try:
                        notification_event = self._human_attention_event(
                            application, outcome
                        )
                        if notification_event is not None:
                            queue_application_notification(session, notification_event)
                    except Exception as exc:  # noqa: BLE001 - notifier is optional
                        LOGGER.warning(
                            "notification event construction failed; application run "
                            "continues (%s: %s)",
                            type(exc).__name__,
                            exc,
                        )
                        notification_event = None
                except BaseException:
                    # Submission authority/document/state refusal is
                    # intentionally pre-click, but the execution browser was
                    # already created to perform owner-thread preflight.  It
                    # must not survive the 409 response or block app shutdown.
                    self._cleanup_active_navigator_lease(
                        "automation request failed before completion"
                    )
                    raise
            return outcome
        finally:
            if claimed_application_id:
                with _CLAIM_GUARD:
                    _ACTIVE_CLAIMS.discard(claimed_application_id)

    @staticmethod
    def _human_attention_event(
        application: Application,
        outcome: AutomationOutcome,
    ) -> HumanAttentionEvent | None:
        if outcome.state not in {
            ApplicationState.NEEDS_USER.value,
            ApplicationState.NEEDS_OA.value,
        }:
            return None
        boundary_kind = ""
        boundary_detail = ""
        if isinstance(outcome.human_boundary, Mapping):
            boundary_kind = str(outcome.human_boundary.get("kind") or "").strip()
            boundary_detail = str(outcome.human_boundary.get("reason") or "").strip()
        specific_codes = tuple(
            code for code in outcome.blocked_reasons if code != "human_boundary"
        )
        reason = select_human_attention_reason(
            state=outcome.state,
            boundary_kind=boundary_kind,
            blocked_reasons=specific_codes,
            detail=" ".join((boundary_detail, str(application.next_action or ""))),
        )
        return HumanAttentionEvent.from_application(application, reason=reason)

    def _run_claimed(
        self,
        session: Session,
        application: Application,
        application_id: str,
        mode: RunMode,
        headed: bool,
        session_id: str = "",
        authority_id: str = "",
    ) -> AutomationOutcome:
        return self._run_claimed_owner(
            session, application, application_id, mode, headed, session_id, authority_id
        )
