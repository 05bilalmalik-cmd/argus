"""Owner-thread application browser navigator.

The sync Playwright API is intentionally isolated in :class:`HeadedSessionWorker`.
Request threads only enqueue immutable commands and consume immutable events.  A
worker owns one Playwright runtime for its complete lifetime, including startup,
event pumping, human-boundary rescans, and teardown.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import queue
import re
import threading
import time
import unicodedata
import uuid
from html import unescape as html_unescape
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

try:  # Playwright is a runtime dependency, but imports stay testable without it.
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - exercised only in a missing dependency env
    sync_playwright = None  # type: ignore[assignment]

from app.automation.types import (
    RunMode,
    SessionCommand,
    SessionCommandType,
    SessionEvent,
    SessionDiagnostics,
    SessionEventType,
    SessionSnapshot,
    SessionState,
)
from app.automation.host_policy import (
    CAPTCHA_PROVIDER_HOSTS,
    BlockedRequestImpact,
    authorize_captcha_request,
    canonical_first_party_service_evidence_path,
    classify_egress,
    classify_blocked_request_impact,
    classify_request,
    origin_for_url,
    request_carries_candidate_data,
    safe_public_navigation_url,
)
from app.automation.targets import (
    BOUND_TARGET_URL_EVIDENCE_KEYS,
    EMPLOYER_EVIDENCE_KEYS,
    FRAME_URL_EVIDENCE_KEYS,
    FORM_IDENTITY_EVIDENCE_KEYS,
    ORIGIN_EVIDENCE_KEYS,
    PATH_EVIDENCE_KEYS,
    PROVIDER_EVIDENCE_KEYS,
    REQUISITION_EVIDENCE_KEYS,
    ROLE_EVIDENCE_KEYS,
    ROOT_SELECTOR_EVIDENCE_KEYS,
    ROOT_TOKEN_EVIDENCE_KEYS,
    FormHandle,
    TargetResolution,
    canonical_target_contract_url,
    classify_target,
    normalise_evidence_key,
    trusted_provider_for_url,
    validate_target_resolution_contract,
)
from app.domain.targets import TargetKind, validate_navigation_url
from app.services.resolution_apply_click import (
    ApplyAffordance,
    ApplyCandidate,
    ApplyClickSafetyViolation,
    ApplyClickBudget,
    is_explicit_apply_name,
    is_submission_or_confirmation_landing,
    guarded_apply_click,
    select_ranked_apply_candidate,
)
from app.services.requisition_identity import (
    RequisitionIdentity,
    identity_matches_url,
    requisition_identity_from_url,
    requisitions_equal,
)
from app.services.role_identity import (
    RoleIdentityMatch,
    RoleTitleCandidate,
    match_role_identity_v2,
)

logger = logging.getLogger(__name__)

_BOUND_SHORTLINK_HOSTS = frozenset({"grnh.se"})
_PLACEHOLDER_EMPLOYERS = frozenset({"", "unknown"})
_OPEN_REDIRECT_QUERY_KEYS = frozenset(
    {
        "continue",
        "destination",
        "next",
        "redirect",
        "redirect_to",
        "redirect_uri",
        "return",
        "return_to",
        "target",
        "url",
    }
)


_JSON_LD_SCRIPT = re.compile(
    r"<script\b[^>]*\btype\s*=\s*['\"]application/ld\+json['\"][^>]*>"
    r"(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _structured_job_posting(markup: str) -> dict[str, str]:
    """Return bounded, page-authored JobPosting identity evidence."""

    def nodes(value: Any, *, depth: int = 0):
        if depth > 5:
            return
        if isinstance(value, Mapping):
            yield value
            for child in list(value.values())[:100]:
                yield from nodes(child, depth=depth + 1)
        elif isinstance(value, list):
            for child in value[:100]:
                yield from nodes(child, depth=depth + 1)

    for match in _JSON_LD_SCRIPT.finditer(str(markup or "")[:200_000]):
        try:
            payload = json.loads(html_unescape(match.group(1)).strip())
        except (TypeError, ValueError):
            continue
        for item in nodes(payload):
            raw_type = item.get("@type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            if not any(str(value).casefold() == "jobposting" for value in types):
                continue
            organisation = item.get("hiringOrganization")
            identifier = item.get("identifier")
            employer = (
                str(organisation.get("name") or "")
                if isinstance(organisation, Mapping)
                else ""
            )
            if isinstance(identifier, Mapping):
                requisition = str(
                    identifier.get("value")
                    or identifier.get("identifier")
                    or identifier.get("name")
                    or ""
                )
            else:
                requisition = str(identifier or "")
            result = {
                "employer": employer.strip()[:500],
                "role": str(item.get("title") or "").strip()[:500],
                "requisition": requisition.strip()[:500],
            }
            if all(result.values()):
                return result
    return {}


class NavigatorError(RuntimeError):
    """Base error raised by the local navigator command surface."""


class DuplicateSessionError(NavigatorError):
    """Raised when an application already has a non-terminal session."""


class SessionNotFoundError(KeyError):
    """Raised for an unknown session identifier."""


class SessionCommandRejected(NavigatorError):
    """Raised when a command cannot be queued for a session."""


class NavigatorShutdownError(NavigatorError):
    """Raised when shutdown cannot prove every worker tore down cleanly."""

    def __init__(self, incomplete_session_ids: tuple[str, ...]):
        self.incomplete_session_ids = tuple(incomplete_session_ids)
        detail = ", ".join(self.incomplete_session_ids) or "unknown"
        super().__init__(f"Navigator shutdown incomplete for session(s): {detail}")


_SERVICE_NAVIGATOR_ATTRIBUTE = "_argus_service_navigator"


def bind_service_navigator(database: Any, navigator: object) -> None:
    """Bind the one application-owned Navigator to a database instance.

    Runner construction can happen in a request, scheduler, or CLI adapter.
    The database object is the common in-process application boundary, so it
    carries a scalar reference to the already-created service owner instead of
    allowing one of those callers to create an unobservable second manager.
    Rebinding to a different owner is refused because two registries would
    make a human handoff ambiguous.
    """

    if database is None or navigator is None:
        raise ValueError("A database and service navigator are required")
    current = getattr(database, _SERVICE_NAVIGATOR_ATTRIBUTE, None)
    if current is not None and current is not navigator:
        raise NavigatorError("A different service navigator is already bound")
    setattr(database, _SERVICE_NAVIGATOR_ATTRIBUTE, navigator)


def service_navigator_for(database: Any) -> object | None:
    """Return the application-owned Navigator, if the service installed one."""

    if database is None:
        return None
    return getattr(database, _SERVICE_NAVIGATOR_ATTRIBUTE, None)


_ACTIVE_STATES = frozenset(
    {
        SessionState.OPENING,
        SessionState.ACTIVE,
        SessionState.HUMAN_REQUIRED,
        SessionState.FINAL_REVIEW,
    }
)

_DEFAULT_CLEANUP_GRACE_SECONDS = 5.0
_MAX_CLEANUP_COMMANDS = 3
_DEFAULT_TERMINAL_SESSION_RETENTION_SECONDS = 300.0
_DEFAULT_EXPIRY_PRESERVATION_SECONDS = 7 * 24 * 60 * 60

# Bounded wait for an ATS single-page app to render its posting.  This only
# decides how long to look for a readiness signal before recording a
# descriptive timeout; it grants no capability and relaxes no gate.
#
# Measured 2026-08-29: raising the Workday budget to 9s did NOT change the
# outcome — a Workday posting returns HTTP 200 and still reports
# ``hydration: timeout``, so the app is not merely slow, it never boots.  The
# open hypothesis is that Workday serves its bundles from sibling hosts which
# the egress guard blocks, exactly as Greenhouse did before Phase 21.  Any
# per-provider budget here must follow that measurement, not precede it.
_DEFAULT_HYDRATION_TIMEOUT_MS = 2_500
_PROVIDER_HYDRATION_TIMEOUT_MS: dict[str, int] = {}

# Commands which can advance a browser journey or arm/confirm an external
# side-effect are deadline-sensitive.  Cleanup controls remain usable after
# expiry so a caller can still close the owner-thread browser deterministically.
_DEADLINE_SENSITIVE_COMMANDS = frozenset(
    {
        SessionCommandType.CONTINUE,
        SessionCommandType.FINAL_MANIFEST,
        SessionCommandType.PREFLIGHT_SUBMISSION,
        SessionCommandType.CONFIRM,
        SessionCommandType.CONFIRM_SUBMISSION,
        SessionCommandType.RUN_JOURNEY,
        SessionCommandType.INJECT_HUMAN_REQUIRED,
        SessionCommandType.CAPTCHA,
    }
)

_FINAL_MANIFEST_FIELDS = (
    "application_id",
    "employer",
    "role",
    "provider",
    "target_fingerprint",
    "control_fingerprint",
    "form_action",
    "method",
    "expires_at",
    "final_url",
    "expected_receipt_url",
)


def _manifest_text(manifest: Mapping[str, object], *keys: str) -> str:
    """Return the first non-empty scalar manifest value for one alias set."""

    for key in keys:
        value = manifest.get(key)
        if value is None or isinstance(value, (Mapping, list, tuple, set, frozenset)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _manifest_missing_fields(manifest: Mapping[str, object], *, provider: str = "") -> tuple[str, ...]:
    """Return load-bearing FINAL_REVIEW fields which are absent.

    The Navigator is intentionally stricter than the display layer: a summary
    or a DOM title may be useful context, but it is never a substitute for the
    exact target/form binding required by a one-time confirmation.
    """

    aliases = {
        "target_fingerprint": ("target_fingerprint", "target_fingerprint_sha256"),
        "control_fingerprint": ("control_fingerprint", "form_fingerprint"),
        "method": ("method", "form_method"),
        "expected_receipt_url": (
            "expected_receipt_url",
            "expected_final_url",
        ),
    }
    missing: list[str] = []
    for field_name in _FINAL_MANIFEST_FIELDS:
        keys = aliases.get(field_name, (field_name,))
        if not _manifest_text(manifest, *keys):
            missing.append(field_name)
    # All supported providers carry a requisition/path proof in the verified
    # target contract.  Keep the field explicit in the manifest and refuse a
    # confirmation if it has disappeared between inspection and review.
    if str(provider or manifest.get("provider") or "").casefold() in {
        "greenhouse",
        "lever",
        "workday",
    } and not _manifest_text(manifest, "requisition", "requisition_id", "job_id"):
        missing.append("requisition")
    return tuple(dict.fromkeys(missing))


# These fields are the owner-thread portion of the durable submission
# authority contract.  They deliberately exclude presentation/TTL values
# (title, step counters, expiry, and submission labels) while retaining the
# exact page/form/control identity which must remain unchanged after the user
# reviews the final manifest.  The service layer may add persisted document
# and form-identity evidence; the Navigator can only attest to the evidence
# it owns in the browser thread.
_PREFLIGHT_BINDING_FIELDS = (
    "application_id",
    "employer",
    "role",
    "requisition",
    "provider",
    "form_identity",
    "destination",
    "frame_url",
    "page_id",
    "root_selector",
    "control_selector",
    "target_fingerprint",
    "control_fingerprint",
    "form_action",
    "method",
    "expected_final_url",
    "final_url",
)


def _manifest_binding_projection(manifest: Mapping[str, object]) -> dict[str, str]:
    """Return canonical owner-thread binding evidence for a manifest.

    The projection is intentionally scalar and deterministic so a request
    thread can compare the final-review evidence with a fresh owner-thread
    read without receiving a Playwright object.  URL aliases and HTTP method
    aliases are folded to the same representation; all other values are
    compared as exact strings.
    """

    aliases = {
        "expected_final_url": ("expected_final_url", "expected_receipt_url"),
        "target_fingerprint": ("target_fingerprint", "target_fingerprint_sha256"),
        "control_fingerprint": ("control_fingerprint", "form_fingerprint"),
        "form_action": ("form_action", "action"),
        "method": ("method", "form_method"),
    }
    projection: dict[str, str] = {}
    for field_name in _PREFLIGHT_BINDING_FIELDS:
        names = aliases.get(field_name, (field_name,))
        value = _manifest_text(manifest, *names)
        if field_name == "method":
            value = value.upper()
        elif field_name in {
            "form_action",
            "expected_final_url",
            "final_url",
            "destination",
            "frame_url",
        } and value:
            # Do not let superficial URL spelling differences make the
            # browser-side binding disagree with the service-side contract.
            try:
                value = canonical_target_contract_url(value)
            except (TypeError, ValueError):
                # `_manifest_is_current` reports malformed URLs separately;
                # retaining the raw value here keeps the mismatch diagnostic
                # deterministic and fail-closed.
                value = str(value)
        projection[field_name] = value
    return projection

_CAPTCHA_SCRIPT = """
() => {
  const visible = (node) => {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      rect.width > 0 && rect.height > 0;
  };
  const selector = [
    'iframe[src*="captcha"]', 'iframe[title*="captcha" i]',
    '[data-captcha]', '[data-sitekey]', '.g-recaptcha', '.h-captcha',
    '#captcha', '[id*="captcha" i]', '[class*="captcha" i]'
  ].join(',');
  for (const node of document.querySelectorAll(selector)) {
    if (visible(node)) return {captcha: true, reason: 'captcha_detected'};
  }
  const text = (document.body && document.body.innerText || '').toLowerCase();
  const phrases = [
    ['verify you are human', 'human_verification'],
    ['prove you are human', 'human_verification'],
    ['complete the captcha', 'captcha_detected'],
    ['captcha required', 'captcha_detected'],
    ['multi-factor authentication', 'mfa_required'],
    ['two-factor authentication', 'mfa_required']
  ];
  for (const [phrase, reason] of phrases) {
    if (text.includes(phrase)) return {captcha: true, reason};
  }
  return {captcha: false, reason: ''};
}
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_mode(mode: RunMode | str) -> str:
    if isinstance(mode, RunMode):
        return mode.value
    raw = str(mode).strip()
    if not raw:
        raise ValueError("Navigator mode is required")
    # Keep unknown future modes serialisable while retaining the existing enum
    # values as the canonical API representation.
    try:
        return RunMode(raw).value
    except ValueError:
        return raw


def _loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _host_allowed(hostname: str | None, allowlist: frozenset[str]) -> bool:
    if _loopback_host(hostname):
        return True
    if not hostname:
        return False
    value = hostname.casefold().rstrip(".")
    for allowed in allowlist:
        candidate = allowed.casefold().rstrip(".")
        if candidate.startswith("*."):
            if value == candidate[2:] or value.endswith("." + candidate[2:]):
                return True
        elif value == candidate:
            return True
    return False


def _validate_navigation_url(
    navigation_url: str,
    allowlist: frozenset[str],
) -> str:
    """Validate an initial URL before creating a worker or browser."""

    value = str(navigation_url or "").strip() or "about:blank"
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError(f"navigation URL has an invalid hostname: {exc}") from exc
    if parsed.scheme in {"http", "https"}:
        if not hostname:
            raise ValueError("navigation URL requires a hostname")
        if not _host_allowed(hostname, allowlist):
            raise ValueError(f"navigation URL blocked by local egress policy: {value}")
    elif parsed.scheme not in {"about", "blob", "data"}:
        raise ValueError(
            f"navigation URL scheme is not allowed: {parsed.scheme or '<missing>'}"
        )
    return value


@dataclass(slots=True)
class SourceResolutionCapability:
    """Revocable permission for one authoritative public-HTTPS source URL.

    The capability is deliberately an in-memory value passed to one Navigator
    call.  It never mutates Settings or the configured live-domain allowlist.
    A caller must revoke it after the owning Navigator has been shut down.
    """

    source_url: str
    hostname: str
    origin: str
    capability_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _active: bool = True

    @classmethod
    def issue(cls, source_url: str) -> "SourceResolutionCapability":
        try:
            validated = validate_navigation_url(source_url)
            parsed = urlsplit(validated)
            hostname = (parsed.hostname or "").casefold().rstrip(".")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Target resolution requires a public HTTPS source URL"
            ) from exc
        if not hostname or not safe_public_navigation_url(validated):
            raise ValueError("Target resolution requires a public HTTPS source URL")
        return cls(
            source_url=validated,
            hostname=hostname,
            origin=origin_for_url(validated),
        )

    @property
    def active(self) -> bool:
        return self._active

    def assert_authorizes(self, source_url: str) -> None:
        if not self._active:
            raise ValueError("Target-resolution source capability has been revoked")
        try:
            candidate = validate_navigation_url(source_url)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Target-resolution source capability does not match the stored source"
            ) from exc
        if candidate != self.source_url:
            raise ValueError(
                "Target-resolution source capability does not match the stored source"
            )

    def assert_authorizes_navigation(self, destination_url: str) -> None:
        """Require click-driven navigation to remain on this exact origin."""

        if not self._active:
            raise ValueError("Target-resolution source capability has been revoked")
        try:
            destination_origin = origin_for_url(
                validate_navigation_url(destination_url)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Target-resolution navigation is outside the capability exact origin"
            ) from exc
        if destination_origin != self.origin:
            raise ValueError(
                "Target-resolution navigation is outside the capability exact origin"
            )

    def revoke(self) -> None:
        self._active = False


def _request_url_without_fragment(value: str) -> str:
    try:
        parsed = urlsplit(validate_navigation_url(value))
    except (TypeError, ValueError):
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _request_redirect_chain(request: Any) -> tuple[str, ...]:
    """Return a bounded, root-to-leaf Playwright redirect chain."""

    chain: list[str] = []
    seen: set[int] = set()
    current = request
    for _ in range(12):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        raw_url = getattr(current, "url", "")
        try:
            raw_url = raw_url() if callable(raw_url) else raw_url
        except Exception:  # noqa: BLE001 - opaque redirect evidence is refused
            return ()
        normalised = _request_url_without_fragment(str(raw_url or ""))
        if not normalised:
            return ()
        chain.append(normalised)
        try:
            previous = getattr(current, "redirected_from", None)
            current = previous() if callable(previous) else previous
        except Exception:  # noqa: BLE001 - opaque redirect evidence is refused
            return ()
    if current is not None:
        return ()
    chain.reverse()
    return tuple(chain)


def _bound_shortlink_source(value: str) -> bool:
    """Recognise an opaque, query-free shortlink that cannot encode a target."""

    try:
        parsed = urlsplit(validate_navigation_url(value))
        port = parsed.port
    except (TypeError, ValueError):
        return False
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    return bool(
        parsed.scheme.casefold() == "https"
        and port in {None, 443}
        and hostname in _BOUND_SHORTLINK_HOSTS
        and not parsed.query
        and not parsed.fragment
        and re.fullmatch(r"/[A-Za-z0-9_-]{6,128}/?", parsed.path or "")
    )


def _evidence_values(value: Any, aliases: frozenset[str], *, depth: int = 0):
    """Yield bounded scalar identity evidence from nested resolution data."""

    if depth > 8:
        return
    if isinstance(value, Mapping):
        for raw_key, item in list(value.items())[:200]:
            key = "".join(character for character in str(raw_key).casefold() if character.isalnum())
            if key in aliases and not isinstance(item, Mapping):
                text = str(item or "").strip()
                if text:
                    yield text
            yield from _evidence_values(item, aliases, depth=depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in list(value)[:200]:
            yield from _evidence_values(item, aliases, depth=depth + 1)


def _resolution_is_complete(resolution: TargetResolution) -> bool:
    """Require the full verified identity bundle before a worker exists."""

    if not isinstance(resolution, TargetResolution):
        return False
    try:
        if not resolution.verified_for_automation:
            return False
        verified_origin = origin_for_url(resolution.final_url)
    except (AttributeError, TypeError, ValueError):
        return False
    evidence = resolution.evidence
    provider = str(resolution.provider or "").strip().casefold()
    employers = tuple(
        _evidence_values(evidence, EMPLOYER_EVIDENCE_KEYS)
    )
    roles = tuple(
        _evidence_values(evidence, ROLE_EVIDENCE_KEYS)
    )
    requisitions = tuple(
        _evidence_values(
            evidence,
            REQUISITION_EVIDENCE_KEYS | PATH_EVIDENCE_KEYS,
        )
    )
    # A root token identifies the current DOM root; it is deliberately not a
    # form identity.  Requiring the explicit form-identity aliases here keeps
    # a URL/root marker from upgrading an otherwise unbound form.
    form_identity = tuple(_evidence_values(evidence, FORM_IDENTITY_EVIDENCE_KEYS))
    origin_candidates = tuple(
        _evidence_values(
            evidence,
            ORIGIN_EVIDENCE_KEYS
            | BOUND_TARGET_URL_EVIDENCE_KEYS
            | FRAME_URL_EVIDENCE_KEYS,
        )
    )
    origin_proven = False
    for candidate in origin_candidates:
        try:
            if origin_for_url(candidate) == verified_origin:
                origin_proven = True
                break
        except ValueError:
            continue
    return bool(provider and employers and roles and requisitions and form_identity and origin_proven)


class PersistedTargetResolutionError(ValueError):
    """Raised when persisted target proof is missing, stale, or contradictory."""

    def __init__(self, message: str, *, code: str = "target_evidence_invalid") -> None:
        super().__init__(message)
        self.code = code


def _canonical_identity_text(value: object) -> str:
    """Canonicalize human/ATS identity text without trusting page prose."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(
        "".join(character if character.isalnum() else " " for character in text).split()
    )


def _canonical_path_identity(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if "://" in text:
        try:
            text = urlsplit(validate_navigation_url(text)).path
        except ValueError:
            return ""
    return _canonical_identity_text(text.rstrip("/"))


def _scalar_evidence_values(value: object, aliases: frozenset[str], *, depth: int = 0):
    """Yield scalar values only from explicit structured evidence keys."""

    if depth > 8:
        return
    if isinstance(value, Mapping):
        for raw_key, item in list(value.items())[:200]:
            key = re.sub(r"[^a-z0-9]", "", str(raw_key).casefold())
            if key in aliases and not isinstance(item, (Mapping, list, tuple, set, frozenset, bool)):
                text = str(item or "").strip()
                if text and text.casefold() not in {"none", "null", "false"}:
                    yield text
            yield from _scalar_evidence_values(item, aliases, depth=depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in list(value)[:200]:
            yield from _scalar_evidence_values(item, aliases, depth=depth + 1)


def _mapping_nodes(value: object, *, depth: int = 0):
    if depth > 8:
        return
    if isinstance(value, Mapping):
        yield value
        for item in list(value.values())[:200]:
            yield from _mapping_nodes(item, depth=depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in list(value)[:200]:
            yield from _mapping_nodes(item, depth=depth + 1)


def _mapping_scalars(mapping: Mapping, aliases: frozenset[str]) -> list[str]:
    values: list[str] = []
    for raw_key, item in mapping.items():
        key = normalise_evidence_key(raw_key)
        if key in aliases and not isinstance(item, (Mapping, list, tuple, set, frozenset, bool)):
            text = str(item or "").strip()
            if text and text.casefold() not in {"none", "null", "false"}:
                values.append(text)
    return values


def _mapping_scalar(mapping: Mapping, aliases: frozenset[str]) -> str:
    values = _mapping_scalars(mapping, aliases)
    return values[0] if values else ""


def _require_canonical_values(
    values: list[str], expected: str, *, label: str, canonicalizer=_canonical_identity_text
) -> None:
    if not values:
        raise PersistedTargetResolutionError(
            f"Serialized target-resolution {label} evidence is missing"
        )
    canonical_expected = canonicalizer(expected)
    if not canonical_expected or any(canonicalizer(value) != canonical_expected for value in values):
        raise PersistedTargetResolutionError(
            f"Serialized target-resolution {label} evidence contradicts the verified contract"
        )


def _load_verified_target_from_opportunity(opportunity: Any) -> TargetResolution:
    """Load and cross-bind one immutable persisted TargetResolution envelope.

    This is intentionally shared by Navigator DB resolution and Automation
    Runner resolution.  ``Opportunity`` columns are authoritative indexes and
    eligibility gates; every structured nested proof value must agree with the
    serialized envelope, stored identity, target origin, and form binding.
    """

    target_url = getattr(opportunity, "automation_url", None)
    if not target_url:
        raise PersistedTargetResolutionError(
            "No verified application URL is available; the source listing remains source-only",
            code="target_unresolved",
        )
    if getattr(opportunity, "resolved_at", None) is None:
        raise PersistedTargetResolutionError(
            "Application target resolution is not current",
            code="target_unresolved",
        )
    try:
        stored = json.loads(opportunity.resolution_evidence_json or "{}")
    except (TypeError, ValueError) as exc:
        raise PersistedTargetResolutionError(
            "Target-resolution evidence is malformed"
        ) from exc
    if not isinstance(stored, Mapping):
        raise PersistedTargetResolutionError("Target-resolution evidence is not an object")
    required = (
        "source_url",
        "final_url",
        "kind",
        "provider",
        "identity_verified",
        "form_verified",
        "evidence",
    )
    if any(key not in stored for key in required):
        raise PersistedTargetResolutionError("Serialized target-resolution envelope is incomplete")
    evidence = stored.get("evidence")
    if not isinstance(evidence, Mapping):
        raise PersistedTargetResolutionError(
            "Serialized target-resolution envelope evidence is malformed"
        )
    try:
        envelope_source = validate_navigation_url(str(stored["source_url"]))
        envelope_final = validate_navigation_url(str(stored["final_url"]))
        envelope_source_contract = canonical_target_contract_url(envelope_source)
        envelope_final_contract = canonical_target_contract_url(envelope_final)
        envelope_kind = TargetKind(str(stored["kind"]))
        authoritative_kind = TargetKind(str(opportunity.target_status))
        authoritative_source = validate_navigation_url(
            str(getattr(opportunity, "navigation_url", None) or opportunity.url)
        )
        authoritative_final = validate_navigation_url(str(opportunity.application_url or ""))
        authoritative_source_contract = canonical_target_contract_url(authoritative_source)
        authoritative_final_contract = canonical_target_contract_url(authoritative_final)
    except (AttributeError, TypeError, ValueError) as exc:
        raise PersistedTargetResolutionError(
            "Serialized target-resolution envelope is malformed"
        ) from exc
    authoritative_provider = _canonical_identity_text(opportunity.resolved_ats_type)
    envelope_provider = _canonical_identity_text(stored["provider"])
    historical_verified = stored.get("verified_target")
    if historical_verified is not None and not isinstance(historical_verified, Mapping):
        raise PersistedTargetResolutionError("Serialized verified-target envelope is malformed")
    historical_disagrees = False
    if isinstance(historical_verified, Mapping):
        historical_url = historical_verified.get("application_url")
        if "application_url" in historical_verified:
            try:
                historical_disagrees = (
                    canonical_target_contract_url(str(historical_url or ""))
                    != authoritative_final_contract
                )
            except (TypeError, ValueError):
                historical_disagrees = True
        historical_disagrees = historical_disagrees or (
            "resolved_ats_type" in historical_verified
            and _canonical_identity_text(historical_verified.get("resolved_ats_type"))
            != authoritative_provider
        ) or (
            "target_status" in historical_verified
            and str(historical_verified.get("target_status") or "") != authoritative_kind.value
        )
    if (
        envelope_source_contract != authoritative_source_contract
        or envelope_final_contract != authoritative_final_contract
        or envelope_kind is not authoritative_kind
        or envelope_provider != authoritative_provider
        or historical_disagrees
        or not isinstance(stored["identity_verified"], bool)
        or not isinstance(stored["form_verified"], bool)
        or not stored["identity_verified"]
        or bool(stored["form_verified"]) != (envelope_kind is TargetKind.APPLICATION_FORM)
    ):
        raise PersistedTargetResolutionError(
            "Serialized target-resolution envelope disagrees with authoritative target columns"
        )

    provider_values = list(_scalar_evidence_values(evidence, PROVIDER_EVIDENCE_KEYS))
    employer_values = list(_scalar_evidence_values(evidence, EMPLOYER_EVIDENCE_KEYS))
    role_values = list(_scalar_evidence_values(evidence, ROLE_EVIDENCE_KEYS))
    _require_canonical_values(provider_values, authoritative_provider, label="provider")
    _require_canonical_values(employer_values, str(opportunity.employer), label="employer")
    _require_canonical_values(role_values, str(opportunity.role_title), label="role")

    requisition_values = list(
        _scalar_evidence_values(evidence, REQUISITION_EVIDENCE_KEYS)
    )
    path_values = list(_scalar_evidence_values(evidence, PATH_EVIDENCE_KEYS))
    requisition_values.extend(
        str(stored[key])
        for key in ("requisition", "requisition_id", "job_id", "posting_id")
        if key in stored and str(stored[key] or "").strip()
    )
    if isinstance(historical_verified, Mapping):
        requisition_values.extend(
            str(historical_verified[key])
            for key in ("requisition", "requisition_id", "job_id", "posting_id")
            if key in historical_verified and str(historical_verified[key] or "").strip()
        )
    if not requisition_values:
        requisition_values.extend(path_values)
    if not requisition_values:
        raise PersistedTargetResolutionError(
            "Serialized target-resolution requisition evidence is missing"
        )
    final_path = _canonical_path_identity(urlsplit(envelope_final).path)
    path_like_requisitions = [
        value
        for value in requisition_values
        if str(value).strip().startswith(("/", "http://", "https://"))
        or "/" in str(value)
    ]
    identifier_requisitions = [
        value for value in requisition_values if value not in path_like_requisitions
    ]
    if identifier_requisitions:
        _require_canonical_values(
            identifier_requisitions,
            identifier_requisitions[0],
            label="requisition",
            canonicalizer=_canonical_identity_text,
        )
    if path_like_requisitions:
        _require_canonical_values(
            path_like_requisitions,
            path_like_requisitions[0],
            label="requisition path",
            canonicalizer=_canonical_path_identity,
        )
        if any(_canonical_path_identity(value) != final_path for value in path_like_requisitions):
            raise PersistedTargetResolutionError(
                "Serialized target-resolution requisition path is not bound to the verified target"
            )
    requisition_contract = _canonical_identity_text(
        identifier_requisitions[0]
        if identifier_requisitions
        else path_like_requisitions[0]
    )
    for path_value in path_values:
        if _canonical_path_identity(path_value) != _canonical_path_identity(path_values[0]):
            raise PersistedTargetResolutionError(
                "Serialized target-resolution path aliases contradict one another"
            )
        if _canonical_path_identity(path_value) != final_path:
            raise PersistedTargetResolutionError(
                "Serialized target-resolution path evidence is not bound to the verified target"
            )
    path_bound = bool(path_values) or bool(
        path_like_requisitions
        or (requisition_contract and requisition_contract in final_path)
    )

    bound_form_identity_aliases = frozenset({"boundformidentity"})
    form_values = list(
        _scalar_evidence_values(
            evidence,
            FORM_IDENTITY_EVIDENCE_KEYS - bound_form_identity_aliases,
        )
    )
    bound_form_identity_values = list(
        _scalar_evidence_values(evidence, bound_form_identity_aliases)
    )
    form_values.extend(
        str(stored[key])
        for key in ("form_identity", "form_id", "application_form")
        if key in stored and str(stored[key] or "").strip()
    )
    stored_bound_form_identity = str(stored.get("bound_form_identity") or "").strip()
    if stored_bound_form_identity:
        bound_form_identity_values.append(stored_bound_form_identity)
    if isinstance(historical_verified, Mapping):
        form_values.extend(
            str(historical_verified[key])
            for key in ("form_identity", "form_id", "application_form")
            if key in historical_verified and str(historical_verified[key] or "").strip()
        )
        historical_bound_form_identity = str(
            historical_verified.get("bound_form_identity") or ""
        ).strip()
        if historical_bound_form_identity:
            bound_form_identity_values.append(historical_bound_form_identity)
    bound_form_identity_values = [
        value for value in bound_form_identity_values if str(value).strip()
    ]
    _require_canonical_values(
        form_values,
        form_values[0] if form_values else "",
        label="form identity",
    )
    if bound_form_identity_values:
        _require_canonical_values(
            bound_form_identity_values,
            bound_form_identity_values[0],
            label="bound form identity",
        )
        if form_values:
            _require_canonical_values(
                [*form_values, *bound_form_identity_values],
                form_values[0],
                label="form identity",
            )
    root_token_values = list(_scalar_evidence_values(evidence, ROOT_TOKEN_EVIDENCE_KEYS))
    root_selector_values = list(_scalar_evidence_values(evidence, ROOT_SELECTOR_EVIDENCE_KEYS))
    if root_token_values:
        _require_canonical_values(root_token_values, root_token_values[0], label="root token")
    if root_selector_values:
        _require_canonical_values(root_selector_values, root_selector_values[0], label="root selector")

    frame_values = list(_scalar_evidence_values(evidence, FRAME_URL_EVIDENCE_KEYS))
    frame_contracts: list[str] = []
    for frame_value in frame_values:
        try:
            frame_contracts.append(canonical_target_contract_url(frame_value))
        except (TypeError, ValueError) as exc:
            raise PersistedTargetResolutionError(
                "Serialized frame URL evidence is malformed"
            ) from exc
    if frame_contracts and any(item != frame_contracts[0] for item in frame_contracts):
        raise PersistedTargetResolutionError(
            "Serialized frame URL aliases contradict one another"
        )
    expected_origin = origin_for_url(envelope_final)
    if frame_values:
        try:
            if any(origin_for_url(value) != expected_origin for value in frame_values):
                raise PersistedTargetResolutionError(
                    "Serialized frame URL evidence escapes the verified target origin"
                )
        except PersistedTargetResolutionError:
            raise
        except (TypeError, ValueError) as exc:
            raise PersistedTargetResolutionError(
                "Serialized frame URL evidence is malformed"
            ) from exc

    bound_target_values = list(
        _scalar_evidence_values(evidence, BOUND_TARGET_URL_EVIDENCE_KEYS)
    )
    bound_target_contracts: list[str] = []
    for bound_target_url in bound_target_values:
        try:
            bound_target_contracts.append(canonical_target_contract_url(bound_target_url))
        except (TypeError, ValueError) as exc:
            raise PersistedTargetResolutionError(
                "Serialized form binding target is malformed"
            ) from exc
    if bound_target_contracts and any(
        item != envelope_final_contract for item in bound_target_contracts
    ):
        raise PersistedTargetResolutionError(
            "Serialized form binding target contradicts the verified target"
        )

    form_contract = False
    form_contract_requisition_values: list[str] = []
    for node in _mapping_nodes(evidence):
        bound_target_values = _mapping_scalars(node, BOUND_TARGET_URL_EVIDENCE_KEYS)
        bound_provider_values = _mapping_scalars(node, frozenset({"boundprovider"}))
        bound_root_values = _mapping_scalars(node, ROOT_TOKEN_EVIDENCE_KEYS)
        bound_form_identity_values = _mapping_scalars(
            node, frozenset({"boundformidentity"})
        )
        raw_binding_values: list[bool] = []
        for raw_key, raw_value in node.items():
            if normalise_evidence_key(raw_key) != "bindingverified":
                continue
            if isinstance(raw_value, bool):
                raw_binding_values.append(raw_value)
            elif isinstance(raw_value, str) and raw_value.strip().casefold() == "true":
                # Accept the exact legacy scalar spelling only; integers,
                # mappings, and arbitrary truthy values are not proof.
                raw_binding_values.append(True)
            else:
                raw_binding_values.append(False)
        binding_verified = bool(raw_binding_values) and all(raw_binding_values)
        if bound_target_values:
            bound_target_contracts: list[str] = []
            for bound_target_url in bound_target_values:
                try:
                    bound_target_contracts.append(canonical_target_contract_url(bound_target_url))
                except (TypeError, ValueError) as exc:
                    raise PersistedTargetResolutionError(
                        "Serialized form binding target is malformed"
                    ) from exc
            if any(item != envelope_final_contract for item in bound_target_contracts):
                raise PersistedTargetResolutionError(
                    "Serialized form binding target contradicts the verified target"
                )
        if bound_provider_values:
            _require_canonical_values(
                bound_provider_values,
                authoritative_provider,
                label="form binding provider",
            )
        if bound_root_values:
            _require_canonical_values(
                bound_root_values,
                bound_root_values[0],
                label="form binding root",
            )
        if bound_form_identity_values:
            _require_canonical_values(
                bound_form_identity_values,
                bound_form_identity_values[0],
                label="form binding identity",
            )
        if (
            binding_verified
            and bound_target_values
            and bound_provider_values
            and bound_root_values
        ):
            form_contract = True
            form_contract_requisition_values.extend(
                _mapping_scalars(node, REQUISITION_EVIDENCE_KEYS)
            )
    if form_contract_requisition_values:
        _require_canonical_values(
            form_contract_requisition_values,
            form_contract_requisition_values[0],
            label="form binding requisition",
        )
        path_bound = path_bound or (
            _canonical_identity_text(form_contract_requisition_values[0]) in final_path
        )
    if not path_bound and not form_contract:
        raise PersistedTargetResolutionError(
            "Serialized requisition evidence is not bound to the verified target path or form"
        )
    form_contract_path = bool(form_values) and any(
        _canonical_identity_text(value) in final_path for value in form_values
    )
    if not form_contract_path and not form_contract:
        raise PersistedTargetResolutionError(
            "Serialized form identity is not bound to the verified target form"
        )

    origin_values = list(
        _scalar_evidence_values(evidence, ORIGIN_EVIDENCE_KEYS)
    )
    origin_values.extend(frame_values)
    origin_values.extend(
        _scalar_evidence_values(evidence, BOUND_TARGET_URL_EVIDENCE_KEYS)
    )
    origin_values.extend(
        str(stored[key])
        for key in ("application_origin", "origin")
        if key in stored and str(stored[key] or "").strip()
    )
    if not origin_values:
        raise PersistedTargetResolutionError("Serialized application origin evidence is missing")
    try:
        canonical_origins = [origin_for_url(value) for value in origin_values]
    except (TypeError, ValueError) as exc:
        raise PersistedTargetResolutionError("Serialized application origin evidence is malformed") from exc
    if any(value != expected_origin for value in canonical_origins):
        raise PersistedTargetResolutionError(
            "Serialized application origin evidence contradicts the verified target origin"
        )

    try:
        resolution = TargetResolution(
            source_url=envelope_source,
            final_url=envelope_final,
            kind=envelope_kind,
            provider=str(stored["provider"]),
            identity_verified=bool(stored["identity_verified"]),
            form_verified=bool(stored["form_verified"]),
            reason_codes=tuple(str(item) for item in stored.get("reason_codes", ())),
            evidence=dict(evidence),
        )
    except (TypeError, ValueError) as exc:
        raise PersistedTargetResolutionError(
            "Stored target-resolution evidence cannot be reconstructed"
        ) from exc
    if not resolution.verified_for_automation:
        raise PersistedTargetResolutionError(
            "Target resolution is not currently verified for automation",
            code="target_unresolved",
        )
    try:
        validate_target_resolution_contract(resolution)
    except ValueError as exc:
        raise PersistedTargetResolutionError(
            "Target resolution nested evidence is contradictory"
        ) from exc
    if not _resolution_is_complete(resolution):
        raise PersistedTargetResolutionError(
            "Target resolution is incomplete for automation"
        )
    return resolution


@dataclass(slots=True)
class _ManagedSession:
    session_id: str
    application_id: str
    mode: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    deadline_monotonic: float
    summary: dict[str, object]
    state: SessionState = SessionState.OPENING
    reason: str = ""
    manifest: dict[str, object] = field(default_factory=dict)
    # Exact owner-thread manifest shown to the user. Once sealed, queued
    # duplicate events may not erase an intervening mutation before confirm.
    sealed_manifest: dict[str, object] = field(default_factory=dict)
    command_queue: queue.Queue[SessionCommand] = field(default_factory=queue.Queue)
    event_queue: queue.Queue[SessionEvent] = field(default_factory=queue.Queue)
    worker: "HeadedSessionWorker | None" = None
    owner_thread_id: int | None = None
    cleanup_complete: bool = False
    cleanup_escalated: bool = False
    journey_result: dict[str, object] = field(default_factory=dict)
    # Last owner-thread final-preflight envelope.  It is kept separately from
    # the ordinary journey result because FINAL_REVIEW can remain live after a
    # refusal and callers must not mistake a stale snapshot for a fresh pass.
    preflight_result: dict[str, object] = field(default_factory=dict)
    headed: bool = False
    human_boundary: dict[str, object] = field(default_factory=dict)
    resume_until: datetime | None = None


class _SourceResolutionExecutor:
    """Owner-thread, read-only inspection of one stored source URL.

    This executor deliberately never invents a destination or upgrades page
    prose into identity proof.  It records only bounded, non-sensitive page
    classification evidence and leaves the immutable ``TargetResolution``
    unverified until the shared target-resolution service can cross-bind an
    exact provider/employer/role/form contract.  ``resume`` re-inspects the
    same page/context after the user has handled a challenge or navigated to a
    candidate application page.
    """

    def __init__(
        self,
        *,
        source_url: str,
        inspection_url: str = "",
        authoritative_requisition: str | None = None,
        requisition_source: str = "",
        requisition_provider: str = "",
        requisition_tenant: str = "",
        provider_hint: str,
        employer: str,
        role_title: str,
        allowlist: frozenset[str] = frozenset(),
        role_match_v2_enabled: bool = False,
        apply_click_enabled: bool = False,
        apply_click_budget: ApplyClickBudget | None = None,
        apply_click_timeout_ms: int = 10_000,
        source_capability: Any | None = None,
        opportunity_id: str = "",
        application_id: str = "",
    ) -> None:
        self.source_url = source_url
        self.inspection_url = inspection_url or source_url
        # Production-owned contexts pass an explicit (possibly empty)
        # requisition so public pages without stored identity always abstain.
        # Loopback fixtures are separately non-egress-capable and must retain
        # their explicit synthetic-lab proof path for end-to-end verification.
        self._requisition_binding_required = bool(
            authoritative_requisition is not None
            and not _loopback_host(urlsplit(self.inspection_url).hostname)
        )
        self.authoritative_requisition = str(
            authoritative_requisition or ""
        ).strip()[:300]
        self.requisition_source = str(requisition_source or "").strip()[:80]
        self.requisition_provider = str(requisition_provider or "").casefold().strip()[:80]
        self.requisition_tenant = str(requisition_tenant or "").casefold().strip()[:255]
        stored_identity = requisition_identity_from_url(self.inspection_url)
        self._expected_job_identity: RequisitionIdentity | None = None
        if stored_identity is not None and (
            not self.authoritative_requisition
            or requisitions_equal(
                stored_identity.requisition, self.authoritative_requisition
            )
        ):
            if (
                (not self.requisition_provider or self.requisition_provider == stored_identity.provider)
                and (not self.requisition_tenant or self.requisition_tenant == stored_identity.tenant)
            ):
                self._expected_job_identity = stored_identity
                self.authoritative_requisition = stored_identity.requisition
                self.requisition_provider = stored_identity.provider
                self.requisition_tenant = stored_identity.tenant
                self.requisition_source = (
                    self.requisition_source or stored_identity.provenance
                )
        raw_provider_hint = str(provider_hint or "").casefold().strip()
        # Transport and catch-all labels describe the source record, never a
        # final ATS.  The final URL/DOM must establish provider identity.
        self.provider_hint = (
            ""
            if raw_provider_hint.endswith("_shortlink")
            or raw_provider_hint in {"custom", "generic", "other", "unknown"}
            else raw_provider_hint
        )
        self.employer = employer
        self.role_title = role_title
        self.allowlist = frozenset(allowlist)
        self.role_match_v2_enabled = bool(role_match_v2_enabled)
        self.apply_click_enabled = bool(apply_click_enabled)
        self.apply_click_budget = apply_click_budget
        self.apply_click_timeout_ms = max(1, int(apply_click_timeout_ms))
        self.source_capability = source_capability
        self.opportunity_id = str(opportunity_id or "")[:128]
        self.application_id = str(application_id or "")[:128]
        self.resolution: TargetResolution | None = None
        self._discovery_attempted = False
        self._discovered_url = ""
        self._discovery_source_url = ""
        self._discovery_candidate_url = ""
        self._apply_hop_target_url = ""
        self._apply_redirect_chain: tuple[str, ...] = ()
        self._apply_hop_evidence: dict[str, object] = {}
        self._requisition_root_marker = ""
        self._requisition_root_selector = ""
        self._requisition_role_corroborated = False
        self._requisition_employer_corroborated = False
        self._role_match_v2_marker = ""
        self._role_match_v2_title = ""
        self._role_match_v2_title_source = ""
        self._role_match_v2_binding_mode = ""
        self._apply_origin_resolution: TargetResolution | None = None
        self._apply_click_attempted = False
        self._apply_click_active = False
        self._apply_click_fatal = False
        self._apply_click_evidence: dict[str, object] = {}
        self.redirect_chain: tuple[str, ...] = ()
        self._source_navigation_chain: tuple[str, ...] = ()
        self._document_responses: dict[str, dict[str, object]] = {}
        self._hydration_evidence: dict[str, object] = {}

    def record_document_response(self, response: Any) -> None:
        """Retain bounded objective metadata from one document response."""

        if response is None:
            return
        raw_request = getattr(response, "request", None)
        raw_request = raw_request() if callable(raw_request) else raw_request
        raw_resource_type = getattr(raw_request, "resource_type", "")
        raw_resource_type = (
            raw_resource_type()
            if callable(raw_resource_type)
            else raw_resource_type
        )
        if raw_resource_type and str(raw_resource_type).casefold() != "document":
            return
        raw_url = getattr(response, "url", "")
        raw_url = raw_url() if callable(raw_url) else raw_url
        url = _request_url_without_fragment(str(raw_url or ""))
        if not url:
            return
        raw_status = getattr(response, "status", None)
        raw_status = raw_status() if callable(raw_status) else raw_status
        try:
            status = int(raw_status)
        except (TypeError, ValueError):
            status = 0
        raw_headers = getattr(response, "headers", {})
        raw_headers = raw_headers() if callable(raw_headers) else raw_headers
        headers = raw_headers if isinstance(raw_headers, Mapping) else {}
        self._document_responses[url] = {
            "status": status,
            "content_type": str(headers.get("content-type") or "")[:200],
            "content_disposition": str(
                headers.get("content-disposition") or ""
            )[:300],
        }
        if len(self._document_responses) > 12:
            oldest = next(iter(self._document_responses))
            self._document_responses.pop(oldest, None)

    def _response_metadata(self, url: str) -> Mapping[str, object]:
        return self._document_responses.get(
            _request_url_without_fragment(url), {}
        )

    def _wait_for_provider_hydration(self, page: Any, final_url: str) -> None:
        """Wait once, boundedly, for structured ATS readiness signals."""

        key = _request_url_without_fragment(final_url)
        if key in self._hydration_evidence:
            return
        provider = trusted_provider_for_url(final_url)
        wait_for_function = getattr(page, "wait_for_function", None)
        if not provider or not callable(wait_for_function):
            self._hydration_evidence[key] = {
                "provider": provider,
                "status": "not_available",
            }
            return
        script = r"""
        () => Boolean(
          document.querySelector(
            '[data-requisition], [data-job-id], [data-posting-id], form, ' +
            '[data-automation-id="applicationForm"], ' +
            '[data-testid="application-form"], #application-form, ' +
            '[data-automation-id="applyButton"], a[href*="/apply"], ' +
            'button[aria-label*="apply" i], ' +
            // Workday tenants render the posting through their own
            // automation ids rather than a form or an /apply href.
            '[data-automation-id="jobPostingHeader"], ' +
            '[data-automation-id="jobPostingPage"], ' +
            '[data-automation-id="jobPostingDescription"], ' +
            '[data-automation-id="adventureButton"]'
          ) || window.__remixContext?.state?.loaderData
        )
        """
        timeout_ms = _PROVIDER_HYDRATION_TIMEOUT_MS.get(
            provider, _DEFAULT_HYDRATION_TIMEOUT_MS
        )
        try:
            wait_for_function(script, timeout=timeout_ms)
            status = "ready"
        except Exception:  # noqa: BLE001 - bounded timeout remains descriptive
            status = "timeout"
        self._hydration_evidence[key] = {
            "provider": provider,
            "status": status,
            "timeout_ms": timeout_ms,
        }

    def record_redirect_chain(self, chain: tuple[str, ...]) -> None:
        """Retain only a complete chain rooted at this exact stored source."""

        if len(chain) < 2 or len(chain) > 12:
            return
        normalised = tuple(_request_url_without_fragment(item) for item in chain)
        if not all(normalised):
            return
        source = _request_url_without_fragment(self.inspection_url)
        if source and normalised[0] == source:
            self.redirect_chain = normalised
            return
        apply_target = _request_url_without_fragment(self._apply_hop_target_url)
        if apply_target and normalised[0] == apply_target:
            self._apply_redirect_chain = normalised
            return
        if self._apply_click_active:
            try:
                allowed_origins = self._apply_click_allowed_origins()
                if allowed_origins and all(
                    origin_for_url(item) in allowed_origins for item in normalised
                ):
                    self._apply_redirect_chain = normalised
            except ValueError:
                return

    def record_source_navigation(self, url: str) -> None:
        """Record bounded main-frame document navigations from the source.

        Some opaque ATS links perform client-side navigations instead of HTTP
        redirects, so Playwright does not expose them through
        ``Request.redirected_from``.  The owner thread may still retain the
        ordered document sequence, but only while the source page is active,
        only for HTTPS hosts already in the session allowlist, and never after
        the Apply click begins.  This sequence is evidence for the one
        source-resolution capability; it does not mutate Settings.
        """

        if self._apply_click_active or self._apply_hop_target_url:
            return
        normalised = _request_url_without_fragment(url)
        source = _request_url_without_fragment(self.inspection_url)
        if not normalised or not source:
            return
        chain = self._source_navigation_chain
        if not chain:
            if normalised == source:
                self._source_navigation_chain = (source,)
            return
        if normalised == chain[-1] or len(chain) >= 12:
            return
        try:
            parsed = urlsplit(normalised)
            if (
                parsed.scheme.casefold() != "https"
                or not parsed.hostname
                or not _host_allowed(parsed.hostname, self.allowlist)
            ):
                return
        except ValueError:
            return
        self._source_navigation_chain = (*chain, normalised)

    def _apply_click_allowed_origins(self) -> frozenset[str]:
        """Return the capability origin plus one observed source redirect.

        A source capability remains exact for the stored URL.  The browser
        may, however, have reached the stored job page through a policy-
        approved redirect (for example an opaque ATS shortlink).  The final
        origin is usable for the one Apply click only when the owner thread
        recorded a complete chain rooted at that exact inspection URL.  No
        origin is inferred from page markup or added to Settings.
        """

        capability = self.source_capability
        if capability is None or not bool(getattr(capability, "active", False)):
            return frozenset()
        capability_origin = str(getattr(capability, "origin", "") or "").strip()
        if not capability_origin:
            return frozenset()
        allowed = {capability_origin}
        source = _request_url_without_fragment(self.inspection_url)
        for candidate in (self.redirect_chain, self._source_navigation_chain):
            chain = tuple(_request_url_without_fragment(item) for item in candidate)
            if len(chain) < 2 or not source or chain[0] != source:
                continue
            try:
                parsed = urlsplit(chain[-1])
                if (
                    parsed.scheme.casefold() == "https"
                    and parsed.hostname
                    and (
                        candidate is self.redirect_chain
                        or _host_allowed(parsed.hostname, self.allowlist)
                    )
                ):
                    allowed.add(origin_for_url(chain[-1]))
            except ValueError:
                continue
        return frozenset(allowed)

    @staticmethod
    def _placeholder_employer(value: object) -> bool:
        return " ".join(str(value or "").casefold().split()) in _PLACEHOLDER_EMPLOYERS

    @property
    def apply_hop_active(self) -> bool:
        return bool(self._apply_hop_target_url or self._apply_click_active)

    def authorizes_apply_document_request(
        self,
        request_url: str,
        redirect_chain: tuple[str, ...],
    ) -> bool:
        """Bind one document navigation to the exact page-authored apply URL."""

        if self._apply_click_active and not self._apply_hop_target_url:
            capability = self.source_capability
            if capability is None or not bool(getattr(capability, "active", False)):
                return False
            allowed_origins = self._apply_click_allowed_origins()
            try:
                if not allowed_origins or origin_for_url(request_url) not in allowed_origins:
                    return False
                return all(
                    origin_for_url(item) in allowed_origins
                    for item in redirect_chain
                )
            except ValueError:
                return False

        target = _request_url_without_fragment(self._apply_hop_target_url)
        request = _request_url_without_fragment(request_url)
        if not target or not request:
            return False
        try:
            target_origin = origin_for_url(target)
        except ValueError:
            return False
        if not redirect_chain:
            return request == target
        chain = tuple(_request_url_without_fragment(item) for item in redirect_chain)
        if not chain or chain[0] != target or chain[-1] != request:
            return False
        try:
            return all(origin_for_url(item) == target_origin for item in chain)
        except ValueError:
            return False

    def record_apply_egress_decision(
        self,
        *,
        reason: str,
        allowed: bool,
        fatal: bool,
    ) -> None:
        """Retain the exact owner-thread egress decision for this one hop."""

        if not self.apply_hop_active:
            return
        evidence = (
            self._apply_click_evidence
            if self._apply_click_active
            else self._apply_hop_evidence
        )
        evidence["egress_guard"] = {
            "reached": True,
            "allowed": bool(allowed),
            "fatal": bool(fatal),
            "reason": str(reason or "egress_decision_unlabelled")[:200],
        }
        if not allowed:
            evidence["outcome"] = (
                "apply_click_off_capability"
                if self._apply_click_active
                else "apply_egress_guard_refused"
            )

    @staticmethod
    def _page_url(page: Any, fallback: str) -> str:
        value = getattr(page, "url", "")
        value = value() if callable(value) else value
        return str(value or fallback)

    @staticmethod
    def _page_html(page: Any) -> str:
        content = getattr(page, "content", None)
        if not callable(content):
            return ""
        try:
            # Classification needs only bounded markup.  Never retain the
            # page HTML in a result or audit record because it may contain
            # candidate answers or authentication material.
            return str(content() or "")[:200_000]
        except Exception:  # noqa: BLE001 - inspection remains fail-closed
            return ""

    @staticmethod
    def _page_title(page: Any) -> str:
        title = getattr(page, "title", None)
        if not callable(title):
            return ""
        try:
            return str(title() or "")[:500]
        except Exception:  # noqa: BLE001 - title is descriptive evidence only
            return ""

    def _page_observation(self, page: Any) -> dict[str, object]:
        """Read bounded form proof inside one exact requisition root."""

        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            return {}
        final_url = self._page_url(page, self.inspection_url)
        document_identity_match = bool(
            self._expected_job_identity is not None
            and identity_matches_url(self._expected_job_identity, final_url)
        )
        marker = uuid.uuid4().hex
        script = r"""
        ({expectedRequisition, documentIdentityMatch, marker, strictBinding}) => {
          // Stable test seam: const root = first (legacy structured probe).
          const canonical = value => String(value || '').normalize('NFKC')
            .toLowerCase().replace(/\s+/g, ' ').trim();
          const visible = node => {
            if (!node) return false;
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              rect.width > 0 && rect.height > 0;
          };
          const first = (selectors, root = document) => {
            for (const selector of selectors) {
              const node = root.querySelector(selector);
              if (node && visible(node)) return node;
            }
            return null;
          };
          const identitySelector =
            '[data-requisition], [data-job-id], [data-posting-id], ' +
            '[data-requisition-id]';
          const rootFor = node => node.closest(
            '[data-job-listing], [data-source-listing], article, section, main'
          ) || node;
          const valuesFor = root => {
            const nodes = [root, ...Array.from(root.querySelectorAll(identitySelector))];
            const values = [];
            for (const node of nodes) {
              for (const name of [
                'data-requisition', 'data-job-id', 'data-posting-id',
                'data-requisition-id'
              ]) {
                const value = canonical(node.getAttribute?.(name) || '');
                if (value) values.push(value);
              }
            }
            return Array.from(new Set(values));
          };
          const explicitRoots = Array.from(new Set(
            Array.from(document.querySelectorAll(identitySelector))
              .filter(visible).map(rootFor).filter(visible)
          ));
          const exactRoots = expectedRequisition ? explicitRoots.filter(root => {
            const values = valuesFor(root);
            return values.length === 1 && values[0] === canonical(expectedRequisition);
          }) : [];
          let root = expectedRequisition
            ? (exactRoots.length === 1 ? exactRoots[0] : null)
            : first([
                '[data-ats]', '[data-employer]', '[data-role]',
                '[data-requisition]', '[data-job-id]', 'main'
              ]);
          if (expectedRequisition && !root && explicitRoots.length === 0 &&
              documentIdentityMatch && visible(document.body)) root = document.body;

          const containerSelector =
            'form, [data-automation-id="applicationForm"], ' +
            '[data-testid="application-form"], #application-form, .application-form';
          const containers = root
            ? (root.matches(containerSelector) ? [root] :
              Array.from(root.querySelectorAll(containerSelector)).filter(visible))
            : [];
          const form = containers.length === 1 ? containers[0] : null;
          if (form) form.setAttribute('data-argus-form-root', marker);
          const applicationEntry = first([
            '[data-automation-id="applyButton"]',
            '[data-automation-id="adventureButton"]',
            'a[href*="/apply"]', 'a[href*="/application"]',
            'button[aria-label*="apply" i]'
          ], root || document);

          const greenhouseHosts = new Set([
            'boards.greenhouse.io', 'job-boards.greenhouse.io',
            'job-boards.eu.greenhouse.io'
          ]);
          const greenhouseJob = (() => {
            if (!greenhouseHosts.has(location.hostname.toLowerCase())) return {};
            const loaderData = window.__remixContext?.state?.loaderData;
            if (!loaderData || typeof loaderData !== 'object') return {};
            for (const item of Object.values(loaderData).slice(0, 100)) {
              const job = item && typeof item === 'object' ? item.jobPost : null;
              if (!job || typeof job !== 'object') continue;
              const employer = String(job.company_name || '').trim();
              const role = String(job.title || '').trim();
              const requisition = String(item.jobPostId || '').trim();
              if (requisition) return {employer, role, requisition};
            }
            return {};
          })();
          const value = (node, names) => {
            if (!node) return '';
            for (const name of names) {
              const item = String(node.getAttribute(name) || '').trim();
              if (item) return item.slice(0, 300);
            }
            return '';
          };
          const data = (node, name) => node?.dataset
            ? String(node.dataset[name] || '').trim().slice(0, 300) : '';
          const observedRequisition = data(root, 'requisition') ||
            data(root, 'jobId') || value(root, [
              'data-requisition', 'data-job-id', 'data-posting-id',
              'data-requisition-id'
            ]) || greenhouseJob.requisition ||
            (documentIdentityMatch ? expectedRequisition : '');
          const submit = form && Array.from(
            form.querySelectorAll('button,input,[role="button"]')
          ).some(node => visible(node) &&
            ((node.getAttribute('type') || '').toLowerCase() === 'submit' ||
             /submit|apply/i.test(node.textContent || node.value || '')));
          const legacyFormIdentity = form
            ? (value(form, [
                'data-form-id', 'data-application-form-id', 'id'
              ]) || 'application-form')
            : (applicationEntry
              ? `application-entry:${value(applicationEntry, [
                  'data-automation-id', 'data-testid', 'id'
                  ]) || 'apply'}`
              : '');
          const landingText = canonical(document.body?.innerText ||
            document.body?.textContent || '');
          const confirmationVisible = /(?:submission confirmed|application received|application confirmation|confirmation number|thank you for applying)/.test(landingText) ||
            /\/(?:confirmation|confirmed|submitted)(?:[/?#]|$)/i.test(location.pathname);
          const submittedVisible = /application (?:has been )?submitted/.test(landingText) ||
            /submission confirmed/.test(landingText);
          return {
            provider: data(root, 'ats') || value(root, ['data-provider', 'data-ats']),
            employer: data(root, 'employer') ||
              value(root, ['data-company', 'data-employer']) ||
              greenhouseJob.employer || '',
            role: data(root, 'role') ||
              value(root, ['data-role', 'data-job-title']) ||
              greenhouseJob.role || '',
            requisition: observedRequisition,
            form_identity: strictBinding
              ? (form ? observedRequisition : '')
              : legacyFormIdentity,
            root_selector: form ? `[data-argus-form-root="${marker}"]` : '',
            form_action: form ? String(form.action || location.href).slice(0, 500) : '',
            form_method: form ? String(form.method || 'get').toUpperCase() : '',
            control_count: form ? Number(form.elements?.length ||
              form.querySelectorAll('input,select,textarea,button').length) : 0,
            submit_present: Boolean(submit),
            form_visible: Boolean(form),
            application_entry_visible: Boolean(applicationEntry),
            confirmation_visible: confirmationVisible,
            submitted_visible: submittedVisible,
            submission_confirmed: confirmationVisible || submittedVisible,
            explicit_identity_root_count: explicitRoots.length,
            exact_identity_root_count: exactRoots.length,
          };
        }
        """
        try:
            raw = evaluate(
                script,
                {
                    "expectedRequisition": (
                        self.authoritative_requisition
                        if self._requisition_binding_required
                        else ""
                    ),
                    "documentIdentityMatch": document_identity_match,
                    "marker": marker,
                    "strictBinding": self._requisition_binding_required,
                },
            )
        except Exception:  # noqa: BLE001 - opaque DOM is not proof
            return {}
        if not isinstance(raw, Mapping):
            return {}
        observation: dict[str, object] = {}
        for key in (
            "provider", "employer", "role", "requisition", "form_identity",
            "root_selector", "form_action", "form_method",
        ):
            value = raw.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                observation[key] = str(value).strip()[:500]
        for key in (
            "control_count", "submit_present", "form_visible",
            "application_entry_visible", "explicit_identity_root_count",
            "exact_identity_root_count", "confirmation_visible",
            "submitted_visible", "submission_confirmed",
        ):
            value = raw.get(key)
            if isinstance(value, (bool, int)):
                observation[key] = value
        return observation

    def _probe_role_match_v2(self, page: Any) -> RoleIdentityMatch:
        """Collect bounded page titles and run the local fallback matcher."""

        evaluate = getattr(page, "evaluate", None)
        raw: object = {}
        script = r"""
        ({employer}) => {
          // argus-role-match-v2-probe: read-only identity evidence collection.
          const visible = node => {
            if (!node) return false;
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              rect.width > 0 && rect.height > 0;
          };
          const identity = value => String(value || '').toLowerCase()
            .replace(/[^a-z0-9]+/g, ' ').trim();
          const containsIdentity = (container, expected) => Boolean(expected) &&
            ` ${container} `.includes(` ${expected} `);
          const employerText = identity(employer);
          const rootSelector =
            '[data-source-listing], [data-job-listing], main, article, section';
          const roots = Array.from(document.querySelectorAll(rootSelector))
            .filter(visible);
          const rootFor = heading =>
            heading.closest('[data-source-listing], [data-job-listing], article') ||
            heading.closest('section, main');
          const interactiveCount = root => Array.from(root.querySelectorAll(
            'a[href], button, [role="button"]'
          )).filter(visible).slice(0, 500).length;
          const titleCount = root => Array.from(root.querySelectorAll(
            'h1, h2, h3, [role="heading"], [data-job-title], [data-role-title]'
          )).filter(visible).slice(0, 500).filter(heading =>
            rootFor(heading) === root
          ).length;
          const safeRoot = root => {
            if (!root || !roots.includes(root)) return false;
            if (root.matches('[data-source-listing], [data-job-listing], article')) {
              return titleCount(root) === 1;
            }
            // Broad section/main roots are evidence-only and never actionable.
            return interactiveCount(root) === 0 && titleCount(root) === 1;
          };
          const headings = Array.from(document.querySelectorAll(
            'h1, h2, h3, [role="heading"], [data-job-title], [data-role-title]'
          )).filter(visible).slice(0, 500);
          const candidates = [];
          for (const heading of headings) {
            const text = String(heading.innerText || heading.textContent || '')
              .replace(/\s+/g, ' ').trim().slice(0, 500);
            if (!text) continue;
            const titleIdentity = identity(text);
            // Employer and role-title evidence must be in the same title node;
            // a broad page root cannot lend identity from a sibling card.
            if (!containsIdentity(titleIdentity, employerText)) continue;
            const chosen = rootFor(heading);
            if (!safeRoot(chosen)) continue;
            const tag = heading.tagName.toLowerCase();
            const source = ['h1', 'h2', 'h3'].includes(tag) ? tag :
              heading.hasAttribute('data-job-title') ? 'data_job_title' :
              heading.hasAttribute('data-role-title') ? 'data_role_title' :
              'aria_heading';
            candidates.push({
              root_marker: `candidate-${roots.indexOf(chosen)}`,
              root_text: text,
              source,
              text,
            });
            if (candidates.length >= 100) break;
          }
          return {candidates};
        }
        """
        if callable(evaluate):
            try:
                raw = evaluate(
                    script,
                    {
                        "employer": self.employer[:300],
                    },
                )
            except TypeError:
                try:
                    raw = evaluate(script)
                except Exception:  # noqa: BLE001 - matcher is fail-closed
                    raw = {}
            except Exception:  # noqa: BLE001 - matcher is fail-closed
                raw = {}
        observations: list[RoleTitleCandidate] = []
        items = raw.get("candidates") if isinstance(raw, Mapping) else None
        if isinstance(items, (list, tuple)):
            for item in items[:100]:
                if not isinstance(item, Mapping):
                    continue
                observations.append(
                    RoleTitleCandidate(
                        root_marker=str(item.get("root_marker") or "")[:128],
                        root_text=str(item.get("root_text") or "")[:500],
                        source=str(item.get("source") or "")[:80],
                        text=str(item.get("text") or "")[:500],
                    )
                )
        return match_role_identity_v2(
            expected_role=self.role_title,
            expected_employer=self.employer,
            candidates=tuple(observations),
        )

    def _bind_role_match_v2_root(
        self,
        page: Any,
        role_match: RoleIdentityMatch | None = None,
        *,
        strict_exact: bool = False,
    ) -> tuple[str, str, str]:
        """Atomically revalidate one identity title and mark only its root."""

        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate) or (
            not strict_exact and (role_match is None or not role_match.matched)
        ):
            return "", "", ""
        marker = uuid.uuid4().hex
        script = r"""
        ({role, employer, selectedTitle, titleSource, marker, strictExact}) => {
          const visible = node => {
            if (!node) return false;
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              rect.width > 0 && rect.height > 0;
          };
          const identity = value => String(value || '').toLowerCase()
            .replace(/[^a-z0-9]+/g, ' ').trim();
          const containsIdentity = (container, expected) => Boolean(expected) &&
            ` ${container} `.includes(` ${expected} `);
          const employerText = identity(employer);
          const roleText = identity(role);
          const selectedText = identity(selectedTitle);
          const exactRoleTitle = pageTitle => {
            if (!pageTitle || !roleText) return false;
            const variants = new Set([pageTitle]);
            const prefix = employerText ? `${employerText} ` : '';
            const suffix = employerText ? ` ${employerText}` : '';
            if (prefix && pageTitle.startsWith(prefix)) {
              variants.add(pageTitle.slice(prefix.length));
            }
            if (suffix && pageTitle.endsWith(suffix)) {
              variants.add(pageTitle.slice(0, -suffix.length));
            }
            if (prefix && suffix && pageTitle.startsWith(prefix) &&
                pageTitle.endsWith(suffix)) {
              variants.add(pageTitle.slice(prefix.length, -suffix.length));
            }
            return variants.has(roleText);
          };
          const rootSelector =
            '[data-source-listing], [data-job-listing], main, article, section';
          const roots = Array.from(document.querySelectorAll(rootSelector))
            .filter(visible);
          const rootFor = heading =>
            heading.closest('[data-source-listing], [data-job-listing], article') ||
            heading.closest('section, main');
          const interactiveCount = root => Array.from(root.querySelectorAll(
            'a[href], button, [role="button"]'
          )).filter(visible).slice(0, 500).length;
          const titleCount = root => Array.from(root.querySelectorAll(
            'h1, h2, h3, [role="heading"], [data-job-title], [data-role-title]'
          )).filter(visible).slice(0, 500).filter(heading =>
            rootFor(heading) === root
          ).length;
          const safeRoot = root => {
            if (!root || !roots.includes(root)) return false;
            if (root.matches('[data-source-listing], [data-job-listing], article')) {
              return titleCount(root) === 1;
            }
            return interactiveCount(root) === 0 && titleCount(root) === 1;
          };
          let matches = [];
          matches = Array.from(document.querySelectorAll(
            'h1, h2, h3, [role="heading"], [data-job-title], [data-role-title]'
          )).filter(visible).slice(0, 500).filter(heading => {
            const text = identity(heading.innerText || heading.textContent || '');
            const root = rootFor(heading);
            const rootText = identity(root?.innerText || root?.textContent || '');
            const identityMatches = strictExact
              ? exactRoleTitle(text) &&
                containsIdentity(rootText, employerText)
              : text === selectedText && containsIdentity(text, employerText);
            return identityMatches && safeRoot(root);
          });
          const matchedHeadings = matches;
          matches = matchedHeadings.map(rootFor);
          const uniqueRoots = Array.from(new Set(matches));
          if (uniqueRoots.length !== 1 || matchedHeadings.length !== 1) {
            return {bound: false, marker: '', title: '', title_source: ''};
          }
          for (const node of document.querySelectorAll(
            '[data-argus-role-match-v2-root]'
          )) {
            node.removeAttribute('data-argus-role-match-v2-root');
          }
          const root = uniqueRoots[0];
          root.setAttribute('data-argus-role-match-v2-root', marker);
          if (root.__argusRoleMatchV2Observer) {
            root.__argusRoleMatchV2Observer.disconnect();
          }
          const observer = new MutationObserver(() => {
            root.removeAttribute('data-argus-role-match-v2-root');
            for (const control of root.querySelectorAll(
              '[data-argus-resolution-affordance]'
            )) control.removeAttribute('data-argus-resolution-affordance');
            observer.disconnect();
          });
          observer.observe(root, {
            subtree: true,
            childList: true,
            characterData: true,
            attributes: true,
            attributeFilter: [
              'class', 'style', 'hidden', 'aria-hidden', 'aria-label', 'title',
              'role', 'data-job-title', 'data-role-title'
            ],
          });
          root.__argusRoleMatchV2Observer = observer;
          const heading = matchedHeadings[0];
          const source = heading.hasAttribute('data-job-title') ? 'data-job-title' :
            heading.hasAttribute('data-role-title') ? 'data-role-title' :
            heading.getAttribute('role') === 'heading' ? 'role-heading' :
            heading.tagName.toLowerCase();
          return {
            bound: true,
            marker,
            title: String(heading.innerText || heading.textContent || '').trim(),
            title_source: source,
          };
        }
        """
        selected_title = (
            ""
            if strict_exact or role_match is None
            else role_match.compared_page_title[:500]
        )
        selected_source = (
            "" if strict_exact or role_match is None else role_match.title_source[:80]
        )
        try:
            raw = evaluate(
                script,
                {
                    "role": self.role_title[:300],
                    "employer": self.employer[:300],
                    "selectedTitle": selected_title,
                    "titleSource": selected_source,
                    "marker": marker,
                    "strictExact": strict_exact,
                },
            )
        except TypeError:
            try:
                raw = evaluate(script)
            except Exception:  # noqa: BLE001 - identity binding is fail-closed
                raw = None
        except Exception:  # noqa: BLE001 - identity binding is fail-closed
            raw = None
        if not isinstance(raw, Mapping) or not bool(raw.get("bound")):
            return "", "", ""
        returned_marker = str(raw.get("marker") or "")[:128]
        if returned_marker != marker:
            return "", "", ""
        returned_title = str(raw.get("title") or selected_title).strip()[:500]
        returned_source = str(raw.get("title_source") or selected_source).strip()[:80]
        if not returned_title:
            return "", "", ""
        return marker, returned_title, returned_source

    def _bind_requisition_root(self, page: Any) -> bool:
        """Mark exactly one visible root carrying the stored requisition."""

        if not self._requisition_binding_required:
            return True
        self._requisition_root_marker = ""
        self._requisition_root_selector = ""
        self._requisition_role_corroborated = False
        self._requisition_employer_corroborated = False
        expected = self.authoritative_requisition
        if not expected or self._expected_job_identity is None:
            return False
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            return False
        current = self._page_url(page, self.inspection_url)
        document_match = False
        if current.startswith(("http://", "https://")):
            document_match = identity_matches_url(self._expected_job_identity, current)
        elif current.startswith(("about:", "data:")):
            # Browser unit fixtures use set_content/about:blank.  Production
            # public navigation never enters this fallback.
            document_match = identity_matches_url(
                self._expected_job_identity, self.inspection_url
            )
        marker = uuid.uuid4().hex
        script = r"""
        ({expected, marker, documentMatch, employer, role}) => {
          const canonical = value => String(value || '').normalize('NFKC')
            .toLowerCase().replace(/\s+/g, ' ').trim();
          const expectedKey = canonical(expected);
          const visible = node => {
            if (!node) return false;
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              rect.width > 0 && rect.height > 0 &&
              node.getAttribute('aria-hidden') !== 'true';
          };
          const identitySelector =
            '[data-requisition], [data-job-id], [data-posting-id], [data-requisition-id]';
          const rootFor = node => node.closest(
            '[data-job-listing], [data-source-listing], article, form, section'
          ) || node;
          const valuesFor = root => {
            const nodes = [root, ...Array.from(root.querySelectorAll(identitySelector))];
            const values = [];
            for (const node of nodes) {
              for (const name of [
                'data-requisition', 'data-job-id', 'data-posting-id',
                'data-requisition-id'
              ]) {
                const value = canonical(node.getAttribute?.(name) || '');
                if (value) values.push(value);
              }
            }
            return Array.from(new Set(values));
          };
          for (const node of document.querySelectorAll(
            '[data-argus-requisition-root]'
          )) node.removeAttribute('data-argus-requisition-root');
          const explicitRoots = Array.from(new Set(
            Array.from(document.querySelectorAll(identitySelector))
              .filter(visible).map(rootFor).filter(visible)
          ));
          const matchingRoots = explicitRoots.filter(root => {
            const values = valuesFor(root);
            return values.length === 1 && values[0] === expectedKey;
          });
          let root = matchingRoots.length === 1 ? matchingRoots[0] : null;
          if (!root && explicitRoots.length === 0 && documentMatch && visible(document.body)) {
            root = document.body;
          }
          if (!root) {
            return {
              bound: false,
              matching_root_count: matchingRoots.length,
              explicit_root_count: explicitRoots.length,
            };
          }
          root.setAttribute('data-argus-requisition-root', marker);
          const rootText = canonical(root.innerText || root.textContent || '');
          const employerText = canonical(employer);
          const roleText = canonical(role);
          return {
            bound: true,
            marker,
            matching_root_count: matchingRoots.length || 1,
            explicit_root_count: explicitRoots.length,
            employer_corroborated: Boolean(employerText) && rootText.includes(employerText),
            role_corroborated: Boolean(roleText) && rootText.includes(roleText),
          };
        }
        """
        try:
            raw = evaluate(
                script,
                {
                    "expected": expected,
                    "marker": marker,
                    "documentMatch": document_match,
                    "employer": self.employer[:300],
                    "role": self.role_title[:300],
                },
            )
        except Exception:  # noqa: BLE001 - requisition binding is fail-closed
            return False
        if (
            not isinstance(raw, Mapping)
            or not bool(raw.get("bound"))
            or str(raw.get("marker") or "") != marker
        ):
            return False
        self._requisition_root_marker = marker
        self._requisition_root_selector = (
            f'[data-argus-requisition-root="{marker}"]'
        )
        self._requisition_employer_corroborated = bool(
            raw.get("employer_corroborated")
        )
        self._requisition_role_corroborated = bool(raw.get("role_corroborated"))
        return True

    def _discover_application_link(self, page: Any) -> str:
        """Return one bounded, visible application link from the current page.

        Discovery runs once, stays inside the visible job-detail root, accepts
        only an exact accessible-name allowlist or a known provider path, and
        returns a URL for GET navigation. It never clicks a control.
        """

        if self._discovery_attempted:
            return ""
        self._discovery_attempted = True
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            return ""
        if self._requisition_binding_required and not self._bind_requisition_root(page):
            source = self._page_url(page, self.inspection_url)
            self._apply_hop_evidence = {
                "attempted": True,
                "source_url": source,
                "hop_chain": [source],
                "candidate_count": 0,
                "control_count": 0,
                "candidate_urls": [],
                "visible_root_count": 0,
                "bound_job_root_found": False,
                "page_apply_affordance_count": 0,
                "bound_apply_affordance_count": 0,
                "eligible_destination_count": 0,
                "ambiguity_guard_triggered": False,
                "navigation_started": False,
                "origin_binding": "not_reached",
                "egress_guard": "not_reached",
                "destination_verification": "not_reached",
                "expected_requisition": self.authoritative_requisition,
                "requisition_source": self.requisition_source,
                "requisition_match": False,
                "role_corroborated": False,
                "employer_corroborated": False,
                "outcome": "apply_requisition_root_not_bound",
            }
            return ""
        script = """
        ({role, employer, role_match_v2_marker: roleMatchV2Marker,
          role_match_v2_title: roleMatchV2Title,
          role_match_v2_title_source: roleMatchV2TitleSource,
          role_match_v2_binding_mode: roleMatchV2BindingMode,
          role_match_v2_enabled: roleMatchV2Enabled,
          requisition_marker: requisitionMarker,
          requisition_binding_required: requisitionBindingRequired,
           expected_requisition: expectedRequisition}) => {
           const visible = node => {
             if (!node) return false;
             const style = getComputedStyle(node);
             const rect = node.getBoundingClientRect();
             return style.display !== 'none' && style.visibility !== 'hidden' &&
               rect.width > 0 && rect.height > 0;
           };
           const nameKey = value => String(value || '').normalize('NFKC')
             .toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
           const accessibleName = node => {
             const labelledBy = String(node.getAttribute('aria-labelledby') || '')
             .split(/\\s+/).filter(Boolean).map(id => document.getElementById(id))
               .filter(Boolean).map(item => item.innerText || item.textContent || '')
               .join(' ');
             return String(
               node.getAttribute('aria-label') || labelledBy ||
               node.getAttribute('title') || node.innerText || node.textContent ||
               node.getAttribute('value') || ''
              ).replace(/\\s+/g, ' ').trim().slice(0, 200);
           };
           const allowedNames = new Set([
             'apply', 'apply now', 'apply for this job',
             'apply for this position', 'apply here', 'apply on employer site',
             'apply online', 'begin application', 'start application',
             'apply with linkedin', 'apply with indeed', 'apply with google',
             'apply with dropbox'
           ]);
           const explicitApplyName = value => allowedNames.has(nameKey(value)) &&
             !/\\b(?:submit|send|confirm|delete|withdraw|sign out|payment|pay|checkout|finish|continue|next)\\b/i
               .test(nameKey(value));
           const socialHost = host => {
            const value = String(host || '').toLowerCase().replace(/^www\\./, '');
             return ['linkedin.com', 'indeed.com', 'google.com', 'dropbox.com']
               .some(item => value === item || value.endsWith(`.${item}`));
           };
           const identity = value => String(value || '').toLowerCase()
            .replace(/[^a-z0-9]+/g, ' ').trim();
           const containsIdentity = (container, expected) => Boolean(expected) &&
             ` ${container} `.includes(` ${expected} `);
           const rootIdentity = root => identity(
             `${root?.innerText || root?.textContent || ''} ` +
             `${root?.getAttribute?.('data-employer') || ''} ` +
             `${root?.getAttribute?.('data-company') || ''} ` +
             `${root?.getAttribute?.('data-company-name') || ''} ` +
             `${root?.getAttribute?.('data-role') || ''} ` +
             `${root?.getAttribute?.('data-role-title') || ''} ` +
             `${root?.getAttribute?.('data-job-title') || ''}`
           );
           const roleText = identity(role);
          const employerText = identity(employer);
          const selectedTitleText = identity(roleMatchV2Title);
          const exactRoleTitle = pageTitle => {
            if (!pageTitle || !roleText) return false;
            const variants = new Set([pageTitle]);
            const prefix = employerText ? `${employerText} ` : '';
            const suffix = employerText ? ` ${employerText}` : '';
            if (prefix && pageTitle.startsWith(prefix)) {
              variants.add(pageTitle.slice(prefix.length));
            }
            if (suffix && pageTitle.endsWith(suffix)) {
              variants.add(pageTitle.slice(0, -suffix.length));
            }
            if (prefix && suffix && pageTitle.startsWith(prefix) &&
                pageTitle.endsWith(suffix)) {
              variants.add(pageTitle.slice(prefix.length, -suffix.length));
            }
            return variants.has(roleText);
          };
          const rootSelector =
            '[data-argus-requisition-root], [data-source-listing], ' +
            '[data-job-listing], [data-job-detail], .jobDisplay, ' +
            '.job-display, main, article, section';
          const roots = Array.from(document.querySelectorAll(rootSelector))
            .filter(visible);
          const rootFor = heading =>
            heading.closest(
              '[data-source-listing], [data-job-listing], [data-job-detail], ' +
              '.jobDisplay, .job-display, article'
            ) || heading.closest('section, main');
          const interactiveCount = candidateRoot => Array.from(
            candidateRoot.querySelectorAll('a[href], button, [role="button"]')
          ).filter(visible).slice(0, 500).length;
          const titleNodes = candidateRoot => Array.from(
            candidateRoot.querySelectorAll(
              'h1, h2, h3, [role="heading"], [data-job-title], [data-role-title]'
            )
          ).filter(visible).slice(0, 500).filter(heading =>
            rootFor(heading) === candidateRoot
          );
          const safeRoot = candidateRoot => {
            if (!candidateRoot || !roots.includes(candidateRoot)) return false;
            if (candidateRoot.matches(
                '[data-source-listing], [data-job-listing], [data-job-detail], ' +
                '.jobDisplay, .job-display, article')) {
              return titleNodes(candidateRoot).length === 1;
            }
            return interactiveCount(candidateRoot) === 0 &&
              titleNodes(candidateRoot).length === 1;
          };
          const markedRootValid = candidateRoot => {
            if (!candidateRoot || !selectedTitleText) return false;
            if (!safeRoot(candidateRoot)) return false;
            const matchingHeadings = titleNodes(candidateRoot).filter(heading =>
              identity(heading.innerText || heading.textContent || '') ===
                selectedTitleText
            );
            if (matchingHeadings.length !== 1) return false;
            if (roleMatchV2BindingMode === 'strict_exact') {
              const rootText = rootIdentity(candidateRoot);
              return exactRoleTitle(selectedTitleText) &&
                containsIdentity(rootText, employerText);
            }
            return roleMatchV2BindingMode === 'fallback' &&
              containsIdentity(selectedTitleText, employerText);
          };
          const markedRoots = roleMatchV2Marker ? roots.filter(node =>
            node.getAttribute('data-argus-role-match-v2-root') === roleMatchV2Marker
          ) : [];
          const markedRoot = markedRoots.length === 1 &&
            markedRootValid(markedRoots[0]) ? markedRoots[0] : null;
          const legacyRoot = roots.find(node => {
            if (node.matches(
                '[data-source-listing], [data-job-listing], [data-job-detail], ' +
                '.jobDisplay, .job-display')) return true;
            const text = rootIdentity(node);
            return (!roleText || text.includes(roleText)) &&
              (!employerText || text.includes(employerText));
          }) || null;
          const strictRoots = roleMatchV2Enabled || roleMatchV2Marker
            ? Array.from(new Set(Array.from(document.querySelectorAll(
                'h1, h2, h3, [role="heading"], [data-job-title], [data-role-title]'
              )).filter(visible).slice(0, 500).filter(heading => {
                const headingText = identity(
                  heading.innerText || heading.textContent || ''
                );
                const candidateRoot = rootFor(heading);
                const rootText = rootIdentity(candidateRoot);
                return exactRoleTitle(headingText) &&
                  containsIdentity(rootText, employerText) && safeRoot(candidateRoot);
              }).map(rootFor)))
            : [];
          const strictRoot = strictRoots.length === 1 ? strictRoots[0] : null;
          const requisitionRoots = requisitionMarker ? roots.filter(node =>
            node.getAttribute('data-argus-requisition-root') === requisitionMarker
          ) : [];
          const requisitionValues = candidateRoot => Array.from(new Set(
            [candidateRoot, ...Array.from(candidateRoot?.querySelectorAll(
              '[data-requisition], [data-job-id], [data-posting-id], [data-requisition-id]'
            ) || [])].flatMap(node => [
              node?.getAttribute?.('data-requisition'),
              node?.getAttribute?.('data-job-id'),
              node?.getAttribute?.('data-posting-id'),
              node?.getAttribute?.('data-requisition-id'),
            ]).filter(Boolean).map(value => String(value).normalize('NFKC')
              .toLowerCase().replace(/\\s+/g, ' ').trim())
          ));
          const requisitionRoot = requisitionRoots.length === 1 && (
            requisitionValues(requisitionRoots[0]).length === 0 ||
            (requisitionValues(requisitionRoots[0]).length === 1 &&
             requisitionValues(requisitionRoots[0])[0] ===
               String(expectedRequisition || '').normalize('NFKC')
                  .toLowerCase().replace(/\\s+/g, ' ').trim())
          ) ? requisitionRoots[0] : null;
          const root = requisitionBindingRequired
            ? requisitionRoot
            : roleMatchV2Marker ? markedRoot
            : roleMatchV2Enabled ? strictRoot : legacyRoot;
          if (!root) {
            const pageApplyAffordanceCount = Array.from(document.querySelectorAll(
              'a[href], button, [role="button"]'
            )).filter(visible).slice(0, 500).filter(control => {
              const label = String(
                control.getAttribute('aria-label') || control.textContent ||
                control.getAttribute('title') || control.getAttribute('value') || ''
              ).replace(/\\s+/g, ' ').trim().toLowerCase().slice(0, 100);
              return explicitApplyName(label);
            }).length;
            return {
              href: '', count: 0, control_count: 0,
              visible_root_count: roots.length, bound_job_root_found: false,
              page_apply_affordance_count: pageApplyAffordanceCount,
              bound_apply_affordance_count: 0, eligible_destination_count: 0,
              ambiguity_guard_triggered: false, candidate_urls: []
            };
          }
          const knownProviderPath = resolved => {
            const host = resolved.hostname.toLowerCase();
            const path = resolved.pathname.toLowerCase();
            if (['boards.greenhouse.io', 'job-boards.greenhouse.io',
                 'job-boards.eu.greenhouse.io'].includes(host)) {
              return /\\/jobs\\/\\d+(?:\\/|$)/.test(path);
            }
            if (host === 'jobs.lever.co') {
              return path.split('/').filter(Boolean).length >= 2;
            }
            if (/^[a-z0-9][a-z0-9-]*\\.wd[0-9]+\\.(?:myworkdayjobs|myworkdaysite)\\.com$/.test(host)) {
              return /\\/(?:apply|application)(?:\\/|$)/.test(path);
            }
            if (host === 'jobs.smartrecruiters.com' || host === 'apply.workable.com') {
              return /\\/(?:apply|application)(?:\\/|$)/.test(path);
            }
            return false;
          };
          const pageApplyAffordanceCount = Array.from(document.querySelectorAll(
            'a[href], button, [role="button"]'
          )).filter(visible).slice(0, 500).filter(control => {
            const label = String(
              control.getAttribute('aria-label') || control.textContent ||
              control.getAttribute('title') || control.getAttribute('value') || ''
            ).replace(/\\s+/g, ' ').trim().toLowerCase().slice(0, 100);
            return explicitApplyName(label);
          }).length;
          const boundApplyAffordanceCount = Array.from(root.querySelectorAll(
            'a[href], button, [role="button"]'
          )).filter(visible).slice(0, 500).filter(control => {
            const label = String(
              control.getAttribute('aria-label') || control.textContent ||
              control.getAttribute('title') || control.getAttribute('value') || ''
            ).replace(/\\s+/g, ' ').trim().toLowerCase().slice(0, 100);
            return explicitApplyName(label);
          }).length;
          const candidates = [];
          const controls = Array.from(root.querySelectorAll(
            'a[href], button, [role="button"]'
          )).slice(0, 100);
          for (const control of controls) {
            if (!visible(control)) continue;
            const anchor = control.matches('a[href]') ? control : control.closest('a[href]');
            const raw = String(
              anchor?.getAttribute('href') || control.getAttribute('data-href') ||
              control.getAttribute('data-url') || ''
            ).trim();
            if (!raw) continue;
            let resolved;
            try { resolved = new URL(raw, location.href); } catch (_error) { continue; }
            if (!['http:', 'https:'].includes(resolved.protocol) ||
                resolved.username || resolved.password) continue;
            resolved.hash = '';
            const label = accessibleName(control);
            if (!explicitApplyName(label) && !knownProviderPath(resolved)) continue;
            const href = resolved.toString();
            if (href === location.href) continue;
            const firstParty = resolved.origin === location.origin;
            const thirdParty = socialHost(resolved.hostname);
            candidates.push({
              href,
              label,
              role: String(control.getAttribute('role') ||
                (control.tagName.toLowerCase() === 'button' ? 'button' : 'link'))
                .toLowerCase(),
              first_party: firstParty,
              third_party: thirdParty,
              inside_job_root: true,
              score: [
                firstParty ? 1 : 0,
                1,
                thirdParty ? 0 : 1,
                control.tagName.toLowerCase() === 'button' ? 1 : 0,
              ],
            });
            if (candidates.length > 20) return {
              href: '', count: candidates.length, candidate_count: candidates.length,
              control_count: candidates.length,
              visible_root_count: roots.length, bound_job_root_found: true,
              page_apply_affordance_count: pageApplyAffordanceCount,
              bound_apply_affordance_count: boundApplyAffordanceCount,
              eligible_destination_count: candidates.length,
              ambiguity_guard_triggered: true, candidate_urls: []
            };
          }
          const byHref = new Map();
          for (const candidate of candidates) {
            const existing = byHref.get(candidate.href);
            if (!existing || candidate.score.join('.') > existing.score.join('.')) {
              byHref.set(candidate.href, candidate);
            }
          }
          const uniqueCandidates = Array.from(byHref.values());
          const ranked = uniqueCandidates.slice().sort((left, right) => {
            for (let index = 0; index < left.score.length; index += 1) {
              if (left.score[index] !== right.score[index]) {
                return right.score[index] - left.score[index];
              }
            }
            return left.href.localeCompare(right.href);
          });
          const best = ranked.length ? ranked[0] : null;
          const bestScore = best ? best.score.join('.') : '';
          const ties = best ? ranked.filter(item => item.score.join('.') === bestScore) : [];
          const candidateUrls = uniqueCandidates.map(item => item.href).slice(0, 20);
          return best && ties.length === 1
            ? {href: best.href, count: 1, candidate_count: uniqueCandidates.length,
               control_count: candidates.length, candidate_urls: candidateUrls,
               selected_candidate: best, visible_root_count: roots.length,
               bound_job_root_found: true,
               page_apply_affordance_count: pageApplyAffordanceCount,
               bound_apply_affordance_count: boundApplyAffordanceCount,
               eligible_destination_count: uniqueCandidates.length,
               ambiguity_guard_triggered: false}
            : {href: '', count: uniqueCandidates.length,
               candidate_count: uniqueCandidates.length,
               control_count: candidates.length,
               candidate_urls: candidateUrls, visible_root_count: roots.length,
               bound_job_root_found: true,
               page_apply_affordance_count: pageApplyAffordanceCount,
               bound_apply_affordance_count: boundApplyAffordanceCount,
               eligible_destination_count: uniqueCandidates.length,
               ambiguity_guard_triggered: uniqueCandidates.length > 1};
        }
        """
        args = {
            "role": self.role_title[:300],
            "employer": (
                "" if self._placeholder_employer(self.employer) else self.employer[:300]
            ),
            "role_match_v2_binding_mode": "",
            "role_match_v2_enabled": self.role_match_v2_enabled,
            "requisition_marker": self._requisition_root_marker,
            "requisition_binding_required": self._requisition_binding_required,
            "expected_requisition": self.authoritative_requisition,
        }
        try:
            raw = evaluate(script, args)
        except TypeError:
            # Small test doubles and older Playwright shims may expose only
            # evaluate(script).  The production path always supplies args.
            try:
                raw = evaluate(script)
            except Exception:  # noqa: BLE001 - discovery is fail-closed
                return ""
        except Exception:  # noqa: BLE001 - discovery is fail-closed
            return ""
        role_identity_evidence: dict[str, object] | None = None
        initially_bound = bool(
            isinstance(raw, Mapping)
            and raw.get(
                "bound_job_root_found",
                bool(raw.get("count") or raw.get("href")),
            )
        )
        strict_binding_failed = False
        if self.role_match_v2_enabled and initially_bound and isinstance(raw, Mapping):
            bound_marker, bound_title, bound_source = self._bind_role_match_v2_root(
                page,
                strict_exact=True,
            )
            strict_args = dict(args)
            strict_args["role_match_v2_marker"] = bound_marker
            strict_args["role_match_v2_title"] = bound_title
            strict_args["role_match_v2_title_source"] = bound_source
            strict_args["role_match_v2_binding_mode"] = "strict_exact"
            try:
                strict_raw = (
                    evaluate(script, strict_args) if bound_marker and bound_title else None
                )
            except TypeError:
                try:
                    strict_raw = evaluate(script)
                except Exception:  # noqa: BLE001 - strict binding is fail-closed
                    strict_raw = None
            except Exception:  # noqa: BLE001 - strict binding is fail-closed
                strict_raw = None
            if isinstance(strict_raw, Mapping) and bool(
                strict_raw.get(
                    "bound_job_root_found",
                    bool(strict_raw.get("count") or strict_raw.get("href")),
                )
            ):
                raw = strict_raw
                self._role_match_v2_marker = bound_marker
                self._role_match_v2_title = bound_title
                self._role_match_v2_title_source = bound_source
                self._role_match_v2_binding_mode = "strict_exact"
            else:
                strict_binding_failed = True
                raw = {
                    **dict(raw),
                    "href": "",
                    "count": 0,
                    "control_count": 0,
                    "candidate_urls": [],
                    "bound_job_root_found": False,
                    "bound_apply_affordance_count": 0,
                    "eligible_destination_count": 0,
                    "ambiguity_guard_triggered": False,
                }
        if (
            self.role_match_v2_enabled
            and isinstance(raw, Mapping)
            and not initially_bound
            and not strict_binding_failed
            and not self._placeholder_employer(self.employer)
        ):
            role_match = self._probe_role_match_v2(page)
            role_identity_evidence = role_match.audit_evidence()
            if role_match.matched and role_match.selected_root_marker:
                bound_marker, bound_title, bound_source = (
                    self._bind_role_match_v2_root(page, role_match)
                )
                fallback_args = dict(args)
                fallback_args["role_match_v2_marker"] = bound_marker
                fallback_args["role_match_v2_title"] = bound_title
                fallback_args["role_match_v2_title_source"] = bound_source
                fallback_args["role_match_v2_binding_mode"] = "fallback"
                try:
                    fallback_raw = (
                        evaluate(script, fallback_args) if bound_marker else None
                    )
                except TypeError:
                    try:
                        fallback_raw = evaluate(script)
                    except Exception:  # noqa: BLE001 - fallback is fail-closed
                        fallback_raw = None
                except Exception:  # noqa: BLE001 - fallback is fail-closed
                    fallback_raw = None
                if isinstance(fallback_raw, Mapping) and bool(
                    fallback_raw.get(
                        "bound_job_root_found",
                        bool(fallback_raw.get("count") or fallback_raw.get("href")),
                    )
                ):
                    raw = fallback_raw
                    self._role_match_v2_marker = bound_marker
                    self._role_match_v2_title = bound_title
                    self._role_match_v2_title_source = bound_source
                    self._role_match_v2_binding_mode = "fallback"
        source = self._page_url(page, self.inspection_url)
        if not isinstance(raw, Mapping):
            self._apply_hop_evidence = {
                "attempted": True,
                "source_url": source,
                "hop_chain": [source],
                "candidate_count": 0,
                "eligible_destination_count": 0,
                "bound_job_root_found": False,
                "ambiguity_guard_triggered": False,
                "navigation_started": False,
                "origin_binding": "not_reached",
                "egress_guard": "not_reached",
                "destination_verification": "not_reached",
                "outcome": "apply_control_inspection_failed",
            }
            return ""
        count = int(raw.get("count", 0) or 0)
        try:
            candidate_count = int(raw.get("candidate_count", count) or 0)
        except (TypeError, ValueError):
            candidate_count = max(0, count)
        href = str(raw.get("href") or "").strip()[:1000]
        bound_job_root_found = bool(
            raw.get("bound_job_root_found", bool(count or href))
        )
        raw_candidates = raw.get("candidate_urls")
        candidate_urls = (
            [str(item).strip()[:1000] for item in raw_candidates[:20] if str(item).strip()]
            if isinstance(raw_candidates, (list, tuple))
            else ([href] if href else [])
        )
        self._apply_hop_evidence = {
            "attempted": True,
            "source_url": source,
            "hop_chain": [source],
            "candidate_count": max(0, candidate_count),
            "control_count": int(raw.get("control_count", count) or 0),
            "candidate_urls": candidate_urls,
            "visible_root_count": int(
                raw.get("visible_root_count", 1 if bound_job_root_found else 0) or 0
            ),
            "bound_job_root_found": bound_job_root_found,
            "page_apply_affordance_count": int(
                raw.get("page_apply_affordance_count", count) or 0
            ),
            "bound_apply_affordance_count": int(
                raw.get("bound_apply_affordance_count", count) or 0
            ),
            "eligible_destination_count": int(
                raw.get("eligible_destination_count", count) or 0
            ),
            "ambiguity_guard_triggered": bool(
                raw.get("ambiguity_guard_triggered", count > 1)
            ),
            "navigation_started": False,
            "origin_binding": "not_reached",
            "egress_guard": "not_reached",
            "destination_verification": "not_reached",
            "outcome": (
                "apply_candidate_selected"
                if count == 1 and href
                else "ambiguous_apply_controls"
                if count > 1
                else "apply_bound_job_root_not_found"
                if not bound_job_root_found
                else "apply_affordance_has_no_get_destination"
                if int(raw.get("bound_apply_affordance_count", 0) or 0) > 0
                else "apply_control_not_found"
            ),
        }
        selected_candidate = raw.get("selected_candidate")
        if isinstance(selected_candidate, Mapping):
            self._apply_hop_evidence["selected_candidate"] = {
                "accessible_name": str(
                    selected_candidate.get("label") or ""
                ).strip()[:200],
                "role": str(selected_candidate.get("role") or "")[:40],
                "first_party": bool(selected_candidate.get("first_party")),
                "third_party": bool(selected_candidate.get("third_party")),
                "inside_job_root": bool(
                    selected_candidate.get("inside_job_root")
                ),
            }
        if self._requisition_binding_required:
            self._apply_hop_evidence.update(
                {
                    "expected_requisition": self.authoritative_requisition,
                    "requisition_source": self.requisition_source,
                    "requisition_match": bool(self._requisition_root_marker),
                    "role_corroborated": self._requisition_role_corroborated,
                    "employer_corroborated": (
                        self._requisition_employer_corroborated
                    ),
                }
            )
        if role_identity_evidence is not None:
            self._apply_hop_evidence["role_identity"] = role_identity_evidence
        if count != 1 or not href:
            return ""
        if self._requisition_binding_required:
            if self._expected_job_identity is None or not identity_matches_url(
                self._expected_job_identity, href
            ):
                self._apply_hop_evidence["requisition_match"] = False
                self._apply_hop_evidence["outcome"] = (
                    "apply_destination_requisition_unproven"
                )
                return ""
        self._discovery_source_url = _request_url_without_fragment(source)
        self._discovery_candidate_url = _request_url_without_fragment(href)
        return href

    def _discover_apply_affordance(
        self,
        page: Any,
        *,
        apply_click_identity_only: bool = False,
    ) -> ApplyAffordance | None:
        """Classify one unique, identity-rooted JS Apply control.

        This runs only after the existing GET discovery found no destination.
        It marks one selected element with an opaque selector but performs no
        action; :func:`guarded_apply_click` re-resolves and revalidates that
        exact element immediately before the sole possible click.
        """

        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            self._apply_click_evidence["outcome"] = "apply_click_inspection_failed"
            return None
        marker = uuid.uuid4().hex
        script = r"""
        ({role, employer, marker,
          apply_click_identity_only: applyClickIdentityOnly,
          role_match_v2_marker: roleMatchV2Marker,
          role_match_v2_title: roleMatchV2Title,
          role_match_v2_title_source: roleMatchV2TitleSource,
          role_match_v2_binding_mode: roleMatchV2BindingMode,
          role_match_v2_enabled: roleMatchV2Enabled,
          requisition_marker: requisitionMarker,
          requisition_binding_required: requisitionBindingRequired}) => {
          const visible = node => {
            if (!node) return false;
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
              rect.width > 0 && rect.height > 0 && !node.disabled &&
              node.getAttribute('aria-disabled') !== 'true';
          };
          const nameKey = value => String(value || '').normalize('NFKC')
            .toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
          const explicitNames = new Set([
            'apply', 'apply now', 'apply for this job',
            'apply for this position', 'apply here', 'apply on employer site',
            'apply online', 'begin application', 'start application',
            'apply with linkedin', 'apply with indeed', 'apply with google',
            'apply with dropbox'
          ]);
          const explicitApplyName = value => explicitNames.has(nameKey(value)) &&
            !/\\b(?:submit|send|confirm|delete|withdraw|sign out|payment|pay|checkout|finish|continue|next)\\b/i
              .test(nameKey(value));
          const identity = value => String(value || '').toLowerCase()
            .replace(/[^a-z0-9]+/g, ' ').trim();
          const containsIdentity = (container, expected) => Boolean(expected) &&
            ` ${container} `.includes(` ${expected} `);
          const rootIdentity = root => identity(
            `${root?.innerText || root?.textContent || ''} ` +
            `${root?.getAttribute?.('data-employer') || ''} ` +
            `${root?.getAttribute?.('data-company') || ''} ` +
            `${root?.getAttribute?.('data-company-name') || ''} ` +
            `${root?.getAttribute?.('data-role') || ''} ` +
            `${root?.getAttribute?.('data-role-title') || ''} ` +
            `${root?.getAttribute?.('data-job-title') || ''}`
          );
          const accessibleName = node => String(
            node.getAttribute('aria-label') ||
            String(node.getAttribute('aria-labelledby') || '').split(/\s+/)
              .filter(Boolean).map(id => document.getElementById(id))
              .filter(Boolean).map(item => item.innerText || item.textContent || '')
              .join(' ') || node.getAttribute('title') || node.innerText ||
            node.textContent || node.getAttribute('value') || ''
          ).replace(/\s+/g, ' ').trim().slice(0, 200);
          const roleOf = node => String(node.getAttribute('role') ||
            (node.tagName.toLowerCase() === 'button' ? 'button' :
             node.tagName.toLowerCase() === 'a' ? 'link' : '')
          ).trim().toLowerCase();
          const roleText = identity(role);
          const employerText = identity(employer);
          const selectedTitleText = identity(roleMatchV2Title);
          const exactRoleTitle = pageTitle => {
            if (!pageTitle || !roleText) return false;
            const variants = new Set([pageTitle]);
            const prefix = employerText ? `${employerText} ` : '';
            const suffix = employerText ? ` ${employerText}` : '';
            if (prefix && pageTitle.startsWith(prefix)) {
              variants.add(pageTitle.slice(prefix.length));
            }
            if (suffix && pageTitle.endsWith(suffix)) {
              variants.add(pageTitle.slice(0, -suffix.length));
            }
            if (prefix && suffix && pageTitle.startsWith(prefix) &&
                pageTitle.endsWith(suffix)) {
              variants.add(pageTitle.slice(prefix.length, -suffix.length));
            }
            return variants.has(roleText);
          };
          const rootSelector =
            '[data-argus-requisition-root], [data-source-listing], ' +
            '[data-job-listing], [data-job-detail], .jobDisplay, ' +
            '.job-display, main, article, section';
          const roots = Array.from(document.querySelectorAll(rootSelector))
            .filter(visible);
          const rootFor = heading =>
            heading.closest(
              '[data-source-listing], [data-job-listing], [data-job-detail], ' +
              '.jobDisplay, .job-display, article'
            ) || heading.closest('section, main');
          const interactiveCount = candidateRoot => Array.from(
            candidateRoot.querySelectorAll('a[href], button, [role="button"]')
          ).filter(visible).slice(0, 500).length;
          const titleNodes = candidateRoot => Array.from(
            candidateRoot.querySelectorAll(
              'h1, h2, h3, [role="heading"], [data-job-title], [data-role-title]'
            )
          ).filter(visible).slice(0, 500).filter(heading =>
            rootFor(heading) === candidateRoot
          );
          const safeRoot = candidateRoot => {
            if (!candidateRoot || !roots.includes(candidateRoot)) return false;
            if (candidateRoot.matches(
                '[data-source-listing], [data-job-listing], [data-job-detail], ' +
                '.jobDisplay, .job-display, article')) {
              return titleNodes(candidateRoot).length === 1;
            }
            return interactiveCount(candidateRoot) === 0 &&
              titleNodes(candidateRoot).length === 1;
          };
          const markedRootValid = candidateRoot => {
            if (!candidateRoot || !selectedTitleText) return false;
            if (!safeRoot(candidateRoot)) return false;
            const matchingHeadings = titleNodes(candidateRoot).filter(heading =>
              identity(heading.innerText || heading.textContent || '') ===
                selectedTitleText
            );
            if (matchingHeadings.length !== 1) return false;
            if (roleMatchV2BindingMode === 'strict_exact') {
              const rootText = rootIdentity(candidateRoot);
              return exactRoleTitle(selectedTitleText) &&
                containsIdentity(rootText, employerText);
            }
            return roleMatchV2BindingMode === 'fallback' &&
              containsIdentity(selectedTitleText, employerText);
          };
          const markedRoots = roleMatchV2Marker ? roots.filter(node =>
            node.getAttribute('data-argus-role-match-v2-root') === roleMatchV2Marker
          ) : [];
          const markedRoot = markedRoots.length === 1 &&
            markedRootValid(markedRoots[0]) ? markedRoots[0] : null;
          if (roleMatchV2Marker && !markedRoot) {
            for (const node of document.querySelectorAll(
              '[data-argus-role-match-v2-root], [data-argus-resolution-affordance]'
            )) {
              node.removeAttribute('data-argus-role-match-v2-root');
              node.removeAttribute('data-argus-resolution-affordance');
            }
          }
          const legacyRoot = roots.find(node => {
            if (node.matches(
                '[data-source-listing], [data-job-listing], [data-job-detail], ' +
                '.jobDisplay, .job-display')) return true;
            const text = rootIdentity(node);
            return (!roleText || text.includes(roleText)) &&
              (!employerText || text.includes(employerText));
          }) || null;
          const strictRoots = roleMatchV2Enabled || roleMatchV2Marker ||
            applyClickIdentityOnly
            ? Array.from(new Set(Array.from(document.querySelectorAll(
                'h1, h2, h3, [role="heading"], [data-job-title], [data-role-title]'
              )).filter(visible).slice(0, 500).filter(heading => {
                const headingText = identity(
                  heading.innerText || heading.textContent || ''
                );
                const candidateRoot = rootFor(heading);
                const rootText = rootIdentity(candidateRoot);
                return exactRoleTitle(headingText) &&
                  containsIdentity(rootText, employerText) && safeRoot(candidateRoot);
              }).map(rootFor)))
            : [];
          const strictRoot = strictRoots.length === 1 ? strictRoots[0] : null;
          const requisitionRoots = requisitionMarker ? roots.filter(node =>
            node.getAttribute('data-argus-requisition-root') === requisitionMarker
          ) : [];
          const requisitionRoot = requisitionRoots.length === 1
            ? requisitionRoots[0] : null;
          const root = requisitionBindingRequired && !applyClickIdentityOnly
            ? requisitionRoot
            : applyClickIdentityOnly
              ? strictRoot
              : roleMatchV2Marker
                ? markedRoot
                : roleMatchV2Enabled ? strictRoot : legacyRoot;
          if (!root) return {count: 0, page_url: location.href, candidates: []};
          const applyRootMarker = `${marker}-root`;
          if (applyClickIdentityOnly) {
            for (const node of document.querySelectorAll(
              '[data-argus-apply-root]'
            )) node.removeAttribute('data-argus-apply-root');
            root.setAttribute('data-argus-apply-root', applyRootMarker);
          }
          const controls = Array.from(root.querySelectorAll(
            'button, [role="button"], a, [role="link"]'
          )).filter(visible).slice(0, 100).filter(node => {
            const type = String(node.getAttribute('type') || '').toLowerCase();
            if (node.hasAttribute('data-href') ||
                node.hasAttribute('data-url')) return false;
            return explicitApplyName(accessibleName(node));
          });
          const affordanceCandidates = controls.map((control, index) => {
            const name = accessibleName(control);
            const role = roleOf(control);
            const thirdParty = /^apply with (linkedin|indeed|google|dropbox)$/i
              .test(nameKey(name));
            return {
              control,
              candidate_id: `${marker}-${index}`,
              accessible_name: name,
              role,
              first_party: !thirdParty,
              third_party: thirdParty,
              inside_job_root: true,
              score: [
                thirdParty ? 0 : 1,
                1,
                thirdParty ? 0 : 1,
                role === 'button' ? 1 : 0,
              ],
            };
          });
          const ranked = affordanceCandidates.slice().sort((left, right) => {
            for (let index = 0; index < left.score.length; index += 1) {
              if (left.score[index] !== right.score[index]) {
                return right.score[index] - left.score[index];
              }
            }
            return left.candidate_id.localeCompare(right.candidate_id);
          });
          const best = ranked.length ? ranked[0] : null;
          const bestScore = best ? best.score.join('.') : '';
          const ties = best ? ranked.filter(item => item.score.join('.') === bestScore) : [];
          const selected = ties.length === 1 ? best : null;
          if (selected) {
            selected.control.setAttribute('data-argus-resolution-affordance', marker);
          }
          const selectedSelector = selected
            ? requisitionBindingRequired && !applyClickIdentityOnly
              ? `[data-argus-requisition-root="${requisitionMarker}"] ` +
                `[data-argus-resolution-affordance="${marker}"]`
              : applyClickIdentityOnly
              ? `[data-argus-apply-root="${applyRootMarker}"] ` +
                `[data-argus-resolution-affordance="${marker}"]`
              : roleMatchV2Marker
              ? `[data-argus-role-match-v2-root="${roleMatchV2Marker}"] ` +
                `[data-argus-resolution-affordance="${marker}"]`
              : `[data-argus-resolution-affordance="${marker}"]`
            : '';
          if (!selected || affordanceCandidates.length !== 1) {
            return {
              count: selected ? 1 : affordanceCandidates.length,
              candidate_count: affordanceCandidates.length,
              page_url: location.href,
              selector: selectedSelector,
              accessible_name: selected ? selected.accessible_name : '',
              role: selected ? selected.role : '',
              selected_candidate: selected ? {
                accessible_name: selected.accessible_name,
                role: selected.role,
                first_party: selected.first_party,
                third_party: selected.third_party,
                inside_job_root: selected.inside_job_root,
              } : null,
              candidates: affordanceCandidates.slice(0, 20).map(item => ({
                accessible_name: item.accessible_name,
                role: item.role,
                first_party: item.first_party,
                third_party: item.third_party,
                inside_job_root: item.inside_job_root,
              })),
            };
          }
          const control = selected.control;
          const selector = selectedSelector;
          return {
            count: 1,
            candidate_count: affordanceCandidates.length,
            selector,
            accessible_name: accessibleName(control),
            role: roleOf(control),
            root_selector: applyClickIdentityOnly
              ? `[data-argus-apply-root="${applyRootMarker}"]`
              : '',
            page_url: location.href,
            candidates: affordanceCandidates.map(item => ({
              accessible_name: item.accessible_name,
              role: item.role,
              first_party: item.first_party,
              third_party: item.third_party,
              inside_job_root: item.inside_job_root,
            })),
          };
        }
        """
        args = {
            "role": self.role_title[:300],
            "employer": (
                "" if self._placeholder_employer(self.employer) else self.employer[:300]
            ),
            "marker": marker,
            "apply_click_identity_only": bool(apply_click_identity_only),
            "role_match_v2_marker": self._role_match_v2_marker,
            "role_match_v2_title": self._role_match_v2_title,
            "role_match_v2_title_source": self._role_match_v2_title_source,
            "role_match_v2_binding_mode": self._role_match_v2_binding_mode,
            "role_match_v2_enabled": self.role_match_v2_enabled,
            "requisition_marker": self._requisition_root_marker,
            "requisition_binding_required": self._requisition_binding_required,
        }
        def scope_url(scope: Any) -> str:
            value = getattr(scope, "url", "")
            try:
                value = value() if callable(value) else value
            except Exception:  # noqa: BLE001 - frame metadata is advisory
                value = ""
            return str(value or "").strip()[:2000]

        page_url = self._page_url(page, self.inspection_url)

        def same_origin_scope(scope: Any) -> bool:
            frame_url = scope_url(scope)
            if not frame_url or frame_url.casefold() == "about:blank":
                return True
            try:
                return origin_for_url(frame_url) == origin_for_url(page_url)
            except (TypeError, ValueError):
                return False

        scopes: list[tuple[Any, int]] = [(page, -1)]
        frames = getattr(page, "frames", ()) or ()
        for index, frame in enumerate(list(frames)[:10]):
            if frame is page:
                continue
            parent_frame = getattr(frame, "parent_frame", None)
            if parent_frame is None:
                continue
            if same_origin_scope(frame):
                scopes.append((frame, index))

        raw = None
        selected_frame_index = -1
        selected_frame_url = ""
        for attempt in range(6):
            findings: list[tuple[Mapping[str, Any], int, str, int]] = []
            for scope, frame_index in scopes:
                scope_evaluate = getattr(scope, "evaluate", None)
                if not callable(scope_evaluate):
                    continue
                try:
                    candidate = scope_evaluate(script, args)
                except TypeError:
                    try:
                        candidate = scope_evaluate(script)
                    except Exception:  # noqa: BLE001 - opaque DOM is refused
                        candidate = None
                except Exception:  # noqa: BLE001 - opaque DOM is refused
                    candidate = None
                if not isinstance(candidate, Mapping):
                    continue
                try:
                    count = max(0, int(candidate.get("count", 0) or 0))
                except (TypeError, ValueError):
                    count = 0
                if count:
                    findings.append(
                        (
                            candidate,
                            frame_index,
                            scope_url(scope) or str(candidate.get("page_url") or ""),
                            count,
                        )
                    )
            total = sum(item[3] for item in findings)
            if total == 1 and findings:
                raw, selected_frame_index, selected_frame_url, _ = findings[0]
                break
            if total > 1 and findings:
                raw = dict(findings[0][0])
                raw["count"] = total
                raw["candidate_count"] = total
                break
            if attempt >= 5:
                break
            waiter = getattr(page, "wait_for_timeout", None)
            if not callable(waiter):
                break
            try:
                waiter(50)
            except Exception:  # noqa: BLE001 - bounded observation is best effort
                break
        if not isinstance(raw, Mapping):
            self._apply_click_evidence["outcome"] = "apply_click_inspection_failed"
            return None
        try:
            count = int(raw.get("count", 0) or 0)
        except (TypeError, ValueError):
            count = 0
        candidates = raw.get("candidates")
        try:
            candidate_count = int(raw.get("candidate_count", count) or 0)
        except (TypeError, ValueError):
            candidate_count = max(0, count)
        self._apply_click_evidence["affordance_count"] = max(0, candidate_count)
        if isinstance(candidates, (list, tuple)):
            candidate_names: list[str] = []
            candidate_records: list[dict[str, object]] = []
            for item in candidates[:20]:
                if isinstance(item, Mapping):
                    name = str(item.get("accessible_name") or "").strip()[:200]
                    candidate_records.append(
                        {
                            "accessible_name": name,
                            "role": str(item.get("role") or "")[:40],
                            "first_party": bool(item.get("first_party")),
                            "third_party": bool(item.get("third_party")),
                            "inside_job_root": bool(item.get("inside_job_root")),
                        }
                    )
                else:
                    name = str(item or "").strip()[:200]
                if name:
                    candidate_names.append(name)
            if candidate_names:
                self._apply_click_evidence["candidate_names"] = candidate_names
            if candidate_records:
                self._apply_click_evidence["candidate_records"] = candidate_records
        ranked_candidate = None
        has_structured_candidates = False
        if isinstance(candidates, (list, tuple)):
            has_structured_candidates = any(
                isinstance(item, Mapping) for item in candidates[:20]
            )
            ranked_candidate = select_ranked_apply_candidate(
                tuple(
                    ApplyCandidate(
                        accessible_name=str(item.get("accessible_name") or ""),
                        role=str(item.get("role") or ""),
                        inside_job_root=bool(item.get("inside_job_root")),
                        first_party=bool(item.get("first_party")),
                        third_party=bool(item.get("third_party")),
                    )
                    for item in candidates[:20]
                    if isinstance(item, Mapping)
                )
            )
        selected_candidate = raw.get("selected_candidate")
        if isinstance(selected_candidate, Mapping):
            self._apply_click_evidence["selected_candidate"] = {
                "accessible_name": str(
                    selected_candidate.get("accessible_name") or ""
                ).strip()[:200],
                "role": str(selected_candidate.get("role") or "")[:40],
                "first_party": bool(selected_candidate.get("first_party")),
                "third_party": bool(selected_candidate.get("third_party")),
                "inside_job_root": bool(
                    selected_candidate.get("inside_job_root")
                ),
            }
        if count > 1:
            self._apply_click_evidence["outcome"] = (
                "apply_click_multiple_affordances"
            )
            return None
        if count != 1:
            self._apply_click_evidence["outcome"] = "apply_click_no_affordance"
            return None
        selector = str(raw.get("selector") or "").strip()[:500]
        accessible_name = str(raw.get("accessible_name") or "").strip()[:200]
        role = str(raw.get("role") or "").casefold().strip()[:40]
        if (
            not selector
            or not accessible_name
            or not is_explicit_apply_name(accessible_name)
            or role not in {"button", "link"}
        ):
            self._apply_click_evidence["outcome"] = "apply_click_ambiguous"
            return None
        if has_structured_candidates and ranked_candidate is None:
            self._apply_click_evidence["outcome"] = "apply_click_ambiguous"
            return None
        if ranked_candidate is not None and (
            ranked_candidate.accessible_name.casefold() != accessible_name.casefold()
            or ranked_candidate.role != role
        ):
            self._apply_click_evidence["outcome"] = "apply_click_affordance_changed"
            return None
        self._apply_click_evidence["frame_index"] = selected_frame_index
        if selected_frame_url:
            self._apply_click_evidence["frame_url"] = selected_frame_url
        return ApplyAffordance(
            selector,
            accessible_name,
            role,
            root_selector=str(
                raw.get("root_selector") or self._requisition_root_selector
            ).strip()[:500],
            requisition=self.authoritative_requisition,
            provider=self.requisition_provider,
            tenant=self.requisition_tenant,
            root_employer=(
                self.employer[:300] if apply_click_identity_only else ""
            ),
            root_role_title=(
                self.role_title[:300] if apply_click_identity_only else ""
            ),
            frame_url=selected_frame_url,
            frame_index=selected_frame_index,
        )

    def _attempt_guarded_apply_click(self, page: Any) -> bool:
        """Attempt the one budgeted click and return whether to re-inspect."""

        if self._apply_click_attempted:
            return False
        self._apply_click_attempted = True
        page_url = self._page_url(page, self.inspection_url)
        capability = self.source_capability
        capability_evidence = {
            "id": str(getattr(capability, "capability_id", "") or "")[:128],
            "source_url": str(getattr(capability, "source_url", "") or "")[:1000],
            "hostname": str(getattr(capability, "hostname", "") or "")[:255],
            "origin": str(getattr(capability, "origin", "") or "")[:500],
        }
        self._apply_click_evidence = {
            "attempted": True,
            "clicked": False,
            "opportunity_id": self.opportunity_id,
            "application_id": self.application_id,
            "page_url": page_url,
            "result_url": page_url,
            "pre_click_url": page_url,
            "post_click_url": page_url,
            "accessible_name": "",
            "control_role": "",
            "verification_verdict": "not_reached",
            "context_discarded": False,
            "destination_kind": TargetKind.JOB_DETAIL.value,
            "egress_guard": "not_reached",
            "capability": capability_evidence,
            "outcome": "apply_click_not_reached",
        }
        if capability is None or not bool(getattr(capability, "active", False)):
            self._apply_click_evidence["outcome"] = (
                "apply_click_capability_unavailable"
            )
            return False
        try:
            allowed_origins = self._apply_click_allowed_origins()
            if (
                not allowed_origins
                or origin_for_url(page_url) not in allowed_origins
            ):
                self._apply_click_evidence["outcome"] = (
                    "apply_click_capability_mismatch"
                )
                return False
        except ValueError:
            self._apply_click_evidence["outcome"] = "apply_click_capability_mismatch"
            return False

        if self._requisition_binding_required and self.authoritative_requisition and (
            self._expected_job_identity is None
            or not self._requisition_root_marker
            or not self._requisition_root_selector
        ):
            self._apply_click_evidence["outcome"] = (
                "apply_click_requisition_unavailable"
            )
            return False

        affordance = self._discover_apply_affordance(
            page,
            apply_click_identity_only=not bool(self.authoritative_requisition),
        )
        if affordance is None:
            return False
        budget = self.apply_click_budget
        if budget is None or not budget.try_consume():
            self._apply_click_evidence["outcome"] = "apply_click_run_cap_reached"
            return False

        self._apply_click_active = True
        result = guarded_apply_click(
            page,
            affordance,
            timeout_ms=self.apply_click_timeout_ms,
        )
        self._apply_click_evidence.update(
            {
                "clicked": bool(result.clicked),
                "accessible_name": result.accessible_name,
                "control_role": result.role,
                "outcome": result.outcome,
            }
        )
        if not result.clicked:
            self._apply_click_evidence["verification_verdict"] = "not_clicked"
            return False
        wait_for_url = getattr(page, "wait_for_url", None)
        if callable(wait_for_url):
            try:
                # Playwright's URL waiter survives the execution-context
                # destruction caused by a real document navigation.  A DOM
                # predicate does not: it can race the click-triggered reload
                # and report a false timeout even after the URL has changed.
                wait_for_url(
                    lambda candidate: str(candidate) != page_url,
                    wait_until="domcontentloaded",
                    timeout=self.apply_click_timeout_ms,
                )
            except Exception:  # noqa: BLE001 - bounded timeout is fail-closed
                self._apply_click_evidence["result_url"] = self._page_url(
                    page, page_url
                )
                self._apply_click_evidence["post_click_url"] = self._apply_click_evidence[
                    "result_url"
                ]
                self._apply_click_evidence["verification_verdict"] = "navigation_failed"
                self._apply_click_evidence["context_discarded"] = True
                self._apply_click_evidence["outcome"] = "apply_click_timeout"
                return False
        wait_for_load_state = getattr(page, "wait_for_load_state", None)
        if callable(wait_for_load_state):
            try:
                wait_for_load_state(
                    "domcontentloaded",
                    timeout=self.apply_click_timeout_ms,
                )
            except Exception:  # noqa: BLE001 - bounded timeout is fail-closed
                self._apply_click_evidence["result_url"] = self._page_url(
                    page, page_url
                )
                self._apply_click_evidence["post_click_url"] = self._apply_click_evidence[
                    "result_url"
                ]
                self._apply_click_evidence["verification_verdict"] = "navigation_failed"
                self._apply_click_evidence["context_discarded"] = True
                self._apply_click_evidence["outcome"] = "apply_click_timeout"
                return False
        result_url = self._page_url(page, page_url)
        self._apply_click_evidence["result_url"] = result_url
        self._apply_click_evidence["post_click_url"] = result_url
        allowed_origins = self._apply_click_allowed_origins()
        try:
            if origin_for_url(result_url) not in allowed_origins:
                self._apply_click_evidence["outcome"] = "apply_click_off_capability"
                self._apply_click_evidence["verification_verdict"] = "mismatch"
                self._apply_click_evidence["context_discarded"] = True
                return False
        except ValueError:
            self._apply_click_evidence["outcome"] = "apply_click_off_capability"
            self._apply_click_evidence["verification_verdict"] = "mismatch"
            self._apply_click_evidence["context_discarded"] = True
            return False
        self._apply_click_evidence["outcome"] = "apply_click_navigation_completed"
        self._apply_click_evidence["verification_verdict"] = "pending"
        return True

    def _approved_discovery_url(self, page: Any, candidate: str) -> str:
        """Authorize a discovered GET target without trusting page markup."""

        current = self._page_url(page, self.inspection_url)
        try:
            resolved = urljoin(current, candidate)
            if (
                _request_url_without_fragment(current) != self._discovery_source_url
                or _request_url_without_fragment(resolved)
                != self._discovery_candidate_url
            ):
                self._apply_hop_evidence["outcome"] = "navigation_not_page_bound"
                self._apply_hop_evidence["origin_binding"] = "page_binding_failed"
                return ""
            parsed_current = urlsplit(current)
            parsed_candidate = urlsplit(resolved)
            if parsed_candidate.scheme not in {"http", "https"}:
                self._apply_hop_evidence["origin_binding"] = "scheme_refused"
                return ""
            if parsed_candidate.username or parsed_candidate.password:
                self._apply_hop_evidence["origin_binding"] = "credentials_refused"
                return ""
            for key, value in parse_qsl(parsed_candidate.query, keep_blank_values=True):
                normalised_key = key.casefold().strip().replace("-", "_")
                if normalised_key not in _OPEN_REDIRECT_QUERY_KEYS:
                    continue
                nested = urlsplit(value.strip())
                if nested.scheme.casefold() in {"http", "https"} and nested.hostname:
                    self._apply_hop_evidence["outcome"] = "open_redirect_refused"
                    self._apply_hop_evidence["origin_binding"] = (
                        "nested_redirect_refused"
                    )
                    return ""
            current_origin = origin_for_url(current)
            candidate_origin = origin_for_url(resolved)
            same_origin = current_origin == candidate_origin
            candidate_provider = trusted_provider_for_url(resolved)
            expected_provider = str(self.provider_hint or "").casefold().strip()
            if expected_provider in {"unknown", "generic", "other"}:
                expected_provider = ""
            if not same_origin:
                if not candidate_provider:
                    self._apply_hop_evidence["origin_binding"] = (
                        "cross_origin_provider_untrusted"
                    )
                    return ""
            if self._requisition_binding_required and (
                self._expected_job_identity is None
                or not identity_matches_url(self._expected_job_identity, resolved)
            ):
                self._apply_hop_evidence["outcome"] = (
                    "apply_destination_requisition_unproven"
                )
                self._apply_hop_evidence["origin_binding"] = (
                    "requisition_binding_failed"
                )
                return ""
            if (
                not same_origin
                and expected_provider
                and candidate_provider != expected_provider
            ):
                self._apply_hop_evidence["origin_binding"] = (
                    "cross_origin_provider_mismatch"
                )
                return ""
            # Do not permit a no-op link or a URL that only differs by a
            # fragment.  The destination is still checked by the browser
            # egress guard before delivery.
            if (
                parsed_current.scheme.casefold() == parsed_candidate.scheme.casefold()
                and (parsed_current.netloc or "").casefold()
                == (parsed_candidate.netloc or "").casefold()
                and (parsed_current.path or "/") == (parsed_candidate.path or "/")
                and parsed_current.query == parsed_candidate.query
            ):
                self._apply_hop_evidence["origin_binding"] = "no_op_destination"
                return ""
            allowlist = getattr(self, "allowlist", frozenset())
            try:
                approved = _validate_navigation_url(resolved, allowlist)
                self._apply_hop_evidence["origin_binding"] = "passed"
                return approved
            except ValueError:
                # A source listing may hand off to the exact trusted ATS host
                # even when that provider is absent from the general live
                # allowlist.  The owner-thread route still permits only the
                # read-only GET document/passive path, and observed
                # identity/form proof remains mandatory.
                if not candidate_provider:
                    self._apply_hop_evidence["origin_binding"] = (
                        "host_not_authorized"
                    )
                    return ""
                self._apply_hop_evidence["origin_binding"] = "passed"
                return resolved
        except (TypeError, ValueError):
            self._apply_hop_evidence["origin_binding"] = "invalid_destination"
            return ""

    def _follow_discovered_application(self, page: Any) -> bool:
        """Navigate once to an approved application link on the owner thread."""

        candidate = self._discover_application_link(page)
        if not candidate:
            return False
        target = self._approved_discovery_url(page, candidate)
        if not target:
            if self._apply_hop_evidence.get("outcome") == "apply_candidate_selected":
                self._apply_hop_evidence["outcome"] = "navigation_not_authorized"
            return False
        goto = getattr(page, "goto", None)
        if not callable(goto):
            self._apply_hop_evidence["outcome"] = "navigation_unavailable"
            return False
        self._apply_hop_target_url = target
        self._apply_hop_evidence["target_url"] = target
        self._apply_hop_evidence["navigation_started"] = True
        self._apply_hop_evidence["outcome"] = "navigation_started"
        try:
            response = goto(
                target, wait_until="domcontentloaded", timeout=10_000
            )
            self.record_document_response(response)
        except Exception:  # noqa: BLE001 - blocked/failed navigation stays unresolved
            guard = self._apply_hop_evidence.get("egress_guard")
            self._apply_hop_evidence["outcome"] = (
                "apply_egress_guard_refused"
                if isinstance(guard, Mapping) and not bool(guard.get("allowed"))
                else "navigation_failed"
            )
            return False
        self._discovered_url = target
        final = self._page_url(page, target)
        redirect_chain = self._apply_redirect_chain or (target, final)
        hop_chain = [self._discovery_source_url]
        for item in redirect_chain:
            value = _request_url_without_fragment(item)
            if value and (not hop_chain or hop_chain[-1] != value):
                hop_chain.append(value)
        self._apply_hop_evidence["hop_chain"] = hop_chain
        self._apply_hop_evidence["destination_url"] = final
        try:
            if origin_for_url(final) != origin_for_url(target):
                self._apply_hop_evidence["outcome"] = "redirected_off_bound_origin"
                return False
        except ValueError:
            self._apply_hop_evidence["outcome"] = "navigation_destination_invalid"
            return False
        self._apply_hop_evidence["outcome"] = "navigation_completed"
        return True

    def _with_apply_hop_evidence(
        self,
        resolution: TargetResolution,
    ) -> TargetResolution:
        if not self._apply_hop_evidence:
            return resolution
        evidence = dict(resolution.evidence)
        evidence["apply_hop"] = dict(self._apply_hop_evidence)
        outcome = str(self._apply_hop_evidence.get("outcome") or "").strip()
        reason_codes = tuple(resolution.reason_codes)
        if outcome and outcome != "verified_application_form":
            apply_reason = (
                outcome if outcome.startswith("apply_") else f"apply_{outcome}"
            )
            reason_codes = tuple(dict.fromkeys((*reason_codes, apply_reason)))
        return replace(resolution, evidence=evidence, reason_codes=reason_codes)

    def _with_apply_click_evidence(
        self,
        resolution: TargetResolution,
    ) -> TargetResolution:
        if not self._apply_click_evidence:
            return resolution
        evidence = dict(resolution.evidence)
        evidence["apply_click"] = dict(self._apply_click_evidence)
        outcome = str(self._apply_click_evidence.get("outcome") or "").strip()
        reason_codes = tuple(resolution.reason_codes)
        if outcome and outcome != "verified_application_form":
            reason_codes = tuple(dict.fromkeys((*reason_codes, outcome)))
        if outcome == "apply_click_destination_mismatch":
            checks = self._apply_click_evidence.get("destination_verification")
            if isinstance(checks, Mapping):
                diagnostic = ""
                if not bool(checks.get("provider_verified")):
                    diagnostic = "apply_destination_provider_unverified"
                elif not bool(checks.get("provider_bound")):
                    diagnostic = "apply_destination_provider_mismatch"
                elif self._requisition_binding_required and not bool(
                    checks.get("destination_origin_bound")
                ):
                    diagnostic = "apply_destination_origin_mismatch"
                elif self._requisition_binding_required and not bool(
                    checks.get("requisition_observed")
                ):
                    diagnostic = "apply_destination_requisition_missing"
                elif self._requisition_binding_required and not bool(
                    checks.get("requisition_bound")
                ):
                    diagnostic = "apply_destination_requisition_mismatch"
                elif not bool(checks.get("employer_observed")):
                    diagnostic = "apply_destination_employer_missing"
                elif not bool(checks.get("employer_bound")):
                    diagnostic = "apply_destination_employer_mismatch"
                elif not bool(checks.get("role_observed")):
                    diagnostic = "apply_destination_role_missing"
                elif not bool(checks.get("role_bound")):
                    diagnostic = "apply_destination_role_mismatch"
                elif not bool(checks.get("form_visible")):
                    diagnostic = "apply_destination_form_not_visible"
                if diagnostic:
                    reason_codes = tuple(
                        dict.fromkeys((*reason_codes, diagnostic))
                    )
        return replace(resolution, evidence=evidence, reason_codes=reason_codes)

    @staticmethod
    def _identity(value: object) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))

    def _inspect(
        self,
        page: Any,
        *,
        follow_job_detail: bool = False,
        follow_listing: bool = False,
    ) -> Mapping[str, object]:
        final_url = self._page_url(page, self.inspection_url)
        self._wait_for_provider_hydration(page, final_url)
        html = self._page_html(page)
        evidence: dict[str, object] = {
            "source_inspection": True,
            "inspection_binding": {
                "source_url": self.source_url,
                "inspection_url": self.inspection_url,
                "job_identity": (
                    self._expected_job_identity.as_evidence()
                    if self._expected_job_identity is not None
                    else {}
                ),
            },
        }
        if self.redirect_chain:
            evidence["redirect_chain"] = list(self.redirect_chain)
        elif self._source_navigation_chain:
            evidence["redirect_chain"] = list(self._source_navigation_chain)
        if self._apply_hop_evidence:
            evidence["apply_hop"] = dict(self._apply_hop_evidence)
        if self._apply_click_evidence:
            evidence["apply_click"] = dict(self._apply_click_evidence)
        hydration = self._hydration_evidence.get(
            _request_url_without_fragment(final_url)
        )
        if isinstance(hydration, Mapping):
            evidence["hydration"] = dict(hydration)
        response_metadata = self._response_metadata(final_url)
        if response_metadata:
            evidence["document_response"] = dict(response_metadata)
        try:
            resolution = classify_target(
                self.source_url,
                final_url,
                html=html,
                provider_hint=self.provider_hint,
                identity_verified=False,
                evidence=evidence,
                http_status=(
                    int(response_metadata.get("status") or 0) or None
                    if response_metadata
                    else None
                ),
                content_type=str(
                    response_metadata.get("content_type") or "text/html"
                ),
                content_disposition=str(
                    response_metadata.get("content_disposition") or ""
                ),
            )
        except (TypeError, ValueError):
            # Keep a typed unresolved result even when a provider page changes
            # URL shape or returns malformed markup.  The caller will persist
            # a human handoff instead of treating this as target proof.
            resolution = TargetResolution(
                source_url=self.source_url,
                final_url=self.source_url,
                kind=TargetKind.UNRESOLVED,
                provider="",
                reason_codes=("source_inspection_invalid",),
                evidence={"source_inspection": True},
            )
        # A target is promotable only after the user has brought the same
        # owner-thread page to an exact ATS entry/form and the page itself has
        # exposed structured provider/employer/role/requisition evidence.
        # Stored values are compared to those observations; they are never
        # copied into proof before the comparison.
        observed = self._page_observation(page)
        actual_apply_click = bool(
            self._apply_click_evidence.get("clicked")
            and (
                self._apply_click_active
                or self._apply_click_evidence.get("pre_click_url")
                or self._apply_click_evidence.get("page_url")
            )
        )
        if actual_apply_click:
            try:
                ApplyClickSafetyViolation.raise_if_landing_is_submission(
                    url=final_url,
                    title=self._page_title(page),
                    text=self._page_html(page),
                    observation=observed,
                )
            except ApplyClickSafetyViolation:
                self._apply_click_fatal = True
                self._apply_click_evidence.update(
                    {
                        "post_click_url": final_url,
                        "verification_verdict": "fatal_submission_boundary",
                        "context_discarded": True,
                        "outcome": "apply_click_submission_boundary_violation",
                    }
                )
                raise ApplyClickSafetyViolation(
                    "Apply click landed on a confirmation/submitted page; "
                    "the resolution-only run is halted",
                    evidence=dict(self._apply_click_evidence),
                )
        structured_job = _structured_job_posting(html)
        if bool(observed.get("application_entry_visible")):
            for key in ("employer", "role", "requisition"):
                if not observed.get(key) and structured_job.get(key):
                    observed[key] = structured_job[key]
        provider = trusted_provider_for_url(final_url) or str(
            observed.get("provider") or ""
        ).casefold()
        synthetic_lab = _loopback_host(urlsplit(final_url).hostname) and bool(
            observed.get("provider")
        )
        # URL shape is navigation context only.  Requisition and form
        # identity must both come from the structured DOM observation above;
        # a plausible ``/application`` path is not proof of either binding.
        observed_requisition = str(observed.get("requisition") or "").strip()
        observed_form_identity = str(observed.get("form_identity") or "").strip()
        employer_observed = self._identity(observed.get("employer"))
        employer_bound = bool(employer_observed) and (
            self._placeholder_employer(self.employer)
            or employer_observed == self._identity(self.employer)
        )
        role_observed = self._identity(observed.get("role"))
        role_bound = bool(role_observed) and role_observed == self._identity(
            self.role_title
        )
        expected_requisition = self.authoritative_requisition
        requisition_bound = bool(
            expected_requisition
            and requisitions_equal(observed_requisition, expected_requisition)
            and requisitions_equal(observed_form_identity, expected_requisition)
        )
        provider_bound = bool(provider) and (
            not self._requisition_binding_required
            or not self.requisition_provider
            or provider == self.requisition_provider
        )
        try:
            same_persisted_origin = origin_for_url(final_url) == origin_for_url(
                self.inspection_url
            )
        except ValueError:
            same_persisted_origin = False
        final_identity_bound = bool(
            self._expected_job_identity is not None
            and identity_matches_url(self._expected_job_identity, final_url)
        )
        destination_origin_bound = bool(
            same_persisted_origin or final_identity_bound
        )
        if self._requisition_binding_required:
            identity_bound = bool(
                provider_bound
                and requisition_bound
                and destination_origin_bound
            )
        else:
            identity_bound = bool(
                provider_bound
                and employer_bound
                and role_bound
                and observed_requisition
                and observed_form_identity
            )
        form_handle: FormHandle | None = None
        root_selector = str(observed.get("root_selector") or "")
        form_action = str(observed.get("form_action") or "")
        if identity_bound and bool(observed.get("form_visible")) and root_selector:
            form_handle = FormHandle(
                page_id="source-resolution-page",
                frame_url=final_url,
                root_selector=root_selector,
                provider=provider,
                evidence={
                    "control_count": int(observed.get("control_count", 0) or 0),
                    "submit_present": bool(observed.get("submit_present")),
                    "root_found": True,
                    "root_token": observed_form_identity,
                    "binding_verified": True,
                    "bound_target_url": final_url,
                    "bound_provider": provider,
                    # Keep proof page-observed.  The stored employer/role
                    # values are comparison inputs, never self-attestation.
                    "bound_employer": str(observed.get("employer") or ""),
                    "bound_role": str(observed.get("role") or ""),
                    "bound_requisition": observed_requisition,
                    "bound_form_identity": observed_form_identity,
                    "form_action": form_action,
                },
            )
        if identity_bound and (
            trusted_provider_for_url(final_url) or synthetic_lab
        ):
            proof = {
                "source_inspection": True,
                "provider": provider,
                "employer": str(observed.get("employer") or ""),
                "role": str(observed.get("role") or ""),
                "requisition": observed_requisition,
                "form_identity": observed_form_identity,
                "application_origin": origin_for_url(final_url),
                "destination_origin_bound": destination_origin_bound,
                "role_corroborated": role_bound,
                "employer_corroborated": employer_bound,
            }
            if self._requisition_binding_required:
                # A false value is a load-bearing contradiction.  Do not emit
                # it for legacy/synthetic sources where no stored requisition
                # binding was required in the first place.
                proof["requisition_match"] = requisition_bound
            if self.redirect_chain:
                proof["redirect_chain"] = list(self.redirect_chain)
            if isinstance(hydration, Mapping):
                proof["hydration"] = dict(hydration)
            if response_metadata:
                proof["document_response"] = dict(response_metadata)
            if synthetic_lab:
                proof["synthetic_lab"] = True
            if (
                self._placeholder_employer(self.employer)
                and trusted_provider_for_url(final_url)
            ):
                proof["employer_established_from_provider"] = True
            if form_handle is not None:
                proof["form_action"] = form_action
            try:
                resolution = classify_target(
                    self.source_url,
                    final_url,
                    html=html,
                    provider_hint=provider,
                    identity_verified=True,
                    form_handle=form_handle,
                    evidence=proof,
                    http_status=(
                        int(response_metadata.get("status") or 0) or None
                        if response_metadata
                        else None
                    ),
                    content_type=str(
                        response_metadata.get("content_type") or "text/html"
                    ),
                    content_disposition=str(
                        response_metadata.get("content_disposition") or ""
                    ),
                )
            except (TypeError, ValueError):
                # Retain the earlier unverified classification on any
                # contradiction; an inspection exception is never proof.
                pass
        form_action_bound = True
        form_action_requisition_bound = True
        if form_action:
            try:
                form_action_bound = origin_for_url(form_action) == origin_for_url(final_url)
            except ValueError:
                form_action_bound = False
            form_action_identity = requisition_identity_from_url(form_action)
            if form_action_identity is not None and expected_requisition:
                form_action_requisition_bound = bool(
                    requisitions_equal(
                        form_action_identity.requisition,
                        expected_requisition,
                    )
                    and (
                        not self.requisition_provider
                        or form_action_identity.provider == self.requisition_provider
                    )
                )
        verified_shape = bool(
            identity_bound
            and (
                not actual_apply_click
                or (employer_bound and role_bound)
            )
            and form_handle is not None
            and (trusted_provider_for_url(final_url) or synthetic_lab)
            and form_action_bound
            and form_action_requisition_bound
            and resolution.verified_for_automation
        )
        provider_verified = bool(trusted_provider_for_url(final_url) or synthetic_lab)
        source_checks = {
            "reached": True,
            "provider_verified": provider_verified,
            "provider_bound": provider_bound,
            "employer_observed": bool(employer_observed),
            "employer_bound": bool(employer_bound),
            "role_observed": bool(role_observed),
            "role_bound": bool(role_bound),
            "requisition_observed": bool(observed_requisition),
            "requisition_bound": requisition_bound,
            "destination_origin_bound": destination_origin_bound,
            "form_identity_observed": bool(observed_form_identity),
            "form_visible": bool(observed.get("form_visible")),
            "root_selector_present": bool(root_selector),
            "form_action_origin_bound": bool(form_action_bound),
            "form_action_requisition_bound": bool(form_action_requisition_bound),
            "automation_contract_verified": bool(
                resolution.verified_for_automation
            ),
        }
        if self._apply_origin_resolution is not None:
            destination_checks = {
                "reached": True,
                "provider_verified": provider_verified,
                "provider_bound": provider_bound,
                "employer_observed": bool(employer_observed),
                "employer_bound": bool(employer_bound),
                "role_observed": bool(role_observed),
                "role_bound": bool(role_bound),
                "requisition_observed": bool(observed_requisition),
                "requisition_bound": requisition_bound,
                "destination_origin_bound": destination_origin_bound,
                "form_identity_observed": bool(observed_form_identity),
                "form_visible": bool(observed.get("form_visible")),
                "root_selector_present": bool(root_selector),
                "form_action_origin_bound": bool(form_action_bound),
                "form_action_requisition_bound": bool(
                    form_action_requisition_bound
                ),
                "automation_contract_verified": bool(
                    resolution.verified_for_automation
                ),
            }
            click_attempt = bool(self._apply_click_evidence)
            destination_evidence = (
                self._apply_click_evidence
                if click_attempt
                else self._apply_hop_evidence
            )
            destination_evidence["destination_verification"] = destination_checks
            destination_evidence["destination_kind"] = resolution.kind.value
            destination_evidence["destination_identity_verified"] = bool(
                resolution.identity_verified
            )
            if verified_shape and resolution.kind is TargetKind.APPLICATION_FORM:
                destination_evidence["outcome"] = "verified_application_form"
                if actual_apply_click:
                    self._apply_click_evidence.update(
                        {
                            "post_click_url": final_url,
                            "verification_verdict": "verified",
                            "context_discarded": True,
                        }
                    )
                resolution = (
                    self._with_apply_click_evidence(resolution)
                    if click_attempt
                    else self._with_apply_hop_evidence(resolution)
                )
            elif click_attempt and resolution.kind in {
                TargetKind.AUTH_WALL,
                TargetKind.HUMAN_CHALLENGE,
            }:
                destination_evidence["outcome"] = (
                    "apply_click_auth_wall"
                    if resolution.kind is TargetKind.AUTH_WALL
                    else "apply_click_human_challenge"
                )
                if actual_apply_click:
                    self._apply_click_evidence.update(
                        {
                            "post_click_url": final_url,
                            "verification_verdict": "blocked",
                            "context_discarded": True,
                        }
                    )
                resolution = self._with_apply_click_evidence(resolution)
            else:
                destination_evidence_present = bool(
                    provider_verified
                    or employer_observed
                    or role_observed
                    or observed_requisition
                    or observed_form_identity
                    or observed.get("form_visible")
                    or root_selector
                )
                if actual_apply_click:
                    outcome = "apply_click_destination_mismatch"
                    self._apply_click_evidence.update(
                        {
                            "post_click_url": final_url,
                            "verification_verdict": "mismatch",
                            "context_discarded": True,
                        }
                    )
                elif not destination_evidence_present:
                    outcome = "destination_not_application_form"
                elif not provider_verified:
                    outcome = "apply_destination_provider_unverified"
                elif not provider_bound:
                    outcome = "apply_destination_provider_mismatch"
                elif self._requisition_binding_required and not destination_origin_bound:
                    outcome = "apply_destination_origin_mismatch"
                elif self._requisition_binding_required and not observed_requisition:
                    outcome = "apply_destination_requisition_missing"
                elif self._requisition_binding_required and not requisition_bound:
                    outcome = "apply_destination_requisition_mismatch"
                elif not self._requisition_binding_required and not employer_observed:
                    outcome = "apply_destination_employer_missing"
                elif not self._requisition_binding_required and not employer_bound:
                    outcome = "apply_destination_employer_mismatch"
                elif not self._requisition_binding_required and not role_observed:
                    outcome = "apply_destination_role_missing"
                elif not self._requisition_binding_required and not role_bound:
                    outcome = "apply_destination_role_mismatch"
                elif not observed_requisition:
                    outcome = "apply_destination_requisition_missing"
                elif not observed_form_identity:
                    outcome = "apply_destination_form_identity_missing"
                elif not bool(observed.get("form_visible")):
                    outcome = "apply_destination_form_not_visible"
                elif not root_selector:
                    outcome = "apply_destination_form_root_missing"
                elif not form_action_bound:
                    outcome = "apply_destination_form_action_origin_mismatch"
                elif not form_action_requisition_bound:
                    outcome = "apply_destination_form_action_requisition_mismatch"
                elif not resolution.verified_for_automation:
                    outcome = "apply_destination_contract_unverified"
                else:
                    outcome = "apply_destination_not_application_form"
                destination_evidence["outcome"] = outcome
                resolution = (
                    self._with_apply_click_evidence(self._apply_origin_resolution)
                    if click_attempt
                    else self._with_apply_hop_evidence(
                        self._apply_origin_resolution
                    )
                )
        elif (
            (follow_job_detail and resolution.kind is TargetKind.JOB_DETAIL)
            or (
                follow_listing
                and resolution.kind
                not in {TargetKind.LISTING, TargetKind.MULTIPLE_CANDIDATE_ROLES}
                and not verified_shape
            )
        ):
            self._apply_origin_resolution = resolution
            followed = self._follow_discovered_application(page)
            if self._apply_hop_evidence:
                self._apply_hop_evidence["source_verification"] = source_checks
            if followed:
                # Re-inspect exactly once. This is GET navigation only and
                # never clicks an Apply, Next, or Submit control.
                return self._inspect(page, follow_job_detail=False)
            if (
                self.apply_click_enabled
                and resolution.kind in {
                    TargetKind.JOB_DETAIL,
                    TargetKind.UNRESOLVED,
                }
            ):
                if self._attempt_guarded_apply_click(page):
                    self._apply_click_evidence["source_verification"] = source_checks
                    return self._inspect(page, follow_job_detail=False)
                if self._apply_click_evidence:
                    self._apply_click_evidence["source_verification"] = source_checks
                    resolution = self._with_apply_click_evidence(resolution)
                else:
                    resolution = self._with_apply_hop_evidence(resolution)
            else:
                resolution = self._with_apply_hop_evidence(resolution)
        self.resolution = resolution
        reasons = tuple(str(item) for item in resolution.reason_codes if str(item))
        return {
            "state": "NEEDS_USER",
            "reason": (
                "Visible source inspection is ready; verify the exact application "
                "destination in this session"
            ),
            "blocked_reasons": reasons or ("source_target_unverified",),
            "source_resolution": True,
            "resolution_kind": resolution.kind.value,
        }

    def prepare(self, page: Any) -> Mapping[str, object]:
        return self._inspect(page, follow_job_detail=True)

    def resume(self, page: Any) -> Mapping[str, object]:
        # Generic listings retain the established explicit human-handoff
        # boundary. Job-detail sources may be followed during prepare so the
        # non-interactive batch resolver can perform its single bounded hop.
        return self._inspect(
            page,
            follow_job_detail=True,
            follow_listing=True,
        )


class HeadedSessionWorker(threading.Thread):
    """One non-daemon owner thread for one browser session.

    The class exposes only scalar owner identity and an immutable diagnostics
    snapshot; no command accepts a page/context/browser object. Those
    diagnostics make thread-affinity and teardown assertions possible without
    allowing request code to operate Playwright handles.
    """

    def __init__(
        self,
        *,
        session_id: str,
        application_id: str,
        mode: str,
        url: str,
        summary: Mapping[str, object],
        command_queue: queue.Queue[SessionCommand],
        event_queue: queue.Queue[SessionEvent],
        ttl_seconds: float,
        deadline_monotonic: float | None = None,
        expires_at: datetime | None = None,
        headless: bool,
        executable_path: str | None = None,
        allowlist: frozenset[str] = frozenset(),
        allowlist_is_run_scoped: bool = False,
        navigation_timeout_ms: int = 10_000,
        playwright_factory: Callable[[], Any] | None = None,
        journey_executor: Any | None = None,
        captcha_provider_hosts: frozenset[str] = CAPTCHA_PROVIDER_HOSTS,
        resumable_on_expiry: bool = False,
        expiry_preservation_seconds: float = _DEFAULT_EXPIRY_PRESERVATION_SECONDS,
        egress_impact_classification_enabled: bool = True,
        cleanup_grace_seconds: float = _DEFAULT_CLEANUP_GRACE_SECONDS,
    ) -> None:
        super().__init__(name=f"argus-navigator-{session_id}", daemon=False)
        self.session_id = session_id
        self.application_id = application_id
        self.mode = mode
        self.url = url
        self.summary = dict(summary)
        self.command_queue = command_queue
        self.event_queue = event_queue
        self.ttl_seconds = max(0.01, float(ttl_seconds))
        self._deadline_monotonic = (
            float(deadline_monotonic)
            if deadline_monotonic is not None
            else time.monotonic() + self.ttl_seconds
        )
        self.expires_at = expires_at or (_utcnow() + timedelta(seconds=self.ttl_seconds))
        self.headless = bool(headless)
        # ``headless`` is the requested posture supplied by the manager.  A
        # mutation-capable session is deliberately forced visible in
        # ``_open_browser`` so boundary metadata must report the effective
        # browser posture, not the stale request flag.
        self._effective_headless = self.headless
        self.executable_path = executable_path
        self.allowlist = frozenset(allowlist)
        # A PREFILL candidate start receives a one-row host scope.  When the
        # scope is locked, the worker must not widen it by implicitly adding a
        # redirected/current-page host during egress classification.
        self.allowlist_is_run_scoped = bool(allowlist_is_run_scoped)
        try:
            self._verified_origin = origin_for_url(url)
        except ValueError:
            self._verified_origin = ""
        self._egress_records: list[dict[str, object]] = []
        self._egress_fatal_reason = ""
        # Populated only from a directly root-bound shortlink document
        # redirect.  It is per worker and disappears with this one row.
        self._source_resolution_bound_origins: set[str] = set()
        self.navigation_timeout_ms = int(navigation_timeout_ms)
        self.playwright_factory = playwright_factory
        self.captcha_provider_hosts = frozenset(captcha_provider_hosts)
        self.resumable_on_expiry = bool(resumable_on_expiry)
        self.expiry_preservation_seconds = max(
            0.01,
            float(expiry_preservation_seconds),
        )
        self._resume_until_monotonic = (
            self._deadline_monotonic + self.expiry_preservation_seconds
        )
        self.resume_until = self.expires_at + timedelta(
            seconds=self.expiry_preservation_seconds
        )
        self.egress_impact_classification_enabled = bool(
            egress_impact_classification_enabled
        )
        # The executor contains no Playwright handle. It is invoked only by
        # this owner thread in response to typed queue commands.
        self.journey_executor = journey_executor
        self.cleanup_grace_seconds = max(0.01, float(cleanup_grace_seconds))

        self.owner_thread_id: int | None = None
        self.thread_id: int | None = None
        self._operation_log: list[tuple[str, int]] = []
        self._teardown_observations: dict[str, object] = {}
        self._runtime: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._state = SessionState.OPENING
        self._state_before_expiry = SessionState.ACTIVE
        self._stop_requested = False
        self._cleanup_complete = False
        self._cleanup_escalated = False
        self._cleanup_lock = threading.Lock()
        self._last_scan = 0.0
        self._manifest: dict[str, object] = {}
        self._journey_result: dict[str, object] = {}
        self._journey_started = False
        self._tracing_started = False
        self._injected_human_required = False
        self._injected_human_reason = ""
        self._pages: list[Any] = []
        self._human_required_sticky = False
        self._human_required_reason = ""
        self._human_boundary_resume_allowed = True
        self._human_boundary_kind = "human_review"
        self._teardown_failures: list[str] = []
    @property
    def cleanup_complete(self) -> bool:
        return self._cleanup_complete

    @property
    def cleanup_escalated(self) -> bool:
        """Whether bounded owner-thread cleanup gave up with resources unresolved."""

        return self._cleanup_escalated

    def diagnostics(self) -> SessionDiagnostics:
        """Return immutable evidence; no Playwright object escapes."""

        return SessionDiagnostics(
            session_id=self.session_id,
            owner_thread_id=self.owner_thread_id,
            worker_alive=self.is_alive(),
            operation_log=tuple(self._operation_log),
            teardown_failures=tuple(self._teardown_failures),
            teardown_complete=self._cleanup_complete,
            page_token=id(self._page) if self._page is not None else None,
            page_count=len(self._pages),
        )

    def _assert_owner(self, operation: str) -> None:
        current = threading.get_ident()
        if self.owner_thread_id != current:
            raise RuntimeError(
                f"Playwright operation {operation!r} attempted outside owner thread"
            )
        self._operation_log.append((operation, current))

    def _call(self, operation: str, function: Callable[[], Any]) -> Any:
        self._assert_owner(operation)
        return function()

    def _emit(
        self,
        event: SessionEventType,
        *,
        state: SessionState | None = None,
        reason: str = "",
        payload: Mapping[str, object] | None = None,
    ) -> None:
        self.event_queue.put(
            SessionEvent(
                event=event,
                session_id=self.session_id,
                state=state,
                reason=reason,
                payload=dict(payload or {}),
                occurred_at=_utcnow(),
            )
        )

    def _set_state(self, state: SessionState, reason: str = "") -> None:
        if self._state == state and not reason:
            return
        self._state = state
        payload: dict[str, object] = {}
        if state is SessionState.HUMAN_REQUIRED:
            # The boundary is a durable, scalar handoff envelope.  It is
            # emitted with the state event so the manager can retain it even
            # when the original journey result is NEEDS_USER/NEEDS_OA.
            payload["human_boundary"] = self._human_boundary_payload(reason)
        else:
            # Clear the last active envelope once a human continuation has
            # genuinely resumed the journey.  The owner still owns all
            # browser handles; this is only the serialisable manager view.
            payload["human_boundary"] = {}
        self._emit(
            SessionEventType.STATE_CHANGED,
            state=state,
            reason=reason,
            payload=payload,
        )

    def _human_boundary_payload(self, reason: str = "") -> dict[str, object]:
        """Build the typed resumable human-boundary contract.

        Human-only questions and anti-bot/authentication challenges must not
        look like a generic ACTIVE/FAILED result.  Every envelope binds the
        exact application/session, exposes the TTL and explicit actions, and
        states that Continue never performs the final submit action.
        """

        boundary_reason = str(reason or self._human_required_reason or "human_required")
        return {
            "session_id": self.session_id,
            "application_id": self.application_id,
            "kind": self._human_boundary_kind or "human_review",
            "reason": boundary_reason,
            "status": "awaiting_human",
            "expires_at": self.expires_at.isoformat(),
            "can_continue": bool(self._human_boundary_resume_allowed),
            "can_cancel": True,
            "requires_visible": bool(self._effective_headless),
            "visibility": "headless" if self._effective_headless else "headed",
            "no_auto_submit": True,
            "resumable": bool(self._human_boundary_resume_allowed),
        }

    @staticmethod
    def _human_boundary_kind_for(
        state_text: str,
        blocked_reasons: object,
        reason: str,
    ) -> str:
        """Classify a boundary without trusting free-form page prose."""

        values = [str(reason or "").casefold()]
        if isinstance(blocked_reasons, (list, tuple, set, frozenset)):
            values.extend(str(item).casefold() for item in blocked_reasons)
        text = " ".join(values)
        if any(token in text for token in ("captcha", "human_verification", "human boundary")):
            return "captcha"
        if any(token in text for token in ("mfa", "multi-factor", "two-factor", "otp", "authenticator", "account_password")):
            return "authentication"
        if any(token in text for token in ("assessment", "online assessment", "needs_oa")):
            return "assessment"
        if any(token in text for token in ("legal", "attestation", "sponsorship", "work_authorisation")):
            return "legal"
        if any(token in text for token in ("demographic", "sensitive")):
            return "demographic"
        if any(token in text for token in ("risk", "unknown", "required_answer", "mapping", "insufficient")) or state_text == "BLOCKED":
            return "risk_review"
        return "human_review"

    @staticmethod
    def _human_boundary_reason_for(kind: str, reason: str) -> str:
        """Give generic runner risk messages a specific human action label."""

        current = str(reason or "").strip()
        generic = {
            "",
            "approved mapping or human review is required",
            "automation evidence is insufficient",
            "human_required",
        }
        if current.casefold() not in generic:
            return current
        labels = {
            "captcha": "CAPTCHA or human verification requires explicit human action",
            "authentication": "MFA or authentication requires explicit human action",
            "assessment": "Online assessment requires explicit human action",
            "legal": "Legal attestation requires explicit human action",
            "demographic": "Sensitive demographic choice requires explicit human action",
            "risk_review": "Risk review requires explicit human action",
        }
        return labels.get(kind, "Human review is required")

    def _allowed_request(self, request_url: str) -> bool:
        if not request_url:
            return True
        lower = request_url.casefold()
        if lower.startswith(("about:", "blob:", "data:")):
            return True
        try:
            parsed = urlsplit(request_url)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        try:
            hostname = parsed.hostname
        except ValueError:
            return False
        return bool(hostname) and _host_allowed(hostname, self.allowlist)

    def _route(self, route: Any) -> None:
        """Route callback; Playwright invokes it on this worker's thread."""

        self._assert_owner("route.callback")
        request = getattr(route, "request", None)
        request = request() if callable(request) else request
        request_url = str(getattr(request, "url", "") or "")
        run_scoped = bool(getattr(self, "allowlist_is_run_scoped", False))
        method: object = ""
        resource_type = ""
        is_navigation_request: bool | None = None
        initiator = ""
        if not request_url:
            # An uninspectable route is not a candidate-bearing request we can
            # prove safe.  Refuse it without allowing a browser-side escape.
            decision = None
        else:
            # Playwright exposes ``Request.method`` as a scalar property.  A
            # missing/opaque test double (or a future API shape we cannot
            # inspect) must remain unknown; defaulting it to GET would turn a
            # candidate-bearing request into a falsely safe passive request.
            try:
                raw_method = getattr(request, "method", None)
                method = raw_method() if callable(raw_method) else raw_method
            except Exception:  # noqa: BLE001 - opaque method is fail-closed
                method = ""
            raw_payload = getattr(request, "post_data", "")
            payload = raw_payload() if callable(raw_payload) else raw_payload
            raw_headers = getattr(request, "headers", {})
            headers = raw_headers() if callable(raw_headers) else raw_headers
            raw_resource_type = getattr(request, "resource_type", "")
            resource_type = (
                raw_resource_type() if callable(raw_resource_type) else raw_resource_type
            )
            resource_type = str(resource_type or "").casefold()
            try:
                raw_navigation = getattr(request, "is_navigation_request", None)
                navigation_value = (
                    raw_navigation() if callable(raw_navigation) else raw_navigation
                )
                is_navigation_request = (
                    navigation_value if isinstance(navigation_value, bool) else None
                )
            except Exception:  # noqa: BLE001 - unknown navigation is fatal
                is_navigation_request = None
            initiator = ""
            frame = None
            try:
                raw_frame = getattr(request, "frame", None)
                frame = raw_frame() if callable(raw_frame) else raw_frame
                raw_frame_url = getattr(frame, "url", "") if frame is not None else ""
                initiator = str(
                    raw_frame_url() if callable(raw_frame_url) else raw_frame_url
                )
            except Exception:  # noqa: BLE001 - initiator is evidence, not authority
                initiator = ""
            redirect_chain = _request_redirect_chain(request)
            source_resolution = bool(
                getattr(self, "summary", {}).get("source_resolution")
            )
            if source_resolution and len(redirect_chain) >= 2:
                journey_executor = getattr(self, "journey_executor", None)
                recorder = getattr(journey_executor, "record_redirect_chain", None)
                if callable(recorder):
                    recorder(redirect_chain)
            else:
                journey_executor = getattr(self, "journey_executor", None)
            approved_hosts = set(self.allowlist)
            if not run_scoped:
                try:
                    parsed = urlsplit(self.url)
                    if parsed.hostname:
                        approved_hosts.add(parsed.hostname)
                except ValueError:
                    pass
            decision = classify_egress(
                request_url,
                str(method) if method is not None else "",
                payload,
                approved_hosts=approved_hosts,
                approved_origins={self._verified_origin} if self._verified_origin else None,
                headers=headers,
            )
            source_resolution_cross_origin_allowed = False
            apply_binding_active = False
            apply_document_authorized = False
            provider_runtime_asset_allowed = False
            provider_runtime_config_allowed = False
            provider_document_upload_allowed = False
            captcha_provider_allowed = False
            captcha_decision = None
            content_type = ""
            request_origin = ""
            if isinstance(headers, Mapping):
                content_type = str(
                    headers.get("content-type") or headers.get("Content-Type") or ""
                )
                request_origin = str(
                    headers.get("origin") or headers.get("Origin") or ""
                )
            page = getattr(self, "_page", None)
            try:
                raw_page_url = getattr(page, "url", "") if page is not None else ""
                current_page_url = str(
                    raw_page_url() if callable(raw_page_url) else raw_page_url
                )
            except Exception:  # noqa: BLE001 - absent page is fail-closed
                current_page_url = ""
            is_main_frame_navigation: bool | None
            if is_navigation_request is False:
                is_main_frame_navigation = False
            elif is_navigation_request is True:
                try:
                    raw_main_frame = (
                        getattr(page, "main_frame", None) if page is not None else None
                    )
                    main_frame = (
                        raw_main_frame()
                        if callable(raw_main_frame)
                        else raw_main_frame
                    )
                    is_main_frame_navigation = (
                        frame is main_frame
                        if frame is not None and main_frame is not None
                        else None
                    )
                except Exception:  # noqa: BLE001 - unknown main frame is blocked
                    is_main_frame_navigation = None
            else:
                is_main_frame_navigation = None
            source_navigation_recorder = getattr(
                journey_executor,
                "record_source_navigation",
                None,
            )
            if (
                source_resolution
                and is_main_frame_navigation is True
                and resource_type == "document"
                and str(method or "").casefold() in {"get", "head"}
                and callable(source_navigation_recorder)
            ):
                source_navigation_recorder(request_url)
            resolved_vendor = str(
                getattr(self, "summary", {}).get("provider") or ""
            ).casefold().strip()
            captcha_candidate_data = request_carries_candidate_data(
                url=request_url,
                payload=payload,
                headers=headers if isinstance(headers, Mapping) else None,
            )
            captcha_decision = authorize_captcha_request(
                url=request_url,
                method=str(method or ""),
                resource_type=resource_type,
                is_navigation_request=is_navigation_request,
                is_main_frame_navigation=is_main_frame_navigation,
                mode=str(getattr(self, "mode", "")),
                resolved_vendor=resolved_vendor,
                current_page_url=current_page_url,
                carries_candidate_data=captcha_candidate_data,
                allowance=getattr(
                    self,
                    "captcha_provider_hosts",
                    CAPTCHA_PROVIDER_HOSTS,
                ),
            )
            if captcha_decision.allowed and not run_scoped:
                decision = replace(
                    decision,
                    approved=True,
                    allowed=True,
                    fatal=False,
                    reason=captcha_decision.reason,
                )
                captcha_provider_allowed = True
            elif captcha_decision.defect:
                decision = replace(
                    decision,
                    approved=False,
                    allowed=False,
                    fatal=True,
                    reason=captcha_decision.reason,
                )
            if source_resolution:
                # A listing may link to an approved ATS host.  During the
                # read-only source-resolution journey, permit only the
                # destination document and passive assets on an exact trusted
                # provider host.  Candidate-bearing GETs, POSTs, opaque
                # resource types, and untrusted cross-origin requests remain
                # blocked by the normal egress policy.
                candidate_provider = trusted_provider_for_url(request_url)
                expected_provider = str(
                    self.summary.get("provider") or ""
                ).casefold().strip()
                if expected_provider in {"unknown", "generic", "other"} or (
                    expected_provider.endswith("_shortlink")
                ):
                    expected_provider = ""
                apply_binding_active = bool(
                    getattr(journey_executor, "apply_hop_active", False)
                )
                apply_authorizer = getattr(
                    journey_executor,
                    "authorizes_apply_document_request",
                    None,
                )
                if apply_binding_active and callable(apply_authorizer):
                    apply_document_authorized = bool(
                        apply_authorizer(request_url, redirect_chain)
                    )
                trusted_provider_match = bool(
                    candidate_provider
                    and (
                        not expected_provider
                        or candidate_provider == expected_provider
                    )
                )
                bound_shortlink_document = bool(
                    len(redirect_chain) == 2
                    and _bound_shortlink_source(self.url)
                    and redirect_chain[0]
                    == _request_url_without_fragment(self.url)
                    and redirect_chain[-1]
                    == _request_url_without_fragment(request_url)
                    and str(method or "").casefold() in {"get", "head"}
                    and decision.classification == "other"
                    and resource_type == "document"
                    and safe_public_navigation_url(request_url)
                )
                bound_redirect_origin = bool(
                    decision.origin
                    and decision.origin in self._source_resolution_bound_origins
                )
                if bound_shortlink_document:
                    decision = replace(
                        decision,
                        approved=True,
                        allowed=True,
                        fatal=False,
                        reason="source_resolution_bound_shortlink_document",
                    )
                    source_resolution_cross_origin_allowed = True
                    if decision.origin:
                        self._source_resolution_bound_origins.add(decision.origin)
                elif (
                    bound_redirect_origin
                    and str(method or "").casefold() in {"get", "head"}
                    and (
                        decision.classification == "passive_asset"
                        or (
                            decision.classification == "other"
                            and resource_type == "document"
                        )
                    )
                ):
                    decision = replace(
                        decision,
                        approved=True,
                        allowed=True,
                        fatal=False,
                        reason="source_resolution_bound_redirect_read_only",
                    )
                    source_resolution_cross_origin_allowed = True
                elif (
                    trusted_provider_match
                    and (not apply_binding_active or apply_document_authorized)
                    and str(method or "").casefold() in {"get", "head"}
                    and decision.classification == "other"
                    and resource_type == "document"
                ):
                    decision = replace(
                        decision,
                        approved=True,
                        allowed=True,
                        fatal=False,
                        reason="source_resolution_trusted_provider_document",
                    )
                    source_resolution_cross_origin_allowed = True
                if (
                    apply_binding_active
                    and resource_type == "document"
                    and str(method or "").casefold() in {"get", "head"}
                    and not apply_document_authorized
                ):
                    decision = replace(
                        decision,
                        approved=False,
                        allowed=False,
                        fatal=True,
                        reason="source_resolution_apply_navigation_not_bound",
                    )
                    source_resolution_cross_origin_allowed = False
                elif (
                    trusted_provider_match
                    and str(method or "").casefold() in {"get", "head"}
                    and decision.classification == "passive_asset"
                ):
                    decision = replace(
                        decision,
                        approved=True,
                        allowed=True,
                        fatal=False,
                        reason="source_resolution_trusted_provider_passive_asset",
                    )
                    source_resolution_cross_origin_allowed = True
            # Current Greenhouse public forms ship their React controls from
            # one exact provider-owned CDN. Without those GET-only passive
            # assets, the server-rendered inputs appear but every custom
            # dropdown is inert. This exception never authorizes form data,
            # mutations, arbitrary CDN hosts, or third-party telemetry.
            expected_provider = str(
                getattr(self, "summary", {}).get("provider") or ""
            ).casefold().strip()
            try:
                runtime_asset_url = urlsplit(request_url)
                runtime_asset_host = (runtime_asset_url.hostname or "").casefold().rstrip(".")
                runtime_asset_path = runtime_asset_url.path.casefold()
                runtime_asset_scheme = runtime_asset_url.scheme.casefold()
                runtime_asset_port = runtime_asset_url.port
                runtime_asset_query = tuple(
                    parse_qsl(runtime_asset_url.query, keep_blank_values=True)
                )
            except ValueError:
                runtime_asset_host = ""
                runtime_asset_path = ""
                runtime_asset_scheme = ""
                runtime_asset_port = None
                runtime_asset_query = ()
            greenhouse_locale_asset = bool(
                resource_type == "fetch"
                and runtime_asset_path.startswith("/locales/")
                and runtime_asset_path.endswith(".json")
                and decision.classification == "other"
            )
            if (
                expected_provider == "greenhouse"
                and not run_scoped
                and runtime_asset_host == "job-boards.cdn.greenhouse.io"
                and str(method or "").casefold() in {"get", "head"}
                and (
                    decision.classification == "passive_asset"
                    or greenhouse_locale_asset
                )
                and resource_type in {"script", "stylesheet", "font", "fetch"}
            ):
                decision = replace(
                    decision,
                    approved=True,
                    allowed=True,
                    fatal=False,
                    reason="greenhouse_runtime_passive_asset",
                )
                provider_runtime_asset_allowed = True
            # The current Greenhouse React form obtains its provider-owned
            # S3 field descriptors from the legacy boards origin at startup.
            # Without this read-only bootstrap the upload widget exists but
            # its uploadFile callback is undefined.  Bind the exception to the
            # exact HTTPS host/path, a GET fetch, the verified form origin,
            # and only the two supported document field names.
            presigned_field_values = tuple(
                value for key, value in runtime_asset_query if key == "fields[]"
            )
            greenhouse_presigned_bootstrap = bool(
                expected_provider == "greenhouse"
                and not run_scoped
                and runtime_asset_scheme == "https"
                and runtime_asset_port in {None, 443}
                and runtime_asset_host == "boards.greenhouse.io"
                and runtime_asset_path
                == "/uncacheable_attributes/presigned_fields"
                and str(method or "").casefold() == "get"
                and resource_type == "fetch"
                and runtime_asset_query
                and len(presigned_field_values) == len(runtime_asset_query)
                and set(presigned_field_values).issubset({"resume", "cover_letter"})
                and request_origin == self._verified_origin
            )
            if greenhouse_presigned_bootstrap:
                decision = replace(
                    decision,
                    classification="other",
                    approved=True,
                    allowed=True,
                    fatal=False,
                    reason="greenhouse_presigned_field_bootstrap",
                )
                provider_runtime_config_allowed = True
            # Greenhouse uploads an explicitly selected CV/cover letter to a
            # provider-owned pre-signed S3 form before the final application
            # submit. Authorize only the exact observed bucket, only while the
            # owner-thread journey exposes one active approved document, and
            # only when the multipart filename is bound to that document.
            active_upload = getattr(
                getattr(self, "journey_executor", None),
                "active_document_upload",
                None,
            )
            active_upload = active_upload if isinstance(active_upload, Mapping) else {}
            active_path = str(active_upload.get("path") or "")
            active_filename = os.path.basename(active_path.replace("\\", "/"))
            active_sha256 = str(active_upload.get("sha256") or "")
            active_kind = str(active_upload.get("kind") or "")
            payload_text = (
                payload.decode("latin-1", errors="replace")
                if isinstance(payload, bytes)
                else str(payload or "")
            )
            expected_filename_marker = f'filename="{active_filename}"'
            if (
                expected_provider == "greenhouse"
                and not run_scoped
                and runtime_asset_host == "grnhse-prod-jben-us-east-1.s3.amazonaws.com"
                and str(getattr(self, "mode", "")).casefold() == "prefill"
                and str(method or "").casefold() == "post"
                and resource_type == "xhr"
                and decision.classification == "data_bearing"
                and bool(active_upload.get("approved"))
                and active_kind in {"document.cv", "document.cover_letter"}
                and bool(re.fullmatch(r"[0-9a-f]{64}", active_sha256.casefold()))
                and bool(active_filename)
                and active_filename.casefold().endswith((".pdf", ".doc", ".docx", ".txt", ".rtf"))
                and expected_filename_marker in payload_text
                and content_type.casefold().startswith("multipart/form-data;")
                and request_origin == self._verified_origin
            ):
                decision = replace(
                    decision,
                    approved=True,
                    allowed=True,
                    fatal=False,
                    reason="greenhouse_bound_approved_document_upload",
                )
                provider_document_upload_allowed = True
            # Host allowlists are insufficient for browser egress: a second
            # port (or a different scheme) is a different canonical origin.
            # Passive third-party assets remain record-only; candidate data is
            # always a fatal refusal before delivery.
            if (
                decision.allowed
                and self._verified_origin
                and decision.origin != self._verified_origin
                and not source_resolution_cross_origin_allowed
                and not provider_runtime_asset_allowed
                and not provider_runtime_config_allowed
                and not provider_document_upload_allowed
                and not captcha_provider_allowed
            ):
                if decision.classification == "passive_asset":
                    decision = replace(
                        decision,
                        allowed=False,
                        fatal=False,
                        reason="passive_asset_cross_origin_record_only",
                    )
                else:
                    decision = replace(
                        decision,
                        allowed=False,
                        fatal=True,
                        reason="data_bearing_cross_origin" if decision.classification == "data_bearing" else "nonpassive_cross_origin",
                    )
            if (
                decision.allowed
                and str(getattr(self, "mode", "")).casefold()
                in {"inspect", "review", "dry_run"}
                and decision.classification == "data_bearing"
                # Host-policy header inspection is deliberately conservative;
                # compare URL/body/method evidence without ordinary browser
                # navigation headers so a benign GET is not mistaken for a
                # candidate-bearing mutation.
                and classify_request(request_url, str(method or ""), payload, {})
                == "data_bearing"
            ):
                # Read-only journeys must not allow a candidate-bearing
                # browser request even when it is same-origin.  This keeps
                # form mutation/data transmission at zero for INSPECT,
                # REVIEW, and DRY_RUN rather than relying on the page being
                # well behaved.
                decision = replace(
                    decision,
                    allowed=False,
                    fatal=True,
                    reason="read_only_data_bearing_request",
                )
        if decision is not None:
            if (
                apply_binding_active
                and resource_type == "document"
                and str(method or "").casefold() in {"get", "head"}
            ):
                recorder = getattr(
                    journey_executor,
                    "record_apply_egress_decision",
                    None,
                )
                if callable(recorder):
                    recorder(
                        reason=decision.reason,
                        allowed=decision.allowed,
                        fatal=decision.fatal,
                    )
            record = {
                "url": decision.url,
                "method": decision.method,
                "classification": decision.classification,
                "origin": decision.origin or "",
                "allowed": decision.allowed,
                "fatal": decision.fatal,
                "record_only": decision.record_only,
                "reason": decision.reason,
            }
            impact = None
            if getattr(self, "egress_impact_classification_enabled", False):
                try:
                    request_url_parts = urlsplit(decision.url)
                    request_host = (request_url_parts.hostname or "").casefold().rstrip(".")
                    request_path = request_url_parts.path or "/"
                except (TypeError, ValueError):
                    request_host = ""
                    request_path = ""
                try:
                    initiator_parts = urlsplit(initiator)
                    initiator_origin = origin_for_url(initiator)
                    safe_initiator = (
                        f"{initiator_origin}{initiator_parts.path or '/'}"
                        if initiator_origin
                        else ""
                    )
                except (TypeError, ValueError):
                    safe_initiator = ""
                # The enabled evidence boundary retains no full request URL:
                # it could contain candidate data in a query or fragment.
                record.pop("url", None)
                record.update(
                    {
                        "host": request_host,
                        "path": request_path,
                        "resource_type": resource_type,
                        "initiator": safe_initiator,
                        "is_navigation_request": is_navigation_request,
                    }
                )
                if not decision.allowed:
                    impact = classify_blocked_request_impact(
                        url=decision.url,
                        method=decision.method,
                        resource_type=resource_type,
                        is_navigation_request=is_navigation_request,
                        mode=self.mode,
                        policy_classification=decision.classification,
                        resolved_vendor=resolved_vendor,
                        current_page_url=current_page_url,
                        approved_hosts=approved_hosts,
                        captcha_provider_hosts=(
                            self._captcha_provider_hosts
                            if hasattr(self, "_captcha_provider_hosts")
                            else None
                        ),
                    )
                    record.update(
                        {
                            "impact": impact.impact.value,
                            "impact_reason": impact.reason,
                            "manifest_vendor": resolved_vendor,
                            "manifest_permission": (
                                impact.impact
                                is BlockedRequestImpact.FIRST_PARTY_SERVICE
                            ),
                            "delivery": "blocked",
                            "carries_candidate_data": impact.carries_candidate_data,
                            "candidate_data_kind": impact.candidate_data_kind or "",
                        }
                    )
                    if self.mode == RunMode.PREFILL.value:
                        canonical_service_path = canonical_first_party_service_evidence_path(
                            host=request_host,
                            path=request_path,
                            resource_type=resource_type,
                        )
                        if canonical_service_path is not None:
                            # Service-host paths are browser/provider-controlled.
                            # Preserve only an exact measured endpoint; a rejected
                            # path must not cross the PREFILL REQUEST event boundary.
                            record["path"] = canonical_service_path
                    if (
                        impact.impact is not BlockedRequestImpact.FIRST_PARTY_SERVICE
                        and impact.consequence.value != "unknown"
                    ):
                        record["consequence"] = impact.consequence.value
                elif captcha_provider_allowed and captcha_decision is not None:
                    record.update(
                        {
                            "captcha_provider": True,
                            "captcha_permission": True,
                            "captcha_defect": False,
                            "delivery": "permitted",
                            "carries_candidate_data": False,
                        }
                    )
                if captcha_decision is not None and captcha_decision.defect:
                    record.update(
                        {
                            "captcha_provider": True,
                            "captcha_permission": False,
                            "captcha_defect": True,
                            "delivery": "blocked",
                            "carries_candidate_data": True,
                        }
                    )
            self._egress_records.append(record)
            if (
                impact is not None
                and impact.impact is BlockedRequestImpact.FATAL
            ) or (impact is None and decision.fatal):
                self._egress_fatal_reason = decision.reason
            event_queue = getattr(self, "event_queue", None)
            if event_queue is not None:
                self._emit(SessionEventType.REQUEST, reason=decision.reason, payload={"egress": record})
            if decision.allowed:
                continue_route = getattr(route, "continue_", None)
                if continue_route is not None:
                    continue_route()
                return
        elif getattr(self, "egress_impact_classification_enabled", False):
            # An opaque route was already refused above. Retain a synthetic,
            # query-free fatal record so missing Playwright metadata cannot
            # disappear before consequence reconciliation.
            record = {
                "method": str(method or ""),
                "classification": "unknown",
                "origin": "",
                "allowed": False,
                "fatal": True,
                "record_only": False,
                "reason": "invalid_request_url",
                "host": "",
                "path": "",
                "resource_type": resource_type,
                "initiator": initiator,
                "is_navigation_request": is_navigation_request,
                "impact": BlockedRequestImpact.FATAL.value,
                "impact_reason": "invalid_request_url",
                "manifest_vendor": "",
                "manifest_permission": False,
                "delivery": "blocked",
                "carries_candidate_data": False,
                "candidate_data_kind": "",
            }
            self._egress_records.append(record)
            self._egress_fatal_reason = "invalid_request_url"
            event_queue = getattr(self, "event_queue", None)
            if event_queue is not None:
                self._emit(
                    SessionEventType.REQUEST,
                    reason="invalid_request_url",
                    payload={"egress": record},
                )
        abort = getattr(route, "abort", None)
        if abort is not None:
            abort("blockedbyclient")

    def _register_page(self, page: Any) -> None:
        self._assert_owner("context.page")
        if any(existing is page for existing in self._pages):
            return
        self._pages.append(page)
        on = getattr(page, "on", None)
        if on is not None:
            self._call(
                "page.on",
                lambda: on("load", lambda: self._on_page_event(page)),
            )

    def _on_new_page(self, page: Any) -> None:
        """Context page/popup callback; Playwright invokes it on the owner."""

        self._assert_owner("context.page")
        self._register_page(page)

    def _on_page_event(self, page: Any) -> None:
        self._assert_owner("page.event")
        # Scanning itself remains bounded in _pump; this callback only makes
        # the page part of the owner-thread lifecycle and records evidence.
        if not any(existing is page for existing in self._pages):
            self._register_page(page)

    def _scan_human_boundary(self, *, allow_clear: bool = False) -> tuple[bool, str]:
        pages = list(self._pages)
        if not pages and self._page is not None:
            pages = [self._page]
        if self._injected_human_required:
            reason = self._injected_human_reason or "captcha_injected"
            self._human_boundary_kind = self._human_boundary_kind_for(
                "HUMAN_REQUIRED", (), reason
            )
            reason = self._human_boundary_reason_for(
                self._human_boundary_kind, reason
            )
            self._human_required_reason = reason
            self._set_state(SessionState.HUMAN_REQUIRED, reason)
            return True, reason
        found = False
        reason = ""
        try:
            for page in pages:
                frames = getattr(page, "frames", None)
                frames = frames() if callable(frames) else frames
                targets = list(frames or [])
                if not targets:
                    evaluate = getattr(page, "evaluate", None)
                    if evaluate is None:
                        continue
                    targets = [("page", evaluate)]
                for frame in targets:
                    if isinstance(frame, tuple):
                        target_name, evaluator = frame
                    else:
                        target_name = "frame.evaluate"
                        evaluator = getattr(frame, "evaluate", None)
                    if evaluator is None:
                        continue
                    result = self._call(
                        (
                            "page.evaluate.human_boundary"
                            if target_name == "page"
                            else "frame.evaluate.human_boundary"
                        ),
                        lambda evaluator=evaluator: evaluator(_CAPTCHA_SCRIPT),
                    )
                    if isinstance(result, Mapping):
                        current_found = bool(result.get("captcha"))
                        current_reason = str(result.get("reason") or "captcha_detected")
                    else:
                        current_found = bool(result)
                        current_reason = "captcha_detected" if current_found else ""
                    if current_found:
                        found = True
                        reason = current_reason
                        break
                if found:
                    break
        except Exception as exc:  # noqa: BLE001 - scan failure is fail-closed
            failure = f"human-boundary scan failed: {type(exc).__name__}: {exc}"
            self._set_state(SessionState.FAILED, failure)
            self._stop_requested = True
            return True, "scan_failed"
        if found:
            self._human_boundary_kind = self._human_boundary_kind_for(
                "HUMAN_REQUIRED", (), reason
            )
            reason = self._human_boundary_reason_for(
                self._human_boundary_kind, reason
            )
            self._human_required_sticky = True
            self._human_required_reason = reason
            self._set_state(SessionState.HUMAN_REQUIRED, reason)
            return True, reason
        if self._human_required_sticky:
            if allow_clear:
                self._human_required_sticky = False
                self._human_required_reason = ""
                return False, ""
            return True, self._human_required_reason or "human_required"
        return False, ""

    def _open_browser(self) -> None:
        if sync_playwright is None:
            raise RuntimeError("Playwright is unavailable")
        runtime_factory_fn = self.playwright_factory or sync_playwright
        if runtime_factory_fn is None:
            raise RuntimeError("Playwright is unavailable")
        runtime_factory = self._call("playwright.factory", runtime_factory_fn)
        self._runtime = self._call("playwright.start", runtime_factory.start)
        # Persistent review/human-boundary sessions must be visible to the
        # user.  Automated DRY_RUN/INSPECT sessions retain their configured
        # headless posture; mutation-capable PREFILL/SUBMIT sessions are also
        # visible because their first human boundary may only be discovered
        # after navigation.
        source_resolution = bool(self.summary.get("source_resolution"))
        self._effective_headless = self.headless and (
            source_resolution
            or self.mode
            not in {
                RunMode.REVIEW.value,
                RunMode.PREFILL.value,
                RunMode.SUBMIT.value,
            }
        )
        options: dict[str, object] = {"headless": self._effective_headless}
        if self.executable_path:
            options["executable_path"] = self.executable_path
        self._browser = self._call(
            "chromium.launch", lambda: self._runtime.chromium.launch(**options)
        )
        context_options: dict[str, object] = {
            "viewport": {"width": 1440, "height": 1000}
        }
        if bool(self.summary.get("sterile_resolution")):
            # Resolution escalation must have no profile, storage snapshot,
            # download surface, or service-worker persistence.  A new worker
            # owns this new context and destroys it in _cleanup regardless of
            # outcome.
            context_options.update(
                {
                    "storage_state": None,
                    "accept_downloads": False,
                    "service_workers": "block",
                }
            )
        self._context = self._call(
            "browser.new_context",
            lambda: self._browser.new_context(**context_options),
        )
        if bool(self.summary.get("sterile_resolution")):
            cookies = getattr(self._context, "cookies", None)
            if not callable(cookies):
                raise RuntimeError("sterile resolution context cannot prove cookie state")
            initial_cookies = self._call("context.cookies.sterile", cookies)
            if initial_cookies:
                raise RuntimeError("sterile resolution context unexpectedly contains cookies")
        trace_path = str(self.summary.get("trace_path") or "")
        tracing = getattr(self._context, "tracing", None)
        if trace_path and tracing is not None:
            try:
                parent = os.path.dirname(trace_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                self._call(
                    "context.tracing.start",
                    lambda: tracing.start(screenshots=True, snapshots=True, sources=True),
                )
                self._tracing_started = True
            except Exception:  # noqa: BLE001 - tracing is advisory
                self._tracing_started = False
        context_on = getattr(self._context, "on", None)
        if context_on is not None:
            self._call(
                "context.on:page",
                lambda: context_on("page", self._on_new_page),
            )
        context_route = getattr(self._context, "route", None)
        if context_route is not None:
            self._call(
                "context.route",
                lambda: context_route("**/*", self._route),
            )
        self._page = self._call("context.new_page", self._context.new_page)
        self._register_page(self._page)
        page_on = getattr(self._page, "on", None)
        response_recorder = getattr(
            self.journey_executor, "record_document_response", None
        )
        if callable(page_on) and callable(response_recorder):
            self._call(
                "page.on:response",
                lambda: page_on("response", response_recorder),
            )
        if context_route is None:
            route = getattr(self._page, "route", None)
            if route is not None:
                self._call("page.route", lambda: route("**/*", self._route))
        if not self._allowed_request(self.url):
            raise RuntimeError(f"navigation blocked by local egress policy: {self.url}")
        goto = getattr(self._page, "goto", None)
        if goto is not None:
            timeout_ms = min(
                self.navigation_timeout_ms,
                max(1, int(self.ttl_seconds * 1000)),
            )
            response = self._call(
                "page.goto",
                lambda: goto(self.url, wait_until="domcontentloaded", timeout=timeout_ms),
            )
            recorder = getattr(self.journey_executor, "record_document_response", None)
            if callable(recorder):
                recorder(response)
        self._emit(SessionEventType.WORKER_READY)
        self._scan_human_boundary()
        if self._state is SessionState.OPENING:
            self._set_state(SessionState.ACTIVE)

    def _authoritative_manifest(
        self,
        candidate: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Build a final-review manifest from owner-thread evidence.

        Journey-provided fields are treated as advisory only.  When a provider
        has produced a concrete ``SubmissionTarget`` we derive the load-bearing
        target/form fields from that immutable object and bind the manifest to
        the current page URL and session expiry.  A direct Navigator session
        without a target therefore remains visibly reviewable but cannot be
        promoted to a confirmable FINAL_REVIEW state.
        """

        supplied = dict(candidate or {})
        summary = dict(self.summary)
        executor = self.journey_executor
        target = getattr(executor, "final_target", None) if executor is not None else None
        target_binding: Mapping[str, object] = {}
        if target is not None:
            binder = getattr(executor, "_target_binding", None)
            if callable(binder):
                try:
                    bound = binder(target)
                    if isinstance(bound, Mapping):
                        target_binding = dict(bound)
                except Exception:  # noqa: BLE001 - stale target remains unconfirmable
                    target_binding = {}

        page = getattr(self, "_page", None)
        current_url = str(getattr(page, "url", "") or getattr(self, "url", ""))
        title = ""
        title_fn = getattr(page, "title", None)
        if title_fn is not None:
            try:
                title = str(title_fn() or "")
            except Exception:  # noqa: BLE001 - title is advisory evidence
                title = ""

        def value(*keys: str, default: object = "") -> object:
            for mapping in (target_binding, supplied, summary):
                for key in keys:
                    item = mapping.get(key)
                    if item is not None and not isinstance(item, (Mapping, list, tuple, set, frozenset)):
                        text = str(item).strip()
                        if text:
                            return item
            return default

        if target is not None:
            provider = str(getattr(target, "provider", "") or value("provider"))
            employer = str(getattr(target, "employer", "") or value("employer", "company"))
            role = str(getattr(target, "role", "") or value("role", "role_title"))
            requisition = str(
                getattr(target, "requisition", "")
                or value("requisition", "requisition_id", "job_id")
            )
            form_action = str(getattr(target, "form_action", "") or "")
            method = str(getattr(target, "method", "") or "")
            control_fingerprint = str(getattr(target, "control_fingerprint", "") or "")
            expected_receipt_url = ""
            target_evidence = getattr(target, "evidence", {})
            if isinstance(target_evidence, Mapping):
                expected_receipt_url = str(
                    target_evidence.get("expected_receipt_url")
                    or target_evidence.get("expected_final_url")
                    or ""
                ).strip()
            expected_receipt_url = expected_receipt_url or form_action
        else:
            provider = str(value("provider", "resolved_ats_type"))
            employer = str(value("employer", "company"))
            role = str(value("role", "role_title"))
            requisition = str(value("requisition", "requisition_id", "job_id"))
            form_action = str(value("form_action", "action"))
            method = str(value("method", "form_method"))
            control_fingerprint = str(value("control_fingerprint", "form_fingerprint"))
            expected_receipt_url = str(
                value("expected_receipt_url", "expected_final_url")
            )

        manifest = {
            "application_id": self.application_id,
            "employer": employer,
            "role": role,
            "requisition": requisition,
            "provider": provider,
            "mode": self.mode,
            "url": current_url,
            # Keep a provider-supplied final URL when one exists so the
            # current-page check can detect a navigation that happened after
            # review.  A missing value is filled from the owner-thread page.
            "final_url": str(value("final_url", "url") or current_url),
            "title": title,
            "target_fingerprint": str(
                target_binding.get("target_fingerprint")
                or value("target_fingerprint")
                or ""
            ),
            "control_fingerprint": control_fingerprint,
            "form_fingerprint": control_fingerprint,
            "form_action": form_action,
            "method": method.upper() if method else "",
            "form_method": method.upper() if method else "",
            "expected_receipt_url": expected_receipt_url,
            "expected_final_url": expected_receipt_url,
            "expires_at": self.expires_at.isoformat(),
            "submission": str(supplied.get("submission") or "not_clicked"),
        }
        # Preserve non-load-bearing provider evidence (step names, frame and
        # selector context) for the UI without allowing it to override the
        # authoritative fields above.
        for key in (
            "provider_step",
            "application_url",
            "destination",
            "form_identity",
            "root_selector",
            "control_selector",
            "page_id",
            "frame_url",
        ):
            item = value(key)
            if item:
                manifest[key] = item
        # These structured lists are produced by the owner-thread journey and
        # contain only durable IDs/hashes/approval metadata. Preserve empty
        # lists as explicit evidence: strict authority projection must be able
        # to distinguish "reviewed with none" from "input was omitted".
        for key in ("documents", "answers"):
            item = supplied.get(key)
            if isinstance(item, (list, tuple)):
                manifest[key] = [
                    dict(value) if isinstance(value, Mapping) else value
                    for value in item
                ]
        # Phase 17 evidence is non-authorizing and query-free. Preserve it
        # through the privileged FINAL_REVIEW rebuild so a tolerable visual
        # degradation remains visible in both the sealed manifest and the
        # handoff snapshot without affecting any target binding.
        if (
            getattr(self, "egress_impact_classification_enabled", False)
            and self.mode == RunMode.PREFILL.value
        ):
            owner_blocked_records = [
                record
                for record in self._egress_records
                if record.get("allowed") is False
            ]
            manifest["blocked_resources"] = [
                self._handoff_blocked_request(
                    record,
                    strict_first_party_paths=True,
                )
                for record in owner_blocked_records
            ]
            manifest["first_party_requests"] = [
                projected
                for record in owner_blocked_records
                if record.get("impact")
                == BlockedRequestImpact.FIRST_PARTY_SERVICE.value
                and (projected := self._first_party_request(record)) is not None
            ]
            manifest["captcha_requests"] = [
                projected
                for record in self._egress_records
                if (projected := self._captcha_request(record)) is not None
            ]
        else:
            legacy_blocked_resources = supplied.get("blocked_resources")
            if isinstance(legacy_blocked_resources, (list, tuple)):
                manifest["blocked_resources"] = [
                    self._handoff_blocked_request(record)
                    for record in legacy_blocked_resources
                    if isinstance(record, Mapping)
                ]
        for key in (
            "nonessential_resources_blocked",
            "fatal_resources_blocked",
            "egress_notice",
        ):
            item = supplied.get(key)
            if isinstance(item, (str, int)) and not isinstance(item, bool):
                manifest[key] = item
        return manifest

    def _final_manifest(self) -> dict[str, object]:
        self._assert_owner("page.manifest")
        candidate = self._journey_result.get("manifest")
        return self._authoritative_manifest(candidate if isinstance(candidate, Mapping) else None)

    def _manifest_is_current(self, manifest: Mapping[str, object]) -> bool:
        """Ensure a confirmation still refers to the page/form just inspected."""

        # A few embedders construct a worker through a test double or restore
        # one from an older serialized snapshot.  Treat a missing deadline as
        # immediately unconfirmable rather than raising while evaluating the
        # safety predicate.  Normal workers always initialise this field.
        deadline = getattr(self, "_deadline_monotonic", None)
        if deadline is None or time.monotonic() >= float(deadline):
            return False
        if _manifest_missing_fields(manifest, provider=str(manifest.get("provider") or "")):
            return False
        expiry_text = _manifest_text(manifest, "expires_at")
        try:
            expiry = datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= _utcnow():
                return False
        except (TypeError, ValueError):
            return False
        for key_group in (
            ("form_action", "action"),
            ("expected_receipt_url", "expected_final_url"),
        ):
            try:
                canonical_target_contract_url(_manifest_text(manifest, *key_group))
            except (TypeError, ValueError):
                return False
        page = getattr(self, "_page", None)
        current_url = str(getattr(page, "url", "") or getattr(self, "url", ""))
        try:
            return canonical_target_contract_url(str(manifest.get("final_url") or "")) == canonical_target_contract_url(current_url)
        except (TypeError, ValueError):
            return False

    def _bound_target_is_current(self) -> tuple[bool, str]:
        """Re-read the bound adapter target without activating its control.

        The final click path performs this same check again.  Keeping a
        non-clicking copy here is important because the durable authority is
        consumed by the request thread only *after* this owner-thread check.
        Adapters expose ``submission_target`` as a read-only reconstruction;
        a missing/changed target is therefore a refusal, never an invitation
        to discover a new control.
        """

        executor = self.journey_executor
        target = getattr(executor, "final_target", None) if executor is not None else None
        if target is None:
            # Direct Navigator sessions can be used for review without a
            # runner-owned target.  Their strict manifest checks still apply.
            return True, ""
        handle = getattr(executor, "final_handle", None)
        final_scope = getattr(executor, "final_scope", None)
        adapter = getattr(executor, "adapter", None)
        if handle is None or final_scope is None or adapter is None or self._page is None:
            return False, "Bound final target is incomplete"
        scope_factory = getattr(executor, "_scope", None)
        try:
            scope = scope_factory(self._page) if callable(scope_factory) else self._page
        except Exception as exc:  # noqa: BLE001 - an uninspectable target is unsafe
            return False, f"Bound final target scope could not be inspected: {type(exc).__name__}"
        if scope is not final_scope:
            return False, "Bound final target scope changed before activation"
        target_builder = getattr(adapter, "submission_target", None)
        if not callable(target_builder):
            return False, "Adapter cannot revalidate the bound final target"
        try:
            current = target_builder(scope, handle)
        except Exception as exc:  # noqa: BLE001 - target inspection is fail-closed
            return False, f"Bound final target could not be revalidated: {type(exc).__name__}"
        if current is None or current != target:
            return False, "Bound final target changed before activation"
        return True, ""

    def _preflight_human_boundary(self) -> tuple[bool, str]:
        """Run both generic CAPTCHA and adapter-specific human scans."""

        found, reason = self._scan_human_boundary()
        if found:
            return True, reason or "human_required"
        executor = self.journey_executor
        detector = getattr(executor, "_boundary", None) if executor is not None else None
        if not callable(detector) or self._page is None:
            return False, ""
        try:
            result = detector(self._page)
        except Exception as exc:  # noqa: BLE001 - detector failure is a boundary
            result = (True, f"human-boundary-scan-failed:{type(exc).__name__}")
        if isinstance(result, tuple):
            found = bool(result[0]) if result else False
            reason = str(result[1] or "human_required") if len(result) > 1 else "human_required"
        else:
            found = bool(result)
            reason = "human_required"
        if not found:
            return False, ""
        self._human_boundary_kind = self._human_boundary_kind_for(
            "HUMAN_REQUIRED", (), reason
        )
        reason = self._human_boundary_reason_for(self._human_boundary_kind, reason)
        self._human_required_sticky = True
        self._human_required_reason = reason
        self._set_state(SessionState.HUMAN_REQUIRED, reason)
        return True, reason

    def _preflight_refusal(self, reason: str, *, human: bool = False) -> None:
        """Emit a scalar refusal event without invoking the click callback."""

        state = self._state
        if human and state is not SessionState.FAILED:
            state = SessionState.HUMAN_REQUIRED
            if self._state is not state:
                self._set_state(state, reason)
        self._emit(
            SessionEventType.REQUEST,
            state=state,
            reason=reason,
            payload={
                "preflight": "blocked",
                "preflight_passed": False,
                "submission_blocked": True,
                "manifest": dict(self._manifest),
                "human_boundary": (
                    self._human_boundary_payload(reason) if human else {}
                ),
            },
        )

    def _preflight_submission(self) -> None:
        """Validate the final binding on the owner thread without clicking.

        This command is intended to run immediately before a request thread
        consumes a durable single-use submission authority.  It never calls
        ``submit`` and never changes the final control.  A CAPTCHA, adapter
        human boundary, stale target, changed control, malformed manifest, or
        validator exception is reported as a fail-closed refusal.

        A runner-owned executor may implement the optional
        ``preflight_submission(page)`` hook for provider-specific read-only
        checks.  It may return ``{"state": "READY", "preflight_passed":
        True}``, a mapping with a fresh manifest, or a human/failure state.
        """

        self._assert_owner("journey.preflight_submission")
        if self._state is not SessionState.FINAL_REVIEW:
            self._preflight_refusal(
                self._human_required_reason
                if self._state is SessionState.HUMAN_REQUIRED
                else "Submission preflight requires FINAL_REVIEW",
                human=self._state is SessionState.HUMAN_REQUIRED,
            )
            return

        found, reason = self._preflight_human_boundary()
        if found:
            self._preflight_refusal(reason or "human_required", human=True)
            return

        executor = self.journey_executor
        validator = (
            getattr(executor, "preflight_submission", None)
            if executor is not None
            else None
        )
        validator_result: Mapping[str, object] | None = None
        if callable(validator):
            try:
                raw_result = validator(self._page)
            except Exception as exc:  # noqa: BLE001 - provider checks are fail-closed
                self._preflight_refusal(
                    f"Submission preflight failed: {type(exc).__name__}"
                )
                return
            if isinstance(raw_result, Mapping):
                validator_result = dict(raw_result)
                validator_state = str(
                    raw_result.get("state") or raw_result.get("status") or ""
                ).upper()
                if validator_state in {
                    "HUMAN_REQUIRED",
                    "NEEDS_USER",
                    "NEEDS_OA",
                }:
                    self._emit_journey_result(raw_result)
                    self._preflight_refusal(
                        str(raw_result.get("reason") or "human_required"),
                        human=True,
                    )
                    return
                if validator_state in {"FAILED", "BLOCKED", "UNKNOWN"} or raw_result.get(
                    "preflight_passed"
                ) is False:
                    self._preflight_refusal(
                        str(raw_result.get("reason") or "Submission preflight refused")
                    )
                    return
                if validator_state and validator_state not in {
                    "READY",
                    "OK",
                    "PREFLIGHT_PASSED",
                    "FINAL_REVIEW",
                } and raw_result.get("preflight_passed") is not True:
                    self._preflight_refusal(
                        str(raw_result.get("reason") or "Submission preflight refused")
                    )
                    return
            elif raw_result is False:
                self._preflight_refusal("Submission preflight refused")
                return

        target_ok, target_reason = self._bound_target_is_current()
        if not target_ok:
            self._preflight_refusal(target_reason)
            return

        candidate: Mapping[str, object] | None = None
        if validator_result is not None:
            raw_candidate = validator_result.get("manifest")
            if isinstance(raw_candidate, Mapping):
                candidate = raw_candidate
        if candidate is None:
            raw_candidate = self._journey_result.get("manifest")
            if isinstance(raw_candidate, Mapping):
                candidate = raw_candidate
        if candidate is None and self._manifest:
            candidate = self._manifest
        current_manifest = self._authoritative_manifest(
            candidate
        )
        missing = _manifest_missing_fields(
            current_manifest,
            provider=str(current_manifest.get("provider") or ""),
        )
        if missing or not self._manifest_is_current(current_manifest):
            self._preflight_refusal(
                "Submission preflight found an incomplete or stale final manifest: "
                + ", ".join(missing or ("target_changed",))
            )
            return
        reviewed_projection = _manifest_binding_projection(self._manifest)
        current_projection = _manifest_binding_projection(current_manifest)
        changed = tuple(
            key
            for key in _PREFLIGHT_BINDING_FIELDS
            if reviewed_projection.get(key) != current_projection.get(key)
        )
        if changed:
            self._preflight_refusal(
                "Submission preflight found changed final binding: "
                + ", ".join(changed)
            )
            return

        # Keep the browser-owned evidence fresh for the manager, while
        # preserving FINAL_REVIEW and never touching the submit callback.
        self._manifest = dict(current_manifest)
        self._emit(
            SessionEventType.REQUEST,
            state=SessionState.FINAL_REVIEW,
            payload={
                "preflight": "passed",
                "preflight_passed": True,
                "submission_blocked": False,
                "manifest": dict(current_manifest),
            },
        )

    @staticmethod
    def _handoff_blocked_request(
        record: Mapping[str, object],
        *,
        strict_first_party_paths: bool = False,
    ) -> dict[str, object]:
        """Return query-free blocked-request evidence safe for handoff/audit."""

        evidence = {
            key: record.get(key)
            for key in (
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
                "manifest_vendor",
                "manifest_permission",
                "delivery",
                "carries_candidate_data",
                "candidate_data_kind",
            )
        }
        initiator = str(evidence.get("initiator") or "")
        try:
            initiator_parts = urlsplit(initiator)
            initiator_origin = origin_for_url(initiator)
            evidence["initiator"] = (
                f"{initiator_origin}{initiator_parts.path or '/'}"
                if initiator_origin
                else ""
            )
        except (TypeError, ValueError):
            evidence["initiator"] = ""
        evidence["path"] = (
            str(evidence.get("path") or "").split("?", 1)[0].split("#", 1)[0]
        )
        if strict_first_party_paths:
            canonical_service_path = canonical_first_party_service_evidence_path(
                host=str(evidence.get("host") or ""),
                path=str(evidence.get("path") or ""),
                resource_type=str(evidence.get("resource_type") or ""),
            )
            if canonical_service_path is not None:
                evidence["path"] = canonical_service_path
        evidence["manifest_vendor"] = str(
            evidence.get("manifest_vendor") or ""
        ).casefold().strip()
        evidence["manifest_permission"] = (
            evidence.get("manifest_permission") is True
        )
        evidence["delivery"] = "blocked"
        evidence["carries_candidate_data"] = (
            evidence.get("carries_candidate_data") is True
        )
        candidate_data_kind = str(evidence.get("candidate_data_kind") or "").casefold()
        evidence["candidate_data_kind"] = (
            candidate_data_kind if candidate_data_kind in {"email", "location"} else ""
        )
        return evidence

    @staticmethod
    def _first_party_request(
        record: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Project one manifest match to its exact private evidence contract."""

        evidence = HeadedSessionWorker._handoff_blocked_request(
            record,
            strict_first_party_paths=True,
        )
        if not evidence.get("path"):
            return None
        return {
            "host": str(evidence.get("host") or "").casefold().rstrip("."),
            "path": str(evidence.get("path") or "").split("?", 1)[0].split("#", 1)[0],
            "method": str(evidence.get("method") or ""),
            "resource_type": str(evidence.get("resource_type") or ""),
            "manifest_vendor": str(
                evidence.get("manifest_vendor") or ""
            ).casefold().strip(),
            "manifest_permission": evidence.get("manifest_permission") is True,
            "delivery": "blocked",
            "carries_candidate_data": evidence.get("carries_candidate_data") is True,
            "candidate_data_kind": str(
                evidence.get("candidate_data_kind") or ""
            ),
        }

    def _captcha_request(
        self,
        record: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Project one permitted challenge request into query-free evidence."""

        if (
            record.get("allowed") is not True
            or record.get("captcha_provider") is not True
            or record.get("captcha_permission") is not True
            or record.get("delivery") != "permitted"
            or record.get("carries_candidate_data") is not False
        ):
            return None
        host = str(record.get("host") or "").casefold().rstrip(".")
        path = str(record.get("path") or "").split("?", 1)[0].split("#", 1)[0]
        method = str(record.get("method") or "").upper()
        resource_type = str(record.get("resource_type") or "").casefold()
        page = getattr(self, "_page", None)
        try:
            raw_page_url = getattr(page, "url", "") if page is not None else ""
            page_url = str(raw_page_url() if callable(raw_page_url) else raw_page_url)
        except Exception:  # noqa: BLE001 - evidence projection is fail-closed
            return None
        decision = authorize_captcha_request(
            url=f"https://{host}{path}",
            method=method,
            resource_type=resource_type,
            is_navigation_request=False,
            is_main_frame_navigation=False,
            mode=self.mode,
            resolved_vendor=str(self.summary.get("provider") or ""),
            current_page_url=page_url,
            carries_candidate_data=False,
            allowance=self.captcha_provider_hosts,
        )
        if not decision.allowed:
            return None
        return {
            "host": decision.host,
            "path": decision.path,
            "method": decision.method,
            "resource_type": decision.resource_type,
            "captcha_provider": True,
            "captcha_permission": True,
            "delivery": "permitted",
            "carries_candidate_data": False,
        }

    def _reconcile_prefill_egress(
        self,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """Annotate or stop PREFILL after retaining every blocked request.

        Route authorization has already happened.  This method cannot make a
        request reachable; it only controls the scalar journey/handoff result.
        Missing impact metadata is fatal by construction.
        """

        if (
            not getattr(self, "egress_impact_classification_enabled", False)
            or self.mode != RunMode.PREFILL.value
        ):
            return payload
        blocked = [
            self._handoff_blocked_request(record, strict_first_party_paths=True)
            for record in self._egress_records
            if record.get("allowed") is False
        ]
        first_party = [
            projected
            for record in self._egress_records
            if record.get("allowed") is False
            and record.get("impact") == BlockedRequestImpact.FIRST_PARTY_SERVICE.value
            and (projected := self._first_party_request(record)) is not None
        ]
        payload["first_party_requests"] = first_party
        captcha_requests = [
            projected
            for record in self._egress_records
            if (projected := self._captcha_request(record)) is not None
        ]
        payload["captcha_requests"] = captcha_requests
        manifest_value = payload.get("manifest")
        manifest = dict(manifest_value) if isinstance(manifest_value, Mapping) else {}
        manifest["first_party_requests"] = first_party
        manifest["captcha_requests"] = captcha_requests
        if not blocked:
            payload["manifest"] = manifest
            return payload
        tolerable = [
            record
            for record in blocked
            if record.get("impact") == BlockedRequestImpact.TOLERABLE.value
        ]
        fatal = [
            record
            for record in blocked
            if record.get("impact")
            not in {
                BlockedRequestImpact.TOLERABLE.value,
                BlockedRequestImpact.FIRST_PARTY_SERVICE.value,
            }
        ]
        payload["blocked_requests"] = blocked
        manifest["blocked_resources"] = blocked

        notices: list[str] = []
        if tolerable:
            count = len(tolerable)
            noun = "resource" if count == 1 else "resources"
            tolerable_notice = f"filled; {count} non-essential {noun} blocked"
            manifest["nonessential_resources_blocked"] = count
            notices.append(tolerable_notice)
        if fatal:
            count = len(fatal)
            noun = "request" if count == 1 else "requests"
            fatal_notice = (
                f"Filling stopped; {count} functional or unclassified {noun} blocked"
            )
            manifest["fatal_resources_blocked"] = count
            notices.insert(0, fatal_notice)
            payload["state"] = "NEEDS_USER"
            try:
                risk_level = int(payload.get("risk_level", 0))
            except (TypeError, ValueError):
                risk_level = 0
            payload["risk_level"] = max(3, risk_level)
            existing_reasons = payload.get("blocked_reasons")
            reasons = (
                tuple(str(value) for value in existing_reasons)
                if isinstance(existing_reasons, (list, tuple, set, frozenset))
                else ()
            )
            payload["blocked_reasons"] = tuple(
                dict.fromkeys((*reasons, "egress_functional_or_unknown_blocked"))
            )
        notice = "; ".join(notices)
        manifest["egress_notice"] = notice
        payload["manifest"] = manifest
        existing_reason = str(payload.get("reason") or "").strip()
        payload["reason"] = f"{existing_reason}; {notice}" if existing_reason else notice
        return payload

    def _emit_journey_result(self, result: Mapping[str, object] | None) -> None:
        """Publish a bounded scalar journey result from the owner thread."""

        payload = dict(result or {})
        payload = self._reconcile_prefill_egress(payload)
        state_text = str(payload.get("state") or "ACTIVE").upper()
        try:
            state = SessionState(state_text)
        except ValueError:
            state = SessionState.ACTIVE
        reason = str(payload.get("reason") or "")
        # NEEDS_USER/NEEDS_OA are journey-result vocabulary, not lifecycle
        # states.  Preserve that result for the runner/application state while
        # projecting the owner lifecycle into HUMAN_REQUIRED so the browser
        # and its Continue/Cancel controls remain alive.  Risk-4 BLOCKED
        # results are also retained as a resumable human boundary: status and
        # cancel remain available, while Continue only re-evaluates safely.
        boundary_result = state_text in {"HUMAN_REQUIRED", "NEEDS_USER", "NEEDS_OA"} or (
            state_text == "BLOCKED"
            and str(payload.get("risk_level") or "0").strip() in {"3", "4"}
        )
        # Continue is always an explicit, non-submitting re-evaluation.  Even
        # a risk-4/unknown result remains on the same owner so the user can
        # inspect fresh evidence or cancel; the command never bypasses the
        # adapter's guard or invokes submit().
        boundary_resume_allowed = bool(boundary_result)
        if boundary_result:
            self._human_boundary_kind = self._human_boundary_kind_for(
                state_text,
                payload.get("blocked_reasons"),
                reason,
            )
            reason = self._human_boundary_reason_for(
                self._human_boundary_kind,
                reason,
            )
            payload["reason"] = reason
            self._human_boundary_resume_allowed = boundary_resume_allowed
            self._human_required_reason = reason or "human_required"
            self._human_required_sticky = True
            payload["human_boundary_resume_allowed"] = boundary_resume_allowed
        manifest = payload.get("manifest")
        if state is SessionState.FINAL_REVIEW:
            # FINAL_REVIEW is a privileged state, so enrich and validate the
            # provider's result before exposing it to the manager/UI.  A
            # direct Navigator executor that has not produced an exact form
            # target remains ACTIVE/NEEDS_USER rather than yielding a false
            # confirmation affordance.
            authoritative = self._authoritative_manifest(
                manifest if isinstance(manifest, Mapping) else None
            )
            payload["manifest"] = authoritative
            self._manifest = dict(authoritative)
            missing = _manifest_missing_fields(
                authoritative,
                provider=str(authoritative.get("provider") or ""),
            )
            if missing or not self._manifest_is_current(authoritative):
                state = SessionState.ACTIVE
                payload["state"] = state.value
                reason = "Final manifest is incomplete or stale: " + ", ".join(missing or ("target_changed",))
                payload["reason"] = reason
        else:
            self._journey_result = payload
            if isinstance(manifest, Mapping):
                self._manifest = dict(manifest)
        if boundary_result:
            # Keep the provider's original result in JOURNEY_RESULT while
            # exposing a typed HUMAN_REQUIRED lifecycle state to the manager.
            self._set_state(SessionState.HUMAN_REQUIRED, self._human_required_reason)
            payload["human_boundary"] = self._human_boundary_payload(
                self._human_required_reason
            )
        elif state is SessionState.FINAL_REVIEW:
            self._set_state(SessionState.FINAL_REVIEW, reason)
        elif state is SessionState.CONFIRMED:
            self._set_state(SessionState.CONFIRMED, reason or "submission confirmed")
        elif state is SessionState.UNKNOWN:
            self._set_state(SessionState.UNKNOWN, reason or "submission outcome unknown")
        elif not self._state.terminal:
            self._set_state(SessionState.ACTIVE, reason)
        self._journey_result = payload
        screenshot_path = str(self.summary.get("screenshot_path") or "")
        if screenshot_path and self._page is not None:
            try:
                parent = os.path.dirname(screenshot_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                screenshot = getattr(self._page, "screenshot", None)
                if screenshot is not None:
                    self._call(
                        "page.screenshot",
                        lambda: screenshot(path=screenshot_path, full_page=True),
                    )
            except Exception:  # noqa: BLE001 - artifacts never alter journey truth
                pass
        self._emit(
            SessionEventType.JOURNEY_RESULT,
            # ``NEEDS_USER``/``NEEDS_OA``/risk BLOCKED are provider result
            # vocabulary, not lifecycle states.  The manager must not
            # overwrite the preceding HUMAN_REQUIRED state with the
            # compatibility ACTIVE fallback used for those result strings.
            state=SessionState.HUMAN_REQUIRED if boundary_result else state,
            reason=reason,
            payload=payload,
        )

    def _run_journey(self, *, resume: bool = False) -> None:
        self._assert_owner("journey.execute")
        executor = self.journey_executor
        if executor is None or self._page is None:
            self._emit_journey_result(
                {"state": "FAILED", "reason": "No owner-thread journey executor"}
            )
            self._set_state(SessionState.FAILED, "No owner-thread journey executor")
            return
        method_name = "resume" if resume and self._journey_started else "prepare"
        method = getattr(executor, method_name, None)
        if method is None:
            method = getattr(executor, "prepare", None)
        if method is None:
            self._emit_journey_result(
                {"state": "FAILED", "reason": "Journey executor lacks prepare()"}
            )
            self._set_state(SessionState.FAILED, "Journey executor lacks prepare()")
            return
        probe_setter = getattr(executor, "set_egress_fatal_probe", None)
        if callable(probe_setter):
            probe_setter(
                lambda: (
                    self._egress_fatal_reason
                    if getattr(self, "egress_impact_classification_enabled", False)
                    and self.mode == RunMode.PREFILL.value
                    else ""
                )
            )
        records_setter = getattr(executor, "set_egress_fatal_records", None)
        if callable(records_setter):
            records_setter(
                list(getattr(self, "_egress_records", []))
            )
        try:
            result = method(self._page)
            self._journey_started = True
            self._emit_journey_result(result if isinstance(result, Mapping) else {})
        except Exception as exc:  # noqa: BLE001 - journey failures are terminal evidence
            result = {
                "state": "FAILED",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            self._emit_journey_result(result)
            self._set_state(SessionState.FAILED, str(result["reason"]))

    def _confirm_submission(self, command: SessionCommand) -> None:
        self._assert_owner("journey.confirm_submission")
        if time.monotonic() >= float(getattr(self, "_deadline_monotonic", 0.0)):
            self._expire_session(
                "session TTL expired before submission confirmation"
            )
            return
        if self._state is not SessionState.FINAL_REVIEW:
            self._emit_journey_result(
                {"state": self._state.value, "reason": "Submission requires FINAL_REVIEW"}
            )
            return
        if not self._manifest_is_current(self._manifest):
            missing = _manifest_missing_fields(
                self._manifest,
                provider=str(self._manifest.get("provider") or ""),
            )
            reason = "Final manifest is incomplete or stale: " + ", ".join(
                missing or ("target_changed",)
            )
            self._emit_journey_result(
                {"state": "ACTIVE", "reason": reason, "manifest": dict(self._manifest)}
            )
            return
        confirmation_id = str(command.payload.get("confirmation_id") or "")
        if confirmation_id != self.application_id:
            # A stale or mismatched action-time confirmation is a refusal and
            # must never invoke the bound click callback.
            self._emit_journey_result(
                {
                    "state": "FINAL_REVIEW",
                    "reason": "Exact application confirmation mismatch",
                    # Preserve the already validated binding.  Rebuilding a
                    # manifest from a bare refusal payload would discard the
                    # target proof and incorrectly downgrade the session to
                    # ACTIVE, even though no page state changed.
                    "manifest": dict(self._manifest),
                }
            )
            return
        submit = getattr(self.journey_executor, "submit", None)
        if submit is None or self._page is None:
            self._emit_journey_result(
                {"state": "FAILED", "reason": "Journey executor lacks submit()"}
            )
            self._set_state(SessionState.FAILED, "Journey executor lacks submit()")
            self._stop_requested = True
            return
        # A direct/custom Navigator executor is review-only.  The production
        # runner installs both the durable intent binding and the owner-thread
        # authority CAS callback only after request-side preflight succeeds.
        # Without both, invoking arbitrary submit() code would bypass the
        # single-use authority service entirely.
        authority_gate = getattr(self.journey_executor, "before_click", None)
        submission_binding = getattr(
            self.journey_executor, "submission_binding", None
        )
        if not callable(authority_gate) or not isinstance(
            submission_binding, Mapping
        ) or not submission_binding:
            self._emit_journey_result(
                {
                    "state": "FINAL_REVIEW",
                    "reason": "Submission authority gate is not installed",
                    "manifest": dict(self._manifest),
                    "click_boundary_crossed": False,
                }
            )
            return
        try:
            result = submit(self._page, confirmation_id)
            payload = dict(result) if isinstance(result, Mapping) else {}
            state_text = str(payload.get("state") or "").upper()
            if not state_text:
                payload = {
                    "state": "UNKNOWN",
                    "reason": "Submission executor returned no outcome state",
                    "click_boundary_crossed": bool(
                        getattr(
                            self.journey_executor,
                            "_click_boundary_crossed",
                            False,
                        )
                    ),
                }
            elif state_text == "CONFIRMED" and payload.get(
                "click_boundary_crossed"
            ) is not True:
                payload = {
                    **payload,
                    "state": "UNKNOWN",
                    "reason": "Confirmation lacks the durable click boundary",
                    "click_boundary_crossed": bool(
                        payload.get("click_boundary_crossed")
                    ),
                }
            self._emit_journey_result(payload)
            if (
                str(payload.get("state") or "").upper() == "CONFIRMED"
                and payload.get("click_boundary_crossed") is True
                and self._state is SessionState.FINAL_REVIEW
            ):
                self._set_state(SessionState.CONFIRMED, "submission click completed")
            self._stop_requested = True
        except Exception as exc:  # noqa: BLE001 - post-click ambiguity is terminal
            payload = {
                "state": "UNKNOWN",
                "reason": f"Submission outcome unknown: {type(exc).__name__}: {exc}",
                "submission_unknown": True,
                "click_boundary_crossed": bool(
                    getattr(self.journey_executor, "_click_boundary_crossed", False)
                ),
            }
            evidence_payload = getattr(
                self.journey_executor, "receipt_evidence_payload", None
            )
            if callable(evidence_payload):
                try:
                    evidence = evidence_payload()
                except Exception:  # noqa: BLE001 - exception evidence is advisory
                    evidence = {}
                if isinstance(evidence, Mapping) and evidence:
                    payload["receipt_evidence"] = dict(evidence)
            self._emit_journey_result(payload)
            self._set_state(SessionState.UNKNOWN, str(payload["reason"]))
            self._stop_requested = True

    def _expire_session(self, reason: str) -> None:
        """Revoke session authority while retaining a bounded exact-page handoff."""

        if self._state is not SessionState.EXPIRED:
            if self._state in {
                SessionState.ACTIVE,
                SessionState.HUMAN_REQUIRED,
                SessionState.FINAL_REVIEW,
            }:
                self._state_before_expiry = self._state
            self._set_state(SessionState.EXPIRED, reason)
        if not self.resumable_on_expiry:
            self._stop_requested = True

    def _resume_expired_session(self, command: SessionCommand) -> None:
        """Issue a fresh finite TTL without replacing the preserved page/context."""

        if (
            not self.resumable_on_expiry
            or self._state is not SessionState.EXPIRED
            or time.monotonic() >= self._resume_until_monotonic
        ):
            self._emit(
                SessionEventType.REQUEST,
                state=self._state,
                reason="expired session is no longer resumable",
                payload={"command_refused": "resume_unavailable"},
            )
            return
        try:
            deadline = float(command.payload.get("deadline_monotonic") or 0.0)
            expires_at = datetime.fromisoformat(
                str(command.payload.get("expires_at") or "").replace("Z", "+00:00")
            )
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            deadline = 0.0
            expires_at = _utcnow()
        if deadline <= time.monotonic() or expires_at <= _utcnow():
            self._emit(
                SessionEventType.REQUEST,
                state=SessionState.EXPIRED,
                reason="fresh resume deadline is invalid",
                payload={"command_refused": "resume_deadline_invalid"},
            )
            return
        self._deadline_monotonic = deadline
        self.expires_at = expires_at
        self._resume_until_monotonic = deadline + self.expiry_preservation_seconds
        self.resume_until = expires_at + timedelta(
            seconds=self.expiry_preservation_seconds
        )
        restored = self._state_before_expiry
        if restored is SessionState.FINAL_REVIEW:
            # Expiry invalidates the reviewed manifest. The exact filled page
            # survives, but the user must request a fresh final review.
            self._manifest = {}
            restored = SessionState.HUMAN_REQUIRED
            self._human_required_reason = (
                "Expired handoff resumed on the preserved page; review again"
            )
        self._set_state(
            restored,
            (
                self._human_required_reason
                if restored is SessionState.HUMAN_REQUIRED
                else "expired handoff resumed on preserved page"
            ),
        )

    def _handle_command(self, command: SessionCommand) -> None:
        self._assert_owner(f"command.{command.command.value}")
        if str(command.session_id) != str(self.session_id):
            # A queue consumer must not be able to use a copied/stale command
            # to operate another browser owner.  Keep the session alive so a
            # legitimate caller can still close it, but record the refusal as
            # a scalar request event; no callback, page action, or click runs.
            self._emit(
                SessionEventType.REQUEST,
                state=self._state,
                reason="session_id mismatch; command refused",
                payload={
                    "command_refused": "session_id_mismatch",
                    "command": command.command.value,
                },
            )
            return
        if command.command is SessionCommandType.RESUME:
            self._resume_expired_session(command)
            return
        if (
            command.command in _DEADLINE_SENSITIVE_COMMANDS
            and time.monotonic() >= float(getattr(self, "_deadline_monotonic", 0.0))
        ):
            # Expiry is checked on the owner thread immediately before any
            # journey callback.  In particular CONFIRM and
            # CONFIRM_SUBMISSION cannot reach a durable callback/click after
            # the session deadline.
            self._expire_session("session TTL expired before command")
            self._emit(
                SessionEventType.REQUEST,
                state=SessionState.EXPIRED,
                reason="session TTL expired before command",
                payload={
                    "command_refused": "deadline_expired",
                    "command": command.command.value,
                },
            )
            return
        if command.command is SessionCommandType.RUN_JOURNEY:
            if self._state.terminal:
                return
            if self._state is SessionState.HUMAN_REQUIRED:
                return
            self._run_journey()
            return
        if command.command is SessionCommandType.PREFLIGHT_SUBMISSION:
            if not self._state.terminal:
                self._preflight_submission()
            return
        if command.command is SessionCommandType.CONFIRM_SUBMISSION:
            if not self._state.terminal:
                self._confirm_submission(command)
            return
        if command.command is SessionCommandType.CONTINUE:
            was_human = self._state is SessionState.HUMAN_REQUIRED
            if self._state in {SessionState.HUMAN_REQUIRED, SessionState.ACTIVE}:
                if (
                    was_human
                    and self._effective_headless
                    and self._human_boundary_resume_allowed
                ):
                    # A hidden PREFILL/SUBMIT boundary cannot be solved by an
                    # invisible user.  Keep the owner/session alive and make
                    # the refusal explicit; importantly, do not resume a
                    # journey or reach any mutation/click callback.
                    reason = (
                        "Visible browser is required before continuing this "
                        "human boundary; no submit was issued"
                    )
                    self._human_required_reason = reason
                    self._emit(
                        SessionEventType.REQUEST,
                        state=SessionState.HUMAN_REQUIRED,
                        reason=reason,
                        payload={
                            "command_refused": "visible_browser_required",
                            "human_boundary": self._human_boundary_payload(reason),
                        },
                    )
                    return
                self._injected_human_required = False
                self._injected_human_reason = ""
                found, _reason = self._scan_human_boundary(allow_clear=True)
                if self._state is SessionState.FAILED:
                    return
                if found:
                    return
                if (
                    was_human
                    and self._journey_started
                    and self._human_boundary_resume_allowed
                    and str(self._journey_result.get("state") or "").upper()
                    in {"HUMAN_REQUIRED", "NEEDS_USER", "NEEDS_OA", "BLOCKED"}
                ):
                    self._set_state(SessionState.ACTIVE)
                    self._run_journey(resume=True)
                elif (
                    was_human
                    and not self._journey_started
                    and bool(self.summary.get("source_resolution"))
                ):
                    # Source inspection may discover a CAPTCHA during the
                    # initial navigation, before ``prepare`` runs.  Continue
                    # re-enters the same owner thread/context and performs a
                    # read-only inspection; it never reaches submit/fill.
                    self._set_state(SessionState.ACTIVE)
                    self._run_journey(resume=False)
                else:
                    self._set_state(SessionState.ACTIVE)
            return
        if command.command in {SessionCommandType.CANCEL, SessionCommandType.CLOSE}:
            if not self._state.terminal:
                self._set_state(SessionState.CANCELLED, command.reason or "closed by user")
            self._stop_requested = True
            return
        if command.command is SessionCommandType.EXPIRE:
            if not self._state.terminal:
                self._expire_session(command.reason or "session TTL expired")
            return
        if command.command in {
            SessionCommandType.INJECT_HUMAN_REQUIRED,
            SessionCommandType.CAPTCHA,
        }:
            if not self._state.terminal:
                self._injected_human_required = True
                self._injected_human_reason = command.reason or "captcha_injected"
                self._human_boundary_kind = self._human_boundary_kind_for(
                    "HUMAN_REQUIRED", (), self._injected_human_reason
                )
                self._human_boundary_resume_allowed = True
                self._set_state(
                    SessionState.HUMAN_REQUIRED,
                    self._injected_human_reason,
                )
            return
        if command.command is SessionCommandType.SHUTDOWN:
            if not self._state.terminal:
                self._set_state(
                    SessionState.CANCELLED,
                    command.reason or "application shutdown",
                )
            self._stop_requested = True
            return
        if command.command is SessionCommandType.FINAL_MANIFEST:
            if self._state.terminal:
                return
            found, _reason = self._scan_human_boundary()
            if found:
                return
            self._manifest = self._final_manifest()
            missing = _manifest_missing_fields(
                self._manifest,
                provider=str(self._manifest.get("provider") or ""),
            )
            if missing or not self._manifest_is_current(self._manifest):
                reason = "Final manifest is incomplete or stale: " + ", ".join(
                    missing or ("target_changed",)
                )
                self._emit(
                    SessionEventType.REQUEST,
                    state=self._state,
                    reason=reason,
                    payload={
                        "manifest": dict(self._manifest),
                        "manifest_complete": False,
                    },
                )
                return
            self._set_state(SessionState.FINAL_REVIEW)
            self._emit(
                SessionEventType.REQUEST,
                state=SessionState.FINAL_REVIEW,
                payload={"manifest": dict(self._manifest)},
            )
            return
        if command.command is SessionCommandType.CONFIRM:
            if (
                self._state is SessionState.FINAL_REVIEW
                and command.payload.get("confirmed") is True
            ):
                if not self._manifest_is_current(self._manifest):
                    missing = _manifest_missing_fields(
                        self._manifest,
                        provider=str(self._manifest.get("provider") or ""),
                    )
                    self._emit_journey_result(
                        {
                            "state": "ACTIVE",
                            "reason": "Final manifest is incomplete or stale: "
                            + ", ".join(missing or ("target_changed",)),
                            "manifest": dict(self._manifest),
                        }
                    )
                    return
                self._set_state(
                    SessionState.CONFIRMED,
                    "final manifest confirmed; no submit click issued",
                )
                self._stop_requested = True

    def _pump(self) -> None:
        """Pump sync Playwright while accepting bounded queue commands."""

        while True:
            try:
                command = self.command_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_command(command)
        if self._page is not None:
            wait_for_timeout = getattr(self._page, "wait_for_timeout", None)
            if wait_for_timeout is not None:
                self._call("page.wait_for_timeout", lambda: wait_for_timeout(50))
        now = time.monotonic()
        if now - self._last_scan >= 0.25 and not self._state.terminal:
            self._last_scan = now
            self._scan_human_boundary()

    def _escalate_cleanup(self, reason: str) -> None:
        """Stop waiting while preserving truthful evidence of incomplete teardown."""

        self._cleanup_escalated = True
        self._teardown_observations["cleanup_escalated"] = True
        self._teardown_observations["cleanup_escalation_reason"] = reason
        self._set_state(SessionState.FAILED, reason)
        self._emit(
            SessionEventType.ERROR,
            state=SessionState.FAILED,
            reason=reason,
            payload={
                "cleanup_escalated": True,
                "teardown_failures": tuple(self._teardown_failures),
            },
        )

    def _cleanup(self) -> None:
        with self._cleanup_lock:
            if self._cleanup_complete:
                return
            # This method is only called from run(), which is the owner thread.
            self._assert_owner("cleanup.begin")
            failures: list[str] = []
            if self._tracing_started and self._context is not None:
                trace_path = str(self.summary.get("trace_path") or "")
                try:
                    tracing = getattr(self._context, "tracing", None)
                    stop = getattr(tracing, "stop", None)
                    if stop is not None:
                        self._call(
                            "context.tracing.stop",
                            lambda: stop(path=trace_path),
                        )
                except Exception as exc:  # noqa: BLE001
                    failures.append(
                        f"context.tracing.stop: {type(exc).__name__}: {exc}"
                    )
                finally:
                    self._tracing_started = False
            for name, handle in (
                ("page.close", self._page),
                ("context.close", self._context),
                ("browser.close", self._browser),
            ):
                if handle is None:
                    continue
                try:
                    close = getattr(handle, "close", None)
                    if close is not None:
                        self._call(name, close)
                    if name == "page.close":
                        is_closed = getattr(handle, "is_closed", None)
                        page_closed = (
                            bool(is_closed()) if callable(is_closed) else True
                        )
                        self._teardown_observations["page_closed"] = page_closed
                        if not page_closed:
                            failures.append("page.close: page remains open after close")
                    elif name == "browser.close":
                        is_connected = getattr(handle, "is_connected", None)
                        browser_disconnected = (
                            not bool(is_connected()) if callable(is_connected) else True
                        )
                        self._teardown_observations["browser_disconnected"] = (
                            browser_disconnected
                        )
                        if not browser_disconnected:
                            failures.append(
                                "browser.close: browser remains connected after close"
                            )
                except Exception as exc:  # noqa: BLE001 - record and continue teardown
                    failure = f"{name}: {type(exc).__name__}: {exc}"
                    failures.append(failure)
                    logger.debug("navigator cleanup failed for %s", name, exc_info=True)
            if self._runtime is not None:
                try:
                    stop = getattr(self._runtime, "stop", None)
                    if stop is not None:
                        self._call("playwright.stop", stop)
                    self._teardown_observations["playwright_stopped"] = True
                except Exception as exc:  # noqa: BLE001 - record and continue teardown
                    failure = f"playwright.stop: {type(exc).__name__}: {exc}"
                    failures.append(failure)
                    logger.debug("navigator runtime cleanup failed", exc_info=True)
            self._teardown_failures = failures
            self._teardown_observations["teardown_failures"] = tuple(failures)
            self._teardown_observations["cleanup_complete"] = not failures
            if failures:
                self._cleanup_complete = False
                reason = "teardown failed: " + "; ".join(failures)
                self._set_state(SessionState.FAILED, reason)
                self._emit(
                    SessionEventType.ERROR,
                    state=SessionState.FAILED,
                    reason=reason,
                    payload={"teardown_failures": tuple(failures)},
                )
            else:
                self._cleanup_complete = True
            self._teardown_observations["cleanup_owner_thread_id"] = self.owner_thread_id
            if not failures:
                self._emit(
                    SessionEventType.CLEANUP_COMPLETE,
                    state=self._state,
                    reason="" if self._state is SessionState.FAILED else self._state.value,
                    payload={"owner_thread_id": self.owner_thread_id},
                )

    def run(self) -> None:
        self.owner_thread_id = threading.get_ident()
        self.thread_id = self.owner_thread_id
        try:
            self._open_browser()
            while not self._stop_requested:
                now = time.monotonic()
                if (
                    self._state is not SessionState.EXPIRED
                    and now >= self._deadline_monotonic
                ):
                    self._expire_session("session TTL expired")
                if (
                    self._state is SessionState.EXPIRED
                    and (
                        not self.resumable_on_expiry
                        or now >= self._resume_until_monotonic
                    )
                ):
                    self._stop_requested = True
                    break
                self._pump()
        except Exception as exc:  # noqa: BLE001 - worker failures are observable state
            if not self._state.terminal:
                self._set_state(SessionState.FAILED, f"{type(exc).__name__}: {exc}")
            logger.debug("navigator worker failed", exc_info=True)
        finally:
            cleanup_commands = 0
            try:
                self._cleanup()
                # Keep the original owner alive after a failed teardown. A
                # bounded number of retry commands can then close the same
                # Playwright handles on this thread; no manager thread may
                # adopt them.
                cleanup_deadline = time.monotonic() + self.cleanup_grace_seconds
                while not self._cleanup_complete:
                    remaining = cleanup_deadline - time.monotonic()
                    if cleanup_commands >= _MAX_CLEANUP_COMMANDS or remaining <= 0:
                        self._escalate_cleanup(
                            "teardown remained incomplete after bounded owner-thread retries"
                        )
                        break
                    try:
                        command = self.command_queue.get(timeout=min(0.1, remaining))
                    except queue.Empty:
                        continue
                    self._assert_owner(
                        f"teardown.command.{command.command.value}"
                    )
                    if command.command in {
                        SessionCommandType.RETRY_CLEANUP,
                        SessionCommandType.SHUTDOWN,
                        SessionCommandType.CANCEL,
                        SessionCommandType.CLOSE,
                    }:
                        cleanup_commands += 1
                        self._cleanup()
            except Exception as exc:  # noqa: BLE001 - preserve worker exit evidence
                logger.debug("navigator cleanup raised", exc_info=True)
                self._escalate_cleanup(f"cleanup failed before bounded escalation: {exc}")
            self._teardown_observations["worker_exited"] = True


class ApplicationNavigator:
    """Thread-safe session registry and queue-only command manager."""

    def __init__(
        self,
        database: Any | None = None,
        settings: Any | None = None,
        *,
        ttl_seconds: float = 86_400.0,
        url_resolver: Callable[[str], Any] | None = None,
        worker_factory: Callable[..., HeadedSessionWorker] = HeadedSessionWorker,
        headless: bool | None = None,
        executable_path: str | None = None,
        playwright_factory: Callable[[], Any] | None = None,
        apply_click_budget: ApplyClickBudget | None = None,
        cleanup_grace_seconds: float = _DEFAULT_CLEANUP_GRACE_SECONDS,
        expiry_preservation_seconds: float = (
            _DEFAULT_EXPIRY_PRESERVATION_SECONDS
        ),
        terminal_session_retention_seconds: float = (
            _DEFAULT_TERMINAL_SESSION_RETENTION_SECONDS
        ),
    ) -> None:
        self.database = database
        self.settings = settings
        self.ttl_seconds = max(0.01, float(ttl_seconds))
        self.url_resolver = url_resolver
        self.worker_factory = worker_factory
        self.headless = (
            bool(headless)
            if headless is not None
            else bool(getattr(settings, "browser_headless", True))
        )
        self.executable_path = executable_path
        self.playwright_factory = playwright_factory
        self.apply_click_enabled = bool(
            getattr(settings, "apply_click_enabled", False)
        )
        self.role_match_v2_enabled = bool(
            getattr(settings, "role_match_v2_enabled", False)
        )
        self.egress_impact_classification_enabled = bool(
            getattr(settings, "egress_impact_classification_enabled", True)
        )
        self.apply_click_timeout_ms = int(
            getattr(settings, "apply_click_timeout_ms", 10_000)
        )
        self.apply_click_budget = apply_click_budget
        if self.apply_click_enabled and self.apply_click_budget is None:
            self.apply_click_budget = ApplyClickBudget(
                int(getattr(settings, "apply_click_run_cap", 25))
            )
        self.cleanup_grace_seconds = max(0.01, float(cleanup_grace_seconds))
        self.expiry_preservation_seconds = max(
            0.01,
            float(expiry_preservation_seconds),
        )
        self.terminal_session_retention_seconds = max(
            0.0, float(terminal_session_retention_seconds)
        )
        self._sessions: dict[str, _ManagedSession] = {}
        # Source-resolution executors are kept by session id so the request
        # thread can retrieve only their immutable typed result.  The
        # executor itself never crosses into the request thread; all page
        # inspection remains on the worker owner thread.
        self._source_executors: dict[str, _SourceResolutionExecutor] = {}
        self._lock = threading.RLock()
        self._shutdown = False

    def _drain(self, record: _ManagedSession) -> None:
        while True:
            try:
                event = record.event_queue.get_nowait()
            except queue.Empty:
                break
            if event.occurred_at is not None:
                record.updated_at = event.occurred_at
            if event.state is not None:
                record.state = event.state
                if event.reason:
                    record.reason = event.reason
                boundary = event.payload.get("human_boundary")
                if isinstance(boundary, Mapping):
                    record.human_boundary = dict(boundary)
                elif event.state is not SessionState.HUMAN_REQUIRED:
                    record.human_boundary = {}
            if event.reason and event.event is SessionEventType.ERROR:
                record.reason = event.reason
                record.state = SessionState.FAILED
            if event.payload.get("cleanup_escalated") is True:
                record.cleanup_escalated = True
            if event.event is SessionEventType.WORKER_READY:
                record.owner_thread_id = (
                    record.worker.owner_thread_id if record.worker else None
                )
            elif event.event is SessionEventType.REQUEST:
                manifest = event.payload.get("manifest")
                if isinstance(manifest, Mapping):
                    self._merge_event_manifest(record, manifest)
                if "preflight" in event.payload:
                    preflight = dict(event.payload)
                    if event.state is not None:
                        preflight.setdefault("state", event.state.value)
                    if event.reason:
                        preflight.setdefault("reason", event.reason)
                    record.preflight_result = preflight
                boundary = event.payload.get("human_boundary")
                if isinstance(boundary, Mapping):
                    record.human_boundary = dict(boundary)
            elif event.event is SessionEventType.JOURNEY_RESULT:
                record.journey_result = dict(event.payload)
                manifest = event.payload.get("manifest")
                if isinstance(manifest, Mapping):
                    self._merge_event_manifest(record, manifest)
                boundary = event.payload.get("human_boundary")
                if isinstance(boundary, Mapping):
                    record.human_boundary = dict(boundary)
            elif event.event is SessionEventType.CLEANUP_COMPLETE:
                record.cleanup_complete = True
                owner = event.payload.get("owner_thread_id")
                if isinstance(owner, int):
                    record.owner_thread_id = owner

    @staticmethod
    def _merge_event_manifest(
        record: _ManagedSession,
        manifest: Mapping[str, object],
    ) -> None:
        """Never let a queued duplicate erase post-display target mutation."""

        candidate = dict(manifest)
        sealed = dict(record.sealed_manifest)
        if not sealed:
            record.manifest = candidate
            return
        if record.manifest != sealed:
            # Preserve the changed value so action-time validation can reject
            # it. A delayed duplicate of the old owner-thread event must not
            # restore the displayed target and turn the confirm into a pass.
            return
        if candidate != sealed:
            # A genuine provider-side change must also remain visible to the
            # review-binding validator and therefore fail closed.
            record.manifest = candidate

    def seal_manifest(
        self,
        session_id: str,
        manifest: Mapping[str, object],
    ) -> SessionSnapshot:
        """Seal the exact raw manifest that was displayed for confirmation."""

        with self._lock:
            record = self._find_record(session_id)
            self._drain(record)
            candidate = dict(manifest)
            if record.manifest != candidate:
                raise NavigatorError(
                    "Final manifest changed while it was being displayed"
                )
            if record.sealed_manifest and record.sealed_manifest != candidate:
                raise NavigatorError(
                    "A different final manifest is already sealed for this session"
                )
            record.sealed_manifest = candidate
            return self._snapshot_locked(record, drain=False)

    def _snapshot_locked(
        self,
        record: _ManagedSession,
        *,
        drain: bool = True,
    ) -> SessionSnapshot:
        if drain:
            self._drain(record)
        worker = record.worker
        return SessionSnapshot(
            session_id=record.session_id,
            application_id=record.application_id,
            mode=record.mode,
            state=record.state,
            created_at=record.created_at,
            updated_at=record.updated_at,
            expires_at=record.expires_at,
            reason=record.reason,
            summary=dict(record.summary),
            owner_thread_id=record.owner_thread_id
            or (worker.owner_thread_id if worker is not None else None),
            worker_alive=bool(worker and worker.is_alive()),
            cleanup_complete=record.cleanup_complete
            or bool(worker and worker.cleanup_complete),
            manifest=dict(record.manifest),
            headed=record.headed,
            human_boundary=dict(record.human_boundary),
            resumable=bool(
                record.state is SessionState.EXPIRED
                and worker
                and worker.is_alive()
                and record.resume_until is not None
                and record.resume_until > _utcnow()
            ),
            resume_until=record.resume_until,
        )

    def _find_record(self, session_id: str) -> _ManagedSession:
        record = self._sessions.get(session_id)
        if record is None:
            raise SessionNotFoundError(f"Unknown navigator session: {session_id}")
        return record

    def _source_context(self, application_id: str) -> dict[str, str]:
        """Load the authoritative source identity for one application id."""

        if self.database is None:
            raise ValueError("Navigator source resolution requires the application database")
        from app.models import Application, Opportunity

        with self.database.session_scope() as session:
            application = session.get(Application, application_id)
            if application is None:
                raise KeyError(f"Application not found: {application_id}")
            opportunity = session.get(Opportunity, application.opportunity_id)
            if opportunity is None:
                raise KeyError(f"Opportunity not found: {application.opportunity_id}")
            source_url = str(opportunity.url or "")
            inspection_url = str(opportunity.application_url or source_url)
            identity = requisition_identity_from_url(inspection_url)
            return {
                "application_id": str(application.id),
                "opportunity_id": str(opportunity.id),
                "source_url": source_url,
                "inspection_url": inspection_url,
                "authoritative_requisition": (
                    identity.requisition if identity is not None else ""
                ),
                "requisition_source": (
                    identity.provenance if identity is not None else ""
                ),
                "requisition_provider": (
                    identity.provider if identity is not None else ""
                ),
                "requisition_tenant": (
                    identity.tenant if identity is not None else ""
                ),
                "employer": str(opportunity.employer or ""),
                "role_title": str(opportunity.role_title or ""),
                "provider_hint": str(opportunity.ats_type or ""),
                "cycle": str(opportunity.cycle or ""),
                "source": str(opportunity.source or ""),
            }

    @staticmethod
    def _source_handoff(
        snapshot: SessionSnapshot,
        *,
        reason: str = "",
    ) -> dict[str, object]:
        """Flatten the scalar resumable boundary for target-resolution APIs."""

        boundary = dict(snapshot.human_boundary)
        next_action = (
            "Complete the visible source inspection, then press Continue to "
            "re-check the exact application destination."
        )
        return {
            "session_id": snapshot.session_id,
            "application_id": snapshot.application_id,
            "state": snapshot.state.value,
            "status": (
                str(boundary.get("status") or "awaiting_human")
                if boundary
                else snapshot.state.value
            ),
            "reason": reason or snapshot.reason or "source_target_unverified",
            "next_action": next_action,
            "human_required": True,
            "visible": snapshot.visibility == "headed",
            "headed": snapshot.headed,
            "kind": str(boundary.get("kind") or "human_review"),
            "expires_at": snapshot.expires_at.isoformat(),
            "can_continue": bool(boundary.get("can_continue", True)),
            "can_cancel": bool(boundary.get("can_cancel", True)),
            "no_auto_submit": True,
            "resumable": bool(boundary.get("resumable", True)),
        }

    def resolve_application_target(
        self,
        application_id: str,
        *,
        source_capability: SourceResolutionCapability | None = None,
        headed: bool = True,
    ) -> tuple[TargetResolution | None, Mapping[str, object]]:
        """Inspect a stored source in this Navigator's visible owner session.

        The method is intentionally application-id-only.  It starts a
        headed, review-mode, read-only worker against the authoritative
        stored source when no session exists, queues the owner-thread source
        inspection, and returns either an immutable (still unverified)
        ``TargetResolution`` or a scalar persistent handoff.  It never
        accepts a URL, creates a second manager, promotes a target, fills a
        form, or submits an application.
        """

        details = self._source_context(application_id)
        inspection_url_value = str(
            details.get("inspection_url") or details["source_url"]
        )
        allowlist = frozenset(
            getattr(self.settings, "live_domain_allowlist", frozenset())
        )
        if source_capability is not None:
            # Compare against a fresh authoritative database read immediately
            # before worker creation.  The capability host is copied into this
            # one worker only; Settings and every other session stay unchanged.
            source_capability.assert_authorizes(inspection_url_value)
            allowlist = frozenset((*allowlist, source_capability.hostname))
        # Source provenance is retained for persistence but is not navigated;
        # the one-time capability authorizes only the stored inspection URL.
        source_url = validate_navigation_url(details["source_url"])
        inspection_url = _validate_navigation_url(
            inspection_url_value, allowlist
        )
        executor = _SourceResolutionExecutor(
            source_url=source_url,
            inspection_url=inspection_url,
            authoritative_requisition=(
                details.get("authoritative_requisition")
                if "authoritative_requisition" in details
                else None
            ),
            requisition_source=details.get("requisition_source", ""),
            requisition_provider=details.get("requisition_provider", ""),
            requisition_tenant=details.get("requisition_tenant", ""),
            provider_hint=details["provider_hint"],
            employer=details["employer"],
            role_title=details["role_title"],
            allowlist=allowlist,
            role_match_v2_enabled=self.role_match_v2_enabled,
            apply_click_enabled=self.apply_click_enabled,
            apply_click_budget=self.apply_click_budget,
            apply_click_timeout_ms=self.apply_click_timeout_ms,
            source_capability=source_capability,
            opportunity_id=details["opportunity_id"],
            application_id=application_id,
        )

        with self._lock:
            if self._shutdown:
                raise NavigatorError("Application navigator is shut down")
            self._evict_terminal_sessions_locked()
            for existing in self._sessions.values():
                self._drain(existing)
                worker = existing.worker
                if existing.application_id != application_id:
                    continue
                cleanup_complete = existing.cleanup_complete or bool(
                    worker and worker.cleanup_complete
                )
                if bool(worker and worker.is_alive()) or not cleanup_complete:
                    if self.apply_click_enabled:
                        # A guarded click may never inherit or resume a prior
                        # handoff/automation browser.  Retire the old owner
                        # completely, then create a new sterile context below.
                        self.close(
                            existing.session_id,
                            reason="replace prior session with sterile resolution",
                        )
                        cleaned = self.wait_for_cleanup(
                            existing.session_id,
                            timeout=min(2.0, self.ttl_seconds),
                        )
                        if cleaned.worker_alive or not cleaned.cleanup_complete:
                            return None, self._source_handoff(
                                cleaned,
                                reason=(
                                    "prior session cleanup incomplete; sterile "
                                    "resolution was not started"
                                ),
                            )
                        continue
                    snapshot = self._snapshot_locked(existing)
                    existing_executor = self._source_executors.get(existing.session_id)
                    existing_resolution = (
                        existing_executor.resolution
                        if existing_executor is not None
                        else None
                    )
                    if existing_resolution is not None and existing_resolution.verified_for_automation:
                        # The source owner has produced a fully structured
                        # proof.  Tear it down before returning so the same
                        # application can never have a source browser and an
                        # Apply browser alive at once.  Persistence still
                        # happens in TargetResolutionService after this
                        # method returns; a later cross-binding refusal is
                        # fail-closed and cannot resurrect the owner.
                        self.close(
                            existing.session_id,
                            reason="verified source target captured",
                        )
                        cleaned = self.wait_for_cleanup(
                            existing.session_id,
                            timeout=min(2.0, self.ttl_seconds),
                        )
                        if cleaned.worker_alive or not cleaned.cleanup_complete:
                            # A verified target must never be promoted until
                            # the source owner is dead *and* cleanup completed.
                            # Escalation is evidence of failure, never a
                            # successful teardown.
                            return None, self._source_handoff(
                                cleaned,
                                reason=(
                                    "verified source target captured; source owner "
                                    "cleanup incomplete, promotion blocked until "
                                    "worker_alive=false and cleanup_complete=true"
                                ),
                            )
                        snapshot = self.get(existing.session_id)
                    if snapshot.worker_alive or not snapshot.cleanup_complete:
                        return None, self._source_handoff(
                            snapshot,
                            reason=(
                                "source owner cleanup incomplete; promotion blocked "
                                "until worker_alive=false and cleanup_complete=true"
                            ),
                        )
                    return existing_resolution, self._source_handoff(snapshot)

            now = _utcnow()
            ttl = self.ttl_seconds
            session_id = uuid.uuid4().hex[:16]
            record = _ManagedSession(
                session_id=session_id,
                application_id=application_id,
                mode=RunMode.REVIEW.value,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(seconds=ttl),
                deadline_monotonic=time.monotonic() + ttl,
                resume_until=now
                + timedelta(seconds=ttl + self.expiry_preservation_seconds),
                summary={
                    "application_id": application_id,
                    "opportunity_id": details["opportunity_id"],
                    "employer": details["employer"],
                    "role": details["role_title"],
                    "provider": details["provider_hint"],
                    "source_resolution": True,
                    "source_resolution_batch": not bool(headed),
                    "sterile_resolution": self.apply_click_enabled,
                },
                command_queue=queue.Queue(),
                event_queue=queue.Queue(),
                headed=bool(headed),
            )
            executable = self.executable_path
            if executable is None:
                configured = os.environ.get("ARGUS_CHROMIUM_EXECUTABLE", "").strip()
                executable = configured or None
            worker_kwargs: dict[str, object] = {
                "session_id": session_id,
                "application_id": application_id,
                "mode": RunMode.REVIEW.value,
                "url": inspection_url,
                "summary": record.summary,
                "command_queue": record.command_queue,
                "event_queue": record.event_queue,
                "ttl_seconds": ttl,
                "deadline_monotonic": record.deadline_monotonic,
                "expires_at": record.expires_at,
                "headless": not bool(headed),
                "executable_path": executable,
                "allowlist": allowlist,
                # Sterile/batch source resolution is intentionally destroyed
                # and is never a resumable human application handoff.
                "resumable_on_expiry": False,
                "expiry_preservation_seconds": self.expiry_preservation_seconds,
                "egress_impact_classification_enabled": (
                    self.egress_impact_classification_enabled
                ),
                "cleanup_grace_seconds": self.cleanup_grace_seconds,
                "journey_executor": executor,
            }
            if self.playwright_factory is not None:
                worker_kwargs["playwright_factory"] = self.playwright_factory
            worker = self.worker_factory(**worker_kwargs)
            record.worker = worker
            self._sessions[session_id] = record
            self._source_executors[session_id] = executor
            worker.start()

        # The flag-off path keeps the established resumable source session.
        # The sterile flag-on path uses this wait only to complete its single
        # attempt, then destroys the context before returning.
        try:
            self._command(session_id, SessionCommandType.RUN_JOURNEY)
            journey_timeout = 20.0 if not headed else 2.0
            self.wait_for_journey_result(
                session_id, timeout=min(journey_timeout, ttl)
            )
        except (KeyError, SessionNotFoundError):
            pass
        snapshot = self.get(session_id)
        if executor._apply_click_fatal:
            self.close(
                session_id,
                reason="fatal apply-click confirmation/submission boundary",
            )
            cleaned = self.wait_for_cleanup(
                session_id,
                timeout=min(2.0, self.ttl_seconds),
            )
            if cleaned.worker_alive or not cleaned.cleanup_complete:
                raise ApplyClickSafetyViolation(
                    "Apply click landed on confirmation/submission and cleanup "
                    "was incomplete; priority run halted",
                    evidence=dict(executor._apply_click_evidence),
                )
            raise ApplyClickSafetyViolation(
                "Apply click landed on confirmation/submission; priority run halted",
                evidence=dict(executor._apply_click_evidence),
            )
        verified = bool(
            executor.resolution is not None
            and executor.resolution.verified_for_automation
        )
        if verified or self.apply_click_enabled:
            self.close(
                session_id,
                reason=(
                    "sterile resolution attempt complete"
                    if self.apply_click_enabled
                    else "verified source target captured"
                ),
            )
            cleaned = self.wait_for_cleanup(
                session_id,
                timeout=min(2.0, self.ttl_seconds),
            )
            if cleaned.worker_alive or not cleaned.cleanup_complete:
                return None, self._source_handoff(
                    cleaned,
                    reason=(
                        "sterile source owner cleanup incomplete; result blocked "
                        "until worker_alive=false and cleanup_complete=true"
                        if self.apply_click_enabled
                        else
                        "verified source target captured; source owner cleanup "
                        "incomplete, promotion blocked until worker_alive=false "
                        "and cleanup_complete=true"
                    ),
                )
            snapshot = self.get(session_id)
        handoff = self._source_handoff(snapshot)
        if self.apply_click_enabled:
            handoff.update(
                {
                    "status": "sterile_attempt_complete",
                    "reason": "Sterile resolution attempt complete; context destroyed",
                    "next_action": (
                        "Review the recorded resolution outcome; start a new "
                        "resolution attempt if another inspection is required."
                    ),
                    "visible": False,
                    "headed": False,
                    "can_continue": False,
                    "resumable": False,
                }
            )
        return executor.resolution, handoff

    @staticmethod
    def _coerce_resolution(value: Any) -> tuple[TargetResolution, dict[str, object]]:
        """Accept only an immutable verified resolution from a resolver."""

        summary: dict[str, object] = {}
        candidate = value
        if isinstance(value, tuple):
            if len(value) != 2 or not isinstance(value[1], Mapping):
                raise ValueError("Navigator resolver must return TargetResolution plus summary")
            candidate, raw_summary = value
            summary = dict(raw_summary)
        elif isinstance(value, Mapping):
            candidate = value.get("resolution")
            raw_summary = value.get("summary")
            if isinstance(raw_summary, Mapping):
                summary = dict(raw_summary)
        if not isinstance(candidate, TargetResolution):
            raise ValueError(
                "Navigator target must be a fully reconstructed TargetResolution; raw URL targets are forbidden"
            )
        # Direct resolver results are untrusted input just like persisted
        # envelopes.  Apply the shared nested-proof contract before any
        # worker/browser construction so contradictory provider, identity,
        # form, or destination evidence cannot reach Playwright.
        validate_target_resolution_contract(candidate)
        if not _resolution_is_complete(candidate):
            raise ValueError(
                "Navigator target resolution is incomplete or not currently verified"
            )
        return candidate, summary

    def _resolve_target(self, application_id: str) -> tuple[TargetResolution, dict[str, object]]:
        if self.url_resolver is not None:
            return self._coerce_resolution(self.url_resolver(application_id))
        if self.database is None:
            raise ValueError(
                "Navigator requires a persisted or explicitly reconstructed verified target"
            )
        from app.models import Application, Opportunity

        with self.database.session_scope() as session:
            application = session.get(Application, application_id)
            if application is None:
                raise KeyError(f"Application not found: {application_id}")
            opportunity = session.get(Opportunity, application.opportunity_id)
            if opportunity is None:
                raise KeyError(f"Opportunity not found: {application.opportunity_id}")
            resolution = _load_verified_target_from_opportunity(opportunity)
            # Keep the immutable target proof available to direct Navigator
            # sessions.  SubmissionTarget carries provider/form evidence but
            # some adapters intentionally leave its provider requisition
            # field empty; the persisted, verified resolution remains the
            # authoritative fallback for the final-review manifest.
            requisition = ""
            evidence = getattr(resolution, "evidence", {})
            if isinstance(evidence, Mapping):
                for key in (
                    "requisition",
                    "requisition_id",
                    "job_id",
                    "posting_id",
                    "target_path",
                    "application_path",
                ):
                    value = evidence.get(key)
                    if value is not None and str(value).strip():
                        requisition = str(value).strip()
                        break
            summary = {
                "employer": opportunity.employer,
                "role": opportunity.role_title,
                "provider": resolution.provider,
                "requisition": requisition,
                "source_url": opportunity.url,
                "application_url": getattr(opportunity, "application_url", None),
                "target_status": getattr(opportunity, "target_status", "UNRESOLVED"),
            }
            return resolution, summary

    def start(
        self,
        application_id: str,
        mode: RunMode | str,
        *,
        headed: bool | None = None,
        url: str | None = None,
        resolution: TargetResolution | None = None,
        summary: Mapping[str, object] | None = None,
        ttl_seconds: float | None = None,
        journey_executor: Any | None = None,
        run_allowlist: frozenset[str] | None = None,
    ) -> SessionSnapshot:
        """Start a worker and return an ``OPENING`` snapshot immediately."""

        if not application_id or not str(application_id).strip():
            raise ValueError("application_id is required")
        mode_value = _normalise_mode(mode)
        if url is not None:
            raise ValueError(
                "Caller-supplied raw url= targets are forbidden; provide a verified TargetResolution"
            )
        if resolution is not None:
            resolved_target, resolved_summary = self._coerce_resolution(resolution)
        else:
            resolved_target, resolved_summary = self._resolve_target(application_id)
        target_url = resolved_target.final_url
        if summary:
            resolved_summary.update(summary)
        # A runner-provided summary is useful context, but it must not erase
        # load-bearing requisition proof carried by the verified resolution.
        # This matters for provider SubmissionTarget objects whose adapter
        # intentionally leaves ``requisition`` blank: FINAL_REVIEW still
        # needs the exact persisted path/identifier before confirmation.
        resolution_evidence = getattr(resolved_target, "evidence", {})
        if isinstance(resolution_evidence, Mapping):
            for key in (
                "requisition",
                "requisition_id",
                "job_id",
                "posting_id",
                "target_path",
                "application_path",
            ):
                value = resolution_evidence.get(key)
                if value is not None and str(value).strip():
                    resolved_summary[key] = str(value).strip()
                    if key != "requisition":
                        resolved_summary.setdefault("requisition", str(value).strip())
                    break
        allowlist_is_run_scoped = run_allowlist is not None
        allowlist = (
            frozenset(str(item).strip() for item in run_allowlist if str(item).strip())
            if run_allowlist is not None
            else frozenset(
                getattr(self.settings, "live_domain_allowlist", frozenset())
            )
        )
        if mode_value == RunMode.REVIEW.value and not allowlist_is_run_scoped:
            trusted_target_provider = trusted_provider_for_url(str(target_url or ""))
            if trusted_target_provider:
                try:
                    target_host = urlsplit(str(target_url)).hostname
                except ValueError:
                    target_host = None
                if target_host:
                    # REVIEW is a visible, read-only inspection.  Extend only
                    # to this exact trusted ATS host; PREFILL/SUBMIT retain
                    # the explicit configured allowlist and confirmation gate.
                    allowlist = frozenset((*allowlist, target_host))
        target_url = _validate_navigation_url(str(target_url or ""), allowlist)
        # ``headless`` is a manager default, never a lock on all sessions.
        # Review is intrinsically a visible handoff.  Other modes honour the
        # caller's explicit per-session intent so a shared headless singleton
        # cannot silently discard ``headed=True``.
        requested_headed = (
            bool(headed) if headed is not None else not self.headless
        )
        if mode_value in {
            RunMode.REVIEW.value,
            RunMode.PREFILL.value,
            RunMode.SUBMIT.value,
        }:
            # These modes can encounter a human boundary after navigation
            # (including a CAPTCHA/MFA injected by the destination).  Starting
            # them visibly is the only deterministic way to guarantee that a
            # boundary is actionable; a caller's headless manager default must
            # never strand the user in an invisible browser.
            requested_headed = True
        with self._lock:
            if self._shutdown:
                raise NavigatorError("Application navigator is shut down")
            self._evict_terminal_sessions_locked()
            for existing in self._sessions.values():
                self._drain(existing)
                existing_worker = existing.worker
                cleanup_complete = existing.cleanup_complete or bool(
                    existing_worker and existing_worker.cleanup_complete
                )
                if (
                    existing.application_id == application_id
                    and (
                        bool(existing_worker and existing_worker.is_alive())
                        or not cleanup_complete
                    )
                ):
                    raise DuplicateSessionError(
                        f"Application {application_id} already has an active session "
                        "or incomplete teardown"
                    )
            now = _utcnow()
            ttl = self.ttl_seconds if ttl_seconds is None else max(0.01, float(ttl_seconds))
            session_id = uuid.uuid4().hex[:16]
            record = _ManagedSession(
                session_id=session_id,
                application_id=application_id,
                mode=mode_value,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(seconds=ttl),
                deadline_monotonic=time.monotonic() + ttl,
                resume_until=now
                + timedelta(seconds=ttl + self.expiry_preservation_seconds),
                summary=resolved_summary,
                command_queue=queue.Queue(),
                event_queue=queue.Queue(),
                headed=requested_headed,
            )
            executable = self.executable_path
            if executable is None:
                configured = os.environ.get("ARGUS_CHROMIUM_EXECUTABLE", "").strip()
                executable = configured or None
            worker_kwargs = {
                "session_id": session_id,
                "application_id": application_id,
                "mode": mode_value,
                "url": target_url,
                "summary": resolved_summary,
                "command_queue": record.command_queue,
                "event_queue": record.event_queue,
                "ttl_seconds": ttl,
                "deadline_monotonic": record.deadline_monotonic,
                "expires_at": record.expires_at,
                "headless": not requested_headed,
                "executable_path": executable,
                "allowlist": allowlist,
                "allowlist_is_run_scoped": allowlist_is_run_scoped,
                "resumable_on_expiry": True,
                "expiry_preservation_seconds": self.expiry_preservation_seconds,
                "egress_impact_classification_enabled": (
                    self.egress_impact_classification_enabled
                ),
                "cleanup_grace_seconds": self.cleanup_grace_seconds,
            }
            if self.playwright_factory is not None:
                worker_kwargs["playwright_factory"] = self.playwright_factory
            if journey_executor is not None:
                worker_kwargs["journey_executor"] = journey_executor
            worker = self.worker_factory(
                **worker_kwargs,
            )
            record.worker = worker
            self._sessions[session_id] = record
            worker.start()
            # Do not drain the worker's first event here.  Returning OPENING
            # makes the asynchronous contract deterministic; callers can poll
            # and observe ACTIVE/HUMAN_REQUIRED through get().
            return self._snapshot_locked(record, drain=False)

    def _command(
        self,
        session_id: str,
        command: SessionCommandType,
        *,
        reason: str = "",
        payload: Mapping[str, object] | None = None,
    ) -> SessionSnapshot:
        with self._lock:
            record = self._find_record(session_id)
            self._drain(record)
            if record.state.terminal:
                return self._snapshot_locked(record)
            record.command_queue.put(
                SessionCommand(
                    command=command,
                    session_id=session_id,
                    reason=reason,
                    payload=dict(payload or {}),
                )
            )
            if command in {
                SessionCommandType.CANCEL,
                SessionCommandType.CLOSE,
                SessionCommandType.SHUTDOWN,
            }:
                # Teardown is still performed by the owner.  A short bounded
                # acknowledgement gives HTTP callers a truthful terminal
                # response without turning the endpoint into an indefinite
                # browser keeper.
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    snapshot = self._snapshot_locked(record)
                    if snapshot.state.terminal and not snapshot.worker_alive:
                        return snapshot
                    time.sleep(0.005)
            return self._snapshot_locked(record)

    def continue_after_human(self, session_id: str) -> SessionSnapshot:
        return self._command(session_id, SessionCommandType.CONTINUE)

    def resume(self, session_id: str) -> SessionSnapshot:
        """Resume one expired, preserved page with a fresh finite TTL."""

        with self._lock:
            record = self._find_record(session_id)
            self._drain(record)
            worker = record.worker
            if (
                record.state is not SessionState.EXPIRED
                or worker is None
                or not worker.is_alive()
                or record.resume_until is None
                or record.resume_until <= _utcnow()
            ):
                raise SessionCommandRejected(
                    f"Session {session_id} has no live preserved page to resume"
                )
            now = _utcnow()
            ttl = self.ttl_seconds
            expires_at = now + timedelta(seconds=ttl)
            deadline = time.monotonic() + ttl
            record.expires_at = expires_at
            record.deadline_monotonic = deadline
            record.resume_until = expires_at + timedelta(
                seconds=self.expiry_preservation_seconds
            )
            record.command_queue.put(
                SessionCommand(
                    command=SessionCommandType.RESUME,
                    session_id=session_id,
                    reason="resume expired preserved handoff",
                    payload={
                        "expires_at": expires_at.isoformat(),
                        "deadline_monotonic": deadline,
                    },
                )
            )
            acknowledgement_deadline = time.monotonic() + 2.0
            while time.monotonic() < acknowledgement_deadline:
                snapshot = self._snapshot_locked(record)
                if snapshot.state is not SessionState.EXPIRED:
                    return snapshot
                time.sleep(0.005)
            raise SessionCommandRejected(
                f"Session {session_id} owner did not acknowledge resume"
            )

    def run_journey(self, session_id: str) -> SessionSnapshot:
        """Queue a typed owner-thread journey execution command."""

        return self._command(session_id, SessionCommandType.RUN_JOURNEY)

    def confirm_submission(
        self, session_id: str, *, confirmation_id: str
    ) -> SessionSnapshot:
        """Queue exact action-time confirmation for the bound final target."""

        return self._command(
            session_id,
            SessionCommandType.CONFIRM_SUBMISSION,
            payload={"confirmation_id": str(confirmation_id)},
        )

    def journey_result(self, session_id: str) -> Mapping[str, object]:
        with self._lock:
            record = self._find_record(session_id)
            self._drain(record)
            return dict(record.journey_result)

    def wait_for_journey_result(
        self, session_id: str, *, timeout: float = 5.0
    ) -> Mapping[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.journey_result(session_id)
            if result:
                return result
            snapshot = self.get(session_id)
            if snapshot.state is SessionState.FAILED:
                return {"state": "FAILED", "reason": snapshot.reason}
            time.sleep(0.01)
        return self.journey_result(session_id)

    def retry_cleanup(self, session_id: str) -> SessionSnapshot:
        """Retry incomplete teardown on the original owner thread."""

        with self._lock:
            record = self._find_record(session_id)
            self._drain(record)
            worker = record.worker
            if record.cleanup_complete or bool(worker and worker.cleanup_complete):
                return self._snapshot_locked(record)
            if worker is None or not worker.is_alive():
                raise SessionCommandRejected(
                    f"Session {session_id} has no live owner for cleanup retry"
                )
            record.command_queue.put(
                SessionCommand(
                    command=SessionCommandType.RETRY_CLEANUP,
                    session_id=session_id,
                    reason="retry incomplete owner-thread teardown",
                )
            )
            return self._snapshot_locked(record)

    def inject_human_required(
        self,
        session_id: str,
        *,
        reason: str = "captcha_injected",
    ) -> SessionSnapshot:
        """Inject a deterministic human boundary for local tests only."""

        return self._command(
            session_id,
            SessionCommandType.INJECT_HUMAN_REQUIRED,
            reason=reason,
        )

    inject_captcha = inject_human_required

    def request_final_manifest(self, session_id: str) -> SessionSnapshot:
        return self._command(session_id, SessionCommandType.FINAL_MANIFEST)

    def preflight_submission(
        self,
        session_id: str,
        *,
        timeout: float = 5.0,
    ) -> SessionSnapshot:
        """Revalidate the exact final target/control without clicking.

        Callers should invoke this immediately before consuming a durable
        submission authority.  The returned snapshot is FINAL_REVIEW only
        when the owner-thread scan passed; HUMAN_REQUIRED/ACTIVE/FAILED is a
        refusal and must leave the authority unconsumed.  The method waits
        for the owner-thread envelope so a request thread never races ahead
        of the actual scan; use :meth:`wait_for_preflight` to inspect the
        decisive ``preflight_passed`` value.
        """

        with self._lock:
            record = self._find_record(session_id)
            self._drain(record)
            # A prior refusal/pass must never satisfy this invocation.
            record.preflight_result = {}
        self._command(session_id, SessionCommandType.PREFLIGHT_SUBMISSION)
        self.wait_for_preflight(session_id, timeout=timeout)
        return self.get(session_id)

    def wait_for_preflight(
        self,
        session_id: str,
        *,
        timeout: float = 5.0,
    ) -> Mapping[str, object]:
        """Wait for and return the latest owner-thread preflight envelope.

        An empty mapping means the worker did not acknowledge the command
        before the bounded timeout; callers must treat that as a refusal and
        must not consume an authority.
        """

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                record = self._find_record(session_id)
                self._drain(record)
                if record.preflight_result:
                    return dict(record.preflight_result)
                if record.state.terminal and not (record.worker and record.worker.is_alive()):
                    return {}
            if time.monotonic() >= deadline:
                return {}
            time.sleep(0.01)

    def confirm(self, session_id: str) -> SessionSnapshot:
        return self._command(
            session_id,
            SessionCommandType.CONFIRM,
            payload={"confirmed": True},
        )

    def cancel(self, session_id: str, *, reason: str = "") -> SessionSnapshot:
        return self._command(session_id, SessionCommandType.CANCEL, reason=reason)

    def close(self, session_id: str, *, reason: str = "") -> SessionSnapshot:
        return self._command(
            session_id,
            SessionCommandType.CLOSE,
            reason=reason or "closed by user",
        )

    def get(self, session_id: str) -> SessionSnapshot:
        with self._lock:
            return self._snapshot_locked(self._find_record(session_id))

    def diagnostics(self, session_id: str) -> SessionDiagnostics:
        """Return immutable owner/teardown evidence without raw handles."""

        with self._lock:
            record = self._find_record(session_id)
            self._drain(record)
            worker = record.worker
            if worker is None:
                raise NavigatorError(f"Session {session_id} has no worker")
            return worker.diagnostics()

    def all_sessions(self) -> list[SessionSnapshot]:
        # The registry is request-driven, so listing sessions is the durable
        # reaper hook used by the HTTP surface rather than a best-effort helper
        # that callers must remember to invoke separately.
        self.reap_expired()
        with self._lock:
            return [self._snapshot_locked(record) for record in self._sessions.values()]

    def _evict_terminal_sessions_locked(self) -> tuple[str, ...]:
        """Evict only dead, terminal records with complete cleanup."""

        now = _utcnow()
        evicted: list[str] = []
        for session_id, record in list(self._sessions.items()):
            self._drain(record)
            worker = record.worker
            if not record.state.terminal or bool(worker and worker.is_alive()):
                continue
            cleanup_complete = record.cleanup_complete or bool(
                worker and worker.cleanup_complete
            )
            if not cleanup_complete:
                continue
            age = max(0.0, (now - record.updated_at).total_seconds())
            if age < self.terminal_session_retention_seconds:
                continue
            self._sessions.pop(session_id, None)
            self._source_executors.pop(session_id, None)
            evicted.append(session_id)
        return tuple(evicted)

    def active_for_application(self, application_id: str) -> SessionSnapshot | None:
        with self._lock:
            for record in self._sessions.values():
                snapshot = self._snapshot_locked(record)
                if (
                    snapshot.application_id == application_id
                    and snapshot.state in _ACTIVE_STATES
                    and snapshot.worker_alive
                ):
                    return snapshot
        return None

    def status_of(self, application_id: str) -> SessionSnapshot | None:
        with self._lock:
            matching = [
                record
                for record in self._sessions.values()
                if record.application_id == application_id
            ]
            if not matching:
                return None
            return self._snapshot_locked(max(matching, key=lambda item: item.created_at))

    def wait_for_state(
        self,
        session_id: str,
        state: SessionState,
        *,
        timeout: float = 5.0,
    ) -> SessionSnapshot:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.get(session_id)
            if snapshot.state is state:
                return snapshot
            if snapshot.state.terminal and snapshot.state is not state:
                return snapshot
            time.sleep(0.01)
        return self.get(session_id)

    def wait_for_terminal(self, session_id: str, *, timeout: float = 5.0) -> SessionSnapshot:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.get(session_id)
            if snapshot.state.terminal and not snapshot.worker_alive:
                return snapshot
            time.sleep(0.01)
        return self.get(session_id)

    def wait_for_cleanup(self, session_id: str, *, timeout: float = 5.0) -> SessionSnapshot:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.get(session_id)
            if snapshot.cleanup_complete and not snapshot.worker_alive:
                return snapshot
            time.sleep(0.01)
        return self.get(session_id)

    def reap_expired(self) -> list[str]:
        """Request expiry handling; the worker performs state/teardown itself."""

        expired: list[str] = []
        with self._lock:
            for record in self._sessions.values():
                snapshot = self._snapshot_locked(record)
                if snapshot.state in _ACTIVE_STATES and snapshot.expires_at <= _utcnow():
                    expired.append(record.session_id)
        # Worker loops already enforce TTL; this explicit nudge keeps the
        # compatibility API useful while retaining owner-thread state/close.
        for session_id in expired:
            self._command(
                session_id,
                SessionCommandType.EXPIRE,
                reason="session TTL expired",
            )
        with self._lock:
            self._evict_terminal_sessions_locked()
        return expired

    def complete(self, session_id: str, *, outcome: str = "") -> bool:
        """Deprecated compatibility alias for close; it never means submitted."""

        snapshot = self.close(session_id, reason=outcome or "closed via deprecated complete")
        return snapshot.state not in _ACTIVE_STATES

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop every worker and join it; all browser teardown stays owner-side."""

        with self._lock:
            if self._shutdown:
                records = list(self._sessions.values())
            else:
                self._shutdown = True
                records = list(self._sessions.values())
            for record in records:
                self._drain(record)
                worker = record.worker
                if record.cleanup_complete and record.state not in _ACTIVE_STATES:
                    continue
                record.command_queue.put(
                    SessionCommand(
                        command=SessionCommandType.SHUTDOWN,
                        session_id=record.session_id,
                        reason="application shutdown",
                    )
                )
        deadline = time.monotonic() + timeout
        for record in records:
            worker = record.worker
            if worker is None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            worker.join(remaining)
        with self._lock:
            for record in records:
                self._drain(record)
            incomplete = []
            for record in records:
                worker = record.worker
                if worker is None:
                    continue
                if worker.is_alive():
                    incomplete.append(record.session_id)
                    continue
                if worker.cleanup_complete:
                    continue
                if not worker.cleanup_complete:
                    incomplete.append(record.session_id)
            if incomplete:
                raise NavigatorShutdownError(tuple(incomplete))


# Public compatibility name used by the existing API wiring.  The concrete
# implementation lives here so ApplicationNavigator and the old manager share
# one owner-thread invariant.
HeadedSessionManager = ApplicationNavigator


__all__ = [
    "ApplicationNavigator",
    "bind_service_navigator",
    "DuplicateSessionError",
    "HeadedSessionManager",
    "HeadedSessionWorker",
    "NavigatorError",
    "NavigatorShutdownError",
    "SessionCommandRejected",
    "SessionNotFoundError",
    "service_navigator_for",
]
