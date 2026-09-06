from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.host_policy import origin_for_url
from app.automation.targets import (
    EMPLOYER_EVIDENCE_KEYS,
    FORM_IDENTITY_EVIDENCE_KEYS,
    PROVIDER_EVIDENCE_KEYS,
    REQUISITION_EVIDENCE_KEYS,
    ROLE_EVIDENCE_KEYS,
    ROOT_TOKEN_EVIDENCE_KEYS,
    TargetResolution,
    normalise_evidence_key,
    validate_target_resolution_contract,
)
from app.domain.states import (
    ApplicationState,
    user_status_exclusion_reason,
    validate_transition,
)
from app.domain.targets import TargetKind, canonical_url_key, validate_navigation_url
from app.models import Application, Opportunity
from app.security.audit import AuditInput, append_audit
from app.services.requisition_identity import (
    RequisitionIdentity,
    identity_matches_url,
    requisition_identity_from_url,
    requisitions_equal,
)


_SENSITIVE_EVIDENCE_KEY = re.compile(
  r"answer|candidate|profile|email|phone|address|resume|\bcv\b|cover.?letter|"
  r"token|secret|password|authorization|cookie|session|csrf|nonce|api.?key|"
  r"auth|signature|state|oauth|\bcode\b|client.?secret",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_KEY = re.compile(
  r"token|secret|password|authorization|cookie|session|csrf|nonce|api.?key|"
  r"email|phone|address|candidate|profile|resume|cv|cover.?letter|answer|"
  r"\bauth\b|signature|state|oauth|(?:^|[_-])code(?:$|[_-])|client.?secret",
    re.IGNORECASE,
)
_BEARER_TOKEN = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKENISH = re.compile(
    r"(?i)\b(?:sk|pk|tok|token|secret|sess|csrf)[A-Za-z0-9_-]{3,}\b"
)
_JWT_TOKEN = re.compile(
    r"\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
# Do not treat arbitrary digit/dash identifiers as phones.  Requisitions such
# as REQ-2027-00431 and numeric job IDs are load-bearing target proof.
_PHONE = re.compile(
    r"(?<![\w-])(?:\+\d[\d ()-]{7,}\d|\(?\d{3}\)?[ -]\d{3}[ -]\d{4})(?![\w-])"
)
_IDENTITY_EVIDENCE_KEYS = frozenset(
    {
        *(normalise_evidence_key(key) for key in PROVIDER_EVIDENCE_KEYS),
        *(normalise_evidence_key(key) for key in EMPLOYER_EVIDENCE_KEYS),
        *(normalise_evidence_key(key) for key in ROLE_EVIDENCE_KEYS),
        *(normalise_evidence_key(key) for key in REQUISITION_EVIDENCE_KEYS),
        *(normalise_evidence_key(key) for key in FORM_IDENTITY_EVIDENCE_KEYS),
        *(normalise_evidence_key(key) for key in ROOT_TOKEN_EVIDENCE_KEYS),
        "applicationorigin",
        "origin",
        "verifiedorigin",
        "canonicalorigin",
        "boundorigin",
        "frameurl",
        "boundframeurl",
        "boundtargeturl",
        "rootselector",
        "applicationroot",
        "formselector",
    }
)
_MAX_EVIDENCE_DEPTH = 8
_MAX_EVIDENCE_ITEMS = 100
_MAX_EVIDENCE_STRING = 1000


class UserStatusAutomationExcludedError(RuntimeError):
    """A candidate-owned status excluded target-resolution work."""


def _sanitise_text(value: str, *, key: str = "") -> str:
    text = value[:_MAX_EVIDENCE_STRING]
    text = _BEARER_TOKEN.sub("[redacted-token]", text)
    text = _TOKENISH.sub("[redacted-token]", text)
    text = _JWT_TOKEN.sub("[redacted-token]", text)
    text = re.sub(
        r"(?i)([\'\"]?\b(token|secret|password|authorization|session|csrf|nonce|"
        r"api[_-]?(?:key|token)|access[_-]?token|refresh[_-]?token|"
        r"email|phone|address|auth|signature|state|oauth|code|client[_-]?secret)"
        r"[\'\"]?\s*[:=]\s*[\'\"]?)([^\'\"\s&,;}]+)",
        r"\1[redacted]",
        text,
    )
    text = _EMAIL.sub("[redacted-email]", text)
    if normalise_evidence_key(key) not in _IDENTITY_EVIDENCE_KEYS:
        text = _PHONE.sub("[redacted-phone]", text)
    return text


def _sanitise_url(value: str, *, key: str = "") -> str:
    try:
        parsed = urlsplit(validate_navigation_url(value))
    except ValueError:
        return _sanitise_text(value, key=key)
    safe_pairs: list[tuple[str, str]] = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        safe_key = _sanitise_text(key)
        safe_pairs.append(
            (safe_key, "[redacted]")
            if _SENSITIVE_QUERY_KEY.search(key)
            else (safe_key, _sanitise_text(item, key=key))
        )
    fragment = _sanitise_text(parsed.fragment, key=key)
    safe_url = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            _sanitise_text(parsed.path, key=key),
            urlencode(safe_pairs),
            fragment,
        )
    )
    return safe_url[:_MAX_EVIDENCE_STRING]


def _contract_url(value: object) -> str:
    """Return the exact URL the target columns store, not a redacted copy.

    ``source_url``, ``final_url`` and ``verified_target.application_url`` are
    the binding contract that ``_load_verified_target_from_opportunity``
    re-checks against ``opportunity.url`` / ``opportunity.application_url``.
    Those columns are written from the SAME ``result.final_url`` via
    ``validate_navigation_url`` and are therefore unredacted, so routing this
    copy through ``_sanitise_url`` protected nothing -- the value was already
    stored in the clear one column away -- while making the contract
    permanently unverifiable whenever a query key merely LOOKED sensitive.
    Greenhouse's ``embed/job_app?for=<board>&token=<job id>`` is exactly that
    case: ``token`` there is the public job identifier, so the envelope was
    written as ``token=[redacted]`` and then failed its own equality check on
    every run, forever.  Every other evidence field stays sanitised.
    """

    try:
        return validate_navigation_url(str(value or ""))
    except (TypeError, ValueError):
        return _sanitise_url(str(value or ""))


def _sanitise_evidence(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if depth > _MAX_EVIDENCE_DEPTH:
        return "[truncated]"
    if (
        key
        and normalise_evidence_key(key) not in _IDENTITY_EVIDENCE_KEYS
        and _SENSITIVE_EVIDENCE_KEY.search(key)
    ):
        return "[redacted]"
    if isinstance(value, Mapping):
        return {
            _sanitise_text(str(item_key))[:120]: _sanitise_evidence(
                item_value, key=str(item_key), depth=depth + 1
            )
            for item_key, item_value in list(value.items())[:_MAX_EVIDENCE_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [
            _sanitise_evidence(item, depth=depth + 1)
            for item in value[:_MAX_EVIDENCE_ITEMS]
        ]
    if isinstance(value, (set, frozenset)):
        return [
            _sanitise_evidence(item, depth=depth + 1)
            for item in list(value)[:_MAX_EVIDENCE_ITEMS]
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    if "//" in text and re.match(r"^https?://", text, re.IGNORECASE):
        return _sanitise_url(text, key=key)
    return _sanitise_text(text, key=key)


def _evidence_scalar_values(value: Any, aliases: frozenset[str], *, depth: int = 0):
    if depth > _MAX_EVIDENCE_DEPTH:
        return
    if isinstance(value, Mapping):
        for raw_key, item in list(value.items())[:_MAX_EVIDENCE_ITEMS]:
            # Apply-click metadata is operational telemetry.  Its ``role`` is
            # the accessibility role of the clicked control (for example,
            # ``button``), never evidence of the opportunity's job title.
            # Keep it in persisted/audit evidence but outside identity proof.
            if normalise_evidence_key(raw_key) == "applyclick":
                continue
            if (
                normalise_evidence_key(raw_key) in aliases
                and not isinstance(item, (Mapping, list, tuple, set, frozenset, bool))
            ):
                text = str(item or "").strip()
                if text and text.casefold() not in {"none", "null", "false"}:
                    yield text
            yield from _evidence_scalar_values(item, aliases, depth=depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in list(value)[:_MAX_EVIDENCE_ITEMS]:
            yield from _evidence_scalar_values(item, aliases, depth=depth + 1)


def _target_path(url: str) -> str:
    path = urlsplit(validate_navigation_url(url)).path or "/"
    return path.rstrip("/") or "/"


def _stamp_verified_identity(
    evidence: Any,
    *,
    result: TargetResolution,
    opportunity: Opportunity,
    promote: bool,
    job_identity: RequisitionIdentity | None,
    inspection_url: str,
    inspection_provenance: str,
) -> dict[str, Any]:
    safe = _sanitise_evidence(evidence)
    persisted = dict(safe) if isinstance(safe, Mapping) else {}
    persisted["inspection_url"] = _sanitise_url(
        inspection_url, key="inspection_url"
    )
    persisted["inspection_provenance"] = _sanitise_text(inspection_provenance)
    if job_identity is not None:
        persisted["job_identity"] = job_identity.as_evidence()
    if not promote:
        return persisted
    final_url = validate_navigation_url(result.final_url)
    existing_requisition = next(
        iter(_evidence_scalar_values(persisted, REQUISITION_EVIDENCE_KEYS)), ""
    )
    existing_form_identity = next(
        iter(_evidence_scalar_values(persisted, FORM_IDENTITY_EVIDENCE_KEYS)),
        "",
    )
    if not existing_form_identity:
        existing_form_identity = next(
            iter(_evidence_scalar_values(persisted, ROOT_TOKEN_EVIDENCE_KEYS)),
            "",
        )
    persisted.setdefault("provider", _sanitise_text(result.provider, key="provider"))
    persisted.setdefault("application_origin", origin_for_url(final_url))
    if job_identity is not None:
        persisted["requisition"] = job_identity.requisition
        if existing_requisition:
            persisted["observed_requisition"] = existing_requisition
    if existing_form_identity:
        persisted.setdefault("form_identity", existing_form_identity)
    return persisted


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    """Authoritative input supplied to a source-target resolver.

    The context deliberately contains the stored source URL and identity only.
    It has no caller-controlled destination field.  A browser/Navigator
    resolver may inspect the source in a visible session and return an
    immutable :class:`TargetResolution`; it may not promote a URL hint from a
    request body.
    """

    opportunity_id: str
    application_id: str | None
    source_url: str
    employer: str
    role_title: str
    cycle: str
    provider_hint: str
    source: str
    inspection_url: str = ""
    authoritative_requisition: str = ""
    requisition_source: str = ""
    requisition_provider: str = ""
    requisition_tenant: str = ""


def _resolution_context(
    opportunity: Opportunity,
    *,
    application_id: str | None,
) -> ResolutionContext:
    source_url = opportunity.navigation_url
    raw_inspection_url = str(opportunity.application_url or source_url)
    inspection_url = validate_navigation_url(raw_inspection_url)
    inspection_provenance = (
        "application_url" if opportunity.application_url else "source_url"
    )
    identity = requisition_identity_from_url(inspection_url)
    return ResolutionContext(
        opportunity_id=opportunity.id,
        application_id=application_id,
        source_url=source_url,
        employer=opportunity.employer,
        role_title=opportunity.role_title,
        cycle=opportunity.cycle,
        provider_hint=str(opportunity.ats_type or ""),
        source=str(opportunity.source or ""),
        inspection_url=inspection_url,
        authoritative_requisition=identity.requisition if identity else "",
        requisition_source=(identity.provenance if identity else ""),
        requisition_provider=identity.provider if identity else "",
        requisition_tenant=identity.tenant if identity else "",
    )


@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    """Truthful result of one user-triggered target-resolution attempt."""

    opportunity: Opportunity
    application_id: str | None
    resolution: TargetResolution | None
    promoted: bool
    application_url: str | None
    target_status: str
    reason_codes: tuple[str, ...]
    human_handoff_required: bool
    next_action: str
    handoff: Mapping[str, object]


class NavigatorTargetResolver:
    """Production adapter for the headed Navigator source-resolution hook.

    ``ApplicationNavigator`` owns browser/page lifetime.  This adapter keeps
    the service independent of that implementation while making the required
    production wiring explicit: the Navigator must expose
    ``resolve_source(application_id)`` (or one of the documented equivalent
    names).  Only the exact application id crosses this adapter; source URLs
    stay inside the Navigator's database-backed owner thread.
    """

    _METHOD_NAMES = (
        "resolve_source",
        "start_resolution",
        "resolve_application_target",
    )

    def __init__(
        self,
        navigator: Any,
        *,
        source_capability: Any | None = None,
        headed: bool | None = None,
    ):
        self.navigator = navigator
        self.source_capability = source_capability
        self.headed = headed

    def __call__(self, context: ResolutionContext) -> Any:
        if not context.application_id:
            return None
        for name in self._METHOD_NAMES:
            method = getattr(self.navigator, name, None)
            if callable(method):
                capability = self.source_capability
                owned_capability = False
                if capability is None and bool(
                    getattr(
                        getattr(self.navigator, "settings", None),
                        "apply_click_enabled",
                        False,
                    )
                ):
                    # The feature flag creates one exact, row-bound grant for
                    # this call only.  It is revoked after Navigator has
                    # completed owner-thread teardown, regardless of result.
                    from app.services.navigator import SourceResolutionCapability

                    capability = SourceResolutionCapability.issue(
                        context.inspection_url or context.source_url
                    )
                    owned_capability = True
                try:
                    if capability is not None:
                        return method(
                            context.application_id,
                            source_capability=capability,
                            headed=bool(self.headed),
                        )
                    return method(context.application_id)
                finally:
                    if owned_capability:
                        capability.revoke()
        raise RuntimeError(
            "ApplicationNavigator has no production source-resolution hook"
        )


_HANDOFF_KEYS = frozenset(
    {
        "session_id",
        "application_id",
        "state",
        "status",
        "reason",
        "outcome",
        "next_action",
        "human_required",
        "visible",
        "headed",
        "visibility",
        "kind",
        "expires_at",
        "can_continue",
        "can_cancel",
        "no_auto_submit",
        "resumable",
    }
)


def _safe_handoff(value: Any) -> dict[str, object]:
    """Keep Navigator session metadata scalar and free of page/URL handles."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if normalise_evidence_key(key) not in {
            normalise_evidence_key(item) for item in _HANDOFF_KEYS
        }:
            continue
        if isinstance(raw_value, (str, int, float, bool)) or raw_value is None:
            if key in {"session_id", "application_id"}:
                # These are opaque routing identifiers, not page evidence or
                # bearer credentials.  They must remain exact or the UI
                # cannot resume the same owner session (and the application
                # binding still authenticates every state-changing command).
                identifier = str(raw_value or "").strip()
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identifier):
                    result[key] = identifier
                continue
            result[key] = _sanitise_text(str(raw_value)) if isinstance(raw_value, str) else raw_value
    return result


def _unverified_resolution(
    resolution: TargetResolution,
    *reason_codes: str,
) -> TargetResolution:
    """Downgrade any unproven candidate before it reaches persistence."""

    reasons = tuple(
        dict.fromkeys(
            [str(code) for code in resolution.reason_codes]
            + [str(code) for code in reason_codes]
        )
    )
    evidence = dict(resolution.evidence)
    evidence["attempted_kind"] = resolution.kind.value
    return replace(
        resolution,
        kind=TargetKind.UNRESOLVED,
        identity_verified=False,
        form_verified=False,
        reason_codes=reasons or ("target_unresolved",),
        evidence=evidence,
    )


def _identity_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _placeholder_employer(value: object) -> bool:
    return _identity_text(value) in {"", "unknown"}


def _page_proven_employer(resolution: TargetResolution) -> str:
    """Return one unambiguous employer from an otherwise verified target."""

    if not resolution.identity_verified or not resolution.verified_for_automation:
        return ""
    values = [
        str(item).strip()
        for item in _evidence_scalar_values(
            resolution.evidence,
            EMPLOYER_EVIDENCE_KEYS,
        )
        if not _placeholder_employer(item)
    ]
    identities = {_identity_text(item) for item in values if _identity_text(item)}
    if len(identities) != 1:
        return ""
    return values[0][:240]


def _mapping_nodes(value: Any, *, depth: int = 0):
    if depth > _MAX_EVIDENCE_DEPTH:
        return
    if isinstance(value, Mapping):
        yield value
        for item in list(value.values())[:_MAX_EVIDENCE_ITEMS]:
            yield from _mapping_nodes(item, depth=depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in list(value)[:_MAX_EVIDENCE_ITEMS]:
            yield from _mapping_nodes(item, depth=depth + 1)


def _candidate_binding_reason(
    resolution: TargetResolution,
    context: ResolutionContext,
) -> str:
    """Cross-bind browser proof to the DB-stored exact requisition identity."""

    evidence = resolution.evidence
    providers = list(_evidence_scalar_values(evidence, PROVIDER_EVIDENCE_KEYS))
    if any(_identity_text(item) != _identity_text(resolution.provider) for item in providers):
        return "resolution_identity_mismatch"
    if (
        context.requisition_provider
        and _identity_text(resolution.provider)
        != _identity_text(context.requisition_provider)
    ):
        return "stored_job_provider_mismatch"

    expected_requisition = str(context.authoritative_requisition or "").strip()
    if not expected_requisition:
        # Loopback fixtures retain their explicit synthetic contract, but no
        # public target may manufacture authority from its observed page.
        if bool(evidence.get("synthetic_lab")):
            return ""
        return "stored_job_identity_missing"
    requisitions = list(_evidence_scalar_values(evidence, REQUISITION_EVIDENCE_KEYS))
    if not requisitions:
        return "resolution_requisition_missing"
    if any(
        not requisitions_equal(item, expected_requisition) for item in requisitions
    ):
        return "resolution_requisition_mismatch"
    final_identity = requisition_identity_from_url(resolution.final_url)
    if final_identity is not None:
        expected_key = "\x1f".join(
            (
                str(context.requisition_provider or "").casefold(),
                str(context.requisition_tenant or "").casefold(),
                str(context.authoritative_requisition or "").casefold(),
            )
        )
        if final_identity.canonical_key != expected_key:
            return "resolution_requisition_mismatch"

    # A form target needs a real bound root/control/action observation.  A
    # synthetic flag may make the pure classifier useful in tests, but it is
    # not enough to promote an application action through this user route.
    if resolution.kind is TargetKind.APPLICATION_FORM:
        form_nodes = [
            node
            for node in _mapping_nodes(evidence)
            if any(normalise_evidence_key(key) == "form" for key in node)
            or "control_count" in node
        ]

        def has_bound_form(node: Mapping[str, Any]) -> bool:
            try:
                control_count = int(node.get("control_count", 0) or 0)
            except (TypeError, ValueError):
                return False
            return bool(
                control_count > 0
                and node.get("submit_present")
                and node.get("root_selector")
                and node.get("binding_verified")
                and str(node.get("bound_target_url") or "").strip()
                and str(node.get("bound_provider") or "").strip()
            )

        if not any(
            has_bound_form(node)
            for node in form_nodes
        ):
            return "resolution_form_evidence_missing"

    # Form actions must remain on the verified application origin.  The
    # destination itself is still the immutable final_url; this is only a
    # contradiction check and never a navigation target.
    final_origin = origin_for_url(resolution.final_url)
    for node in _mapping_nodes(evidence):
        for raw_key, value in node.items():
            if normalise_evidence_key(raw_key) not in {"action", "formaction"}:
                continue
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                if "://" in value and origin_for_url(value) != final_origin:
                    return "resolution_contract_invalid"
            except ValueError:
                return "resolution_contract_invalid"
    return ""


class TargetResolutionService:
    def __init__(self, session: Session):
        self.session = session

    def _assert_user_status_eligible(self, opportunity: Opportunity) -> None:
        # A scalar SELECT auto-flushes same-session edits and reads the current
        # database value, closing the browser-time gap without trusting a
        # potentially stale ORM relationship.
        status = self.session.scalar(
            select(Opportunity.user_status).where(Opportunity.id == opportunity.id)
        )
        reason = user_status_exclusion_reason(status)
        if reason is not None:
            raise UserStatusAutomationExcludedError(reason)

    @staticmethod
    def _apply_click_evidence(
        resolution: TargetResolution,
    ) -> Mapping[str, object] | None:
        evidence = resolution.evidence
        if not isinstance(evidence, Mapping):
            return None
        apply_click = evidence.get("apply_click")
        return apply_click if isinstance(apply_click, Mapping) else None

    @classmethod
    def _is_apply_click_destination_mismatch(
        cls,
        resolution: TargetResolution,
    ) -> bool:
        apply_click = cls._apply_click_evidence(resolution)
        if apply_click is None:
            return False
        outcome = str(apply_click.get("outcome") or "").strip()
        if outcome == "apply_click_destination_mismatch":
            return True
        verification = apply_click.get("destination_verification")
        return bool(
            apply_click.get("clicked")
            and isinstance(verification, Mapping)
            and str(verification.get("verdict") or "").casefold() == "mismatch"
        )

    @staticmethod
    def _apply_click_audit_details(
        apply_click: Mapping[str, object],
        *,
        classification: str,
    ) -> dict[str, object]:
        capability = apply_click.get("capability")
        capability = capability if isinstance(capability, Mapping) else {}
        try:
            safe_classification = TargetKind(classification).value
        except ValueError:
            safe_classification = TargetKind.UNRESOLVED.value
        return {
            "accessible_name": _sanitise_text(
                str(apply_click.get("accessible_name") or "")
            ),
            "clicked": bool(apply_click.get("clicked")),
            "capability": {
                "hostname": _sanitise_text(
                    str(capability.get("hostname") or ""), key="hostname"
                ),
                "id": _sanitise_text(
                    str(capability.get("id") or ""), key="capability_id"
                ),
                "origin": _sanitise_url(
                    str(capability.get("origin") or ""), key="origin"
                ),
                "source_url": _sanitise_url(
                    str(capability.get("source_url") or ""), key="source_url"
                ),
            },
            "classification_outcome": safe_classification,
            "outcome": _sanitise_text(str(apply_click.get("outcome") or "")),
            "page_url": _sanitise_url(
                str(apply_click.get("page_url") or ""), key="page_url"
            ),
            "pre_click_url": _sanitise_url(
                str(
                    apply_click.get("pre_click_url")
                    or apply_click.get("page_url")
                    or ""
                ),
                key="pre_click_url",
            ),
            "result_url": _sanitise_url(
                str(apply_click.get("result_url") or ""), key="result_url"
            ),
            "post_click_url": _sanitise_url(
                str(
                    apply_click.get("post_click_url")
                    or apply_click.get("result_url")
                    or ""
                ),
                key="post_click_url",
            ),
            "role": _sanitise_text(str(apply_click.get("control_role") or "")),
            "verification_verdict": _sanitise_text(
                str(apply_click.get("verification_verdict") or "")
            ),
            "context_discarded": bool(apply_click.get("context_discarded")),
        }

    def _record_apply_click_mismatch_unchanged(
        self,
        opportunity: Opportunity,
        *,
        application_id: str | None,
        resolution: TargetResolution,
        handoff: Mapping[str, object],
    ) -> ResolutionOutcome:
        """Audit a bad click while leaving every opportunity column untouched."""

        apply_click = self._apply_click_evidence(resolution)
        if apply_click is None:  # defensive; caller checks this invariant
            raise RuntimeError("Apply-click mismatch evidence is missing")
        append_audit(
            self.session,
            AuditInput(
                "target_resolver",
                "opportunity.apply_click_resolution",
                "opportunity",
                opportunity.id,
                self._apply_click_audit_details(
                    apply_click,
                    classification=opportunity.target_status,
                ),
            ),
        )
        self.session.flush()
        reason_codes = tuple(
            dict.fromkeys(
                str(item)
                for item in (
                    *resolution.reason_codes,
                    "apply_click_destination_mismatch",
                )
                if str(item)
            )
        )
        return ResolutionOutcome(
            opportunity=opportunity,
            application_id=application_id,
            resolution=resolution,
            promoted=bool(opportunity.automation_url),
            application_url=opportunity.application_url,
            target_status=opportunity.target_status,
            reason_codes=reason_codes,
            human_handoff_required=False,
            next_action=(
                "Apply destination mismatch; disposable browser context discarded; "
                "no opportunity fields were changed."
            ),
            handoff=dict(handoff),
        )

    def record_apply_click_safety_violation(
        self,
        opportunity_id: str,
        *,
        evidence: Mapping[str, object] | None = None,
    ) -> None:
        """Persist a fatal click audit without changing the opportunity row."""

        opportunity = self.session.get(Opportunity, opportunity_id)
        if opportunity is None:
            raise KeyError(opportunity_id)
        apply_click = dict(evidence) if isinstance(evidence, Mapping) else {}
        apply_click.setdefault("attempted", True)
        apply_click.setdefault("clicked", True)
        apply_click.setdefault(
            "outcome", "apply_click_submission_boundary_violation"
        )
        apply_click.setdefault("verification_verdict", "fatal_submission_boundary")
        apply_click.setdefault("context_discarded", True)
        append_audit(
            self.session,
            AuditInput(
                "target_resolver",
                "opportunity.apply_click_resolution",
                "opportunity",
                opportunity.id,
                self._apply_click_audit_details(
                    apply_click,
                    classification=str(opportunity.target_status or ""),
                ),
            ),
        )
        self.session.flush()

    @staticmethod
    def _resolver_result(raw: Any) -> tuple[TargetResolution | None, dict[str, object]]:
        """Coerce only a typed resolver result; raw URL dictionaries are forbidden."""

        if isinstance(raw, TargetResolution):
            return raw, {}
        if isinstance(raw, tuple):
            if len(raw) != 2:
                raise ValueError("Target resolver tuple must contain resolution and handoff")
            candidate, metadata = raw
            if not isinstance(metadata, Mapping):
                raise ValueError("Target resolver handoff metadata must be an object")
            if candidate is None:
                return None, _safe_handoff(metadata)
            if not isinstance(candidate, TargetResolution):
                raise ValueError(
                    "Target resolver must return TargetResolution; raw URL targets are forbidden"
                )
            return candidate, _safe_handoff(metadata)
        if isinstance(raw, Mapping):
            if any(key in raw for key in ("url", "source_url", "final_url", "application_url")):
                raise ValueError(
                    "Target resolver must return TargetResolution; raw URL targets are forbidden"
                )
            candidate = raw.get("resolution")
            metadata = raw.get("handoff") or raw.get("session") or raw
            if candidate is None:
                return None, _safe_handoff(metadata)
            if not isinstance(candidate, TargetResolution):
                raise ValueError(
                    "Target resolver must return TargetResolution; raw URL targets are forbidden"
                )
            return candidate, _safe_handoff(metadata)
        if raw is None:
            return None, {}
        # Navigator snapshots are immutable dataclass-like objects.  Accept
        # only their scalar handoff fields; never retain page/context/browser
        # objects or a raw destination attribute.
        if any(hasattr(raw, name) for name in ("session_id", "state", "status")):
            snapshot = {
                key: getattr(raw, key, None)
                for key in _HANDOFF_KEYS
                if hasattr(raw, key)
            }
            return None, _safe_handoff(snapshot)
        raise ValueError(
            "Target resolver must return TargetResolution; raw URL targets are forbidden"
        )

    def _attach_handoff(
        self,
        opportunity: Opportunity,
        *,
        application_id: str | None,
        handoff: Mapping[str, object],
        reason_codes: tuple[str, ...],
        promoted: bool,
    ) -> tuple[bool, str]:
        """Persist the next human action without storing a destination hint."""

        if promoted:
            return False, "Review the verified application destination in Navigator."
        next_action = str(
            handoff.get("next_action")
            or "Open the visible Navigator to verify the exact application destination; no submission will be made."
        )[:500]
        try:
            payload = json.loads(opportunity.resolution_evidence_json or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["human_handoff_required"] = True
        payload["next_action"] = _sanitise_text(next_action)
        payload["human_handoff"] = _safe_handoff(handoff)
        payload["application_id"] = _sanitise_text(application_id or "")
        payload["reason_codes"] = [
            _sanitise_text(str(code)) for code in dict.fromkeys(reason_codes)
        ]
        opportunity.resolution_evidence_json = json.dumps(
            payload, separators=(",", ":"), sort_keys=True
        )
        self.session.flush()
        append_audit(
            self.session,
            AuditInput(
                "user",
                "opportunity.target_resolution_handoff",
                "opportunity",
                opportunity.id,
                {
                    "application_id": _sanitise_text(application_id or ""),
                    "reason_codes": payload["reason_codes"],
                    "human_handoff_required": True,
                },
            ),
        )
        return True, next_action

    def resolve(
        self,
        opportunity_id: str,
        *,
        application_id: str | None = None,
        resolver: Callable[[ResolutionContext], Any] | Any | None = None,
        allow_not_open: bool = False,
    ) -> ResolutionOutcome:
        """Run one application-bound, user-triggered target-resolution attempt.

        ``resolver`` is an application-owned Navigator adapter.  It receives
        only :class:`ResolutionContext` and must return a typed
        :class:`TargetResolution`; callers cannot supply a destination URL.
        Any unverified, contradictory, or source-mismatched result is written
        as an unresolved attempt and routed to a persistent human handoff.
        """

        opportunity = self.session.get(Opportunity, opportunity_id)
        if opportunity is None:
            raise KeyError(f"Opportunity not found: {opportunity_id}")
        self._assert_user_status_eligible(opportunity)
        window_open = opportunity.is_open_for_applications
        window_status = str(opportunity.application_window_status or "UNKNOWN").upper()
        if not window_open and not allow_not_open:
            raise ValueError(
                "Opportunity application window is not OPEN: "
                f"{opportunity.application_window_status or 'UNKNOWN'}"
            )
        application = None
        if application_id:
            application = self.session.get(Application, application_id)
            if application is None:
                raise KeyError(f"Application not found: {application_id}")
            if application.opportunity_id != opportunity.id:
                raise ValueError("Application is not bound to this opportunity")

        context = _resolution_context(
            opportunity,
            application_id=application.id if application is not None else None,
        )
        handoff: dict[str, object] = {}
        resolution: TargetResolution | None = None
        resolver_error = ""
        if resolver is not None:
            try:
                raw = resolver(context) if callable(resolver) else resolver.resolve(context)
                resolution, handoff = self._resolver_result(raw)
            except ValueError:
                # A malformed typed result is a fail-closed resolver error.
                # Persist the unresolved attempt and human next action rather
                # than turning an internal browser/parser failure into a
                # false success or an unrecorded 500.
                resolver_error = "contract_invalid"
            except Exception as exc:  # noqa: BLE001 - browser failure is a human boundary
                from app.services.resolution_apply_click import ApplyClickSafetyViolation

                if isinstance(exc, ApplyClickSafetyViolation):
                    raise
                resolver_error = type(exc).__name__.casefold()

        if resolution is None:
            reason = "resolver_unavailable" if resolver is None else "resolver_no_verified_result"
            if resolver_error == "contract_invalid":
                reason = "resolver_contract_invalid"
            elif resolver_error:
                reason = "resolver_failed"
            resolution = TargetResolution(
                source_url=context.source_url,
                final_url=context.source_url,
                kind=TargetKind.UNRESOLVED,
                reason_codes=(reason,),
                evidence={"resolver_error": resolver_error} if resolver_error else {},
            )

        if resolution is not None and self._is_apply_click_destination_mismatch(
            resolution
        ):
            # A failed click probe is deliberately not a normal target
            # resolution attempt.  Persisting it through ``record`` would
            # overwrite the row's prior status/timestamps/evidence and would
            # make a bad destination look like a new truth.  The audit event
            # is the only durable write for this branch.
            return self._record_apply_click_mismatch_unchanged(
                opportunity,
                application_id=application.id if application is not None else application_id,
                resolution=resolution,
                handoff=handoff,
            )

        if not window_open:
            # A human may stage a real URL for a closed/not-yet-open programme,
            # but its form identity must not become an automation claim before
            # the authoritative application window is OPEN.  Keep the typed
            # observation and the human URL in the review record only.
            resolution = _unverified_resolution(
                resolution,
                f"application_window_{window_status.casefold()}",
            )

        source_matches = canonical_url_key(context.source_url) == canonical_url_key(
            resolution.source_url
        )
        employer_established = False
        if not source_matches:
            resolution = _unverified_resolution(
                resolution,
                "source_url_mismatch",
            )
        elif _placeholder_employer(context.employer):
            established_employer = _page_proven_employer(resolution)
            if not established_employer:
                resolution = _unverified_resolution(
                    resolution,
                    "employer_unprovable",
                )
            else:
                proven_context = replace(context, employer=established_employer)
                binding_reason = _candidate_binding_reason(resolution, proven_context)
                if binding_reason:
                    resolution = _unverified_resolution(resolution, binding_reason)
                else:
                    try:
                        validate_target_resolution_contract(resolution)
                    except ValueError:
                        resolution = _unverified_resolution(
                            resolution,
                            "resolution_contract_invalid",
                        )
                    else:
                        previous_employer = str(opportunity.employer or "")
                        backfill_evidence = {
                            "from_placeholder": previous_employer,
                            "established_employer": established_employer,
                            "proof": "verified_provider_identity",
                        }
                        resolution = replace(
                            resolution,
                            evidence={
                                **dict(resolution.evidence),
                                "employer_backfill": backfill_evidence,
                            },
                        )
                        opportunity.employer = established_employer
                        context = proven_context
                        employer_established = True
                        self.session.flush()
                        append_audit(
                            self.session,
                            AuditInput(
                                "target_resolver",
                                "opportunity.employer_established",
                                "opportunity",
                                opportunity.id,
                                backfill_evidence,
                            ),
                        )
        elif resolution.kind is TargetKind.BLOCKED:
            # A provider-hosted tombstone is negative evidence about this
            # exact stored source, not a candidate application target.
            pass
        elif resolution.verified_for_automation:
            binding_reason = _candidate_binding_reason(resolution, context)
            if binding_reason:
                resolution = _unverified_resolution(resolution, binding_reason)
            else:
                try:
                    validate_target_resolution_contract(resolution)
                except ValueError:
                    resolution = _unverified_resolution(
                        resolution,
                        "resolution_contract_invalid",
                    )
        elif resolution.kind in {
            TargetKind.LISTING,
            TargetKind.MULTIPLE_CANDIDATE_ROLES,
            TargetKind.JOB_DETAIL,
            TargetKind.AUTH_WALL,
            TargetKind.HUMAN_CHALLENGE,
            TargetKind.NON_HTML,
        }:
            # These are truthful observations, not failed claims that an
            # automation target was verified. Persist the observed kind.
            pass
        elif resolution.kind is TargetKind.UNRESOLVED:
            # Resolver absence/failure already has a precise reason. Do not
            # bury it under a second generic verification label.
            pass
        else:
            resolution = _unverified_resolution(
                resolution,
                "target_not_verified",
            )

        if employer_established and not resolution.verified_for_automation:
            # Defensive invariant: a backfill is inseparable from the positive
            # target proof that established it.
            raise RuntimeError("Employer backfill lost its verified target binding")

        recorded = self.record(opportunity.id, resolution)
        promoted = bool(recorded.automation_url)
        reason_codes = tuple(
            str(item)
            for item in json.loads(recorded.resolution_evidence_json or "{}").get(
                "reason_codes", ()
            )
        )
        effective_handoff = dict(handoff)
        if resolution.kind is TargetKind.BLOCKED:
            effective_handoff.setdefault(
                "next_action",
                "Find a current application target",
            )
        handoff_required, next_action = self._attach_handoff(
            recorded,
            application_id=application.id if application is not None else application_id,
            handoff=effective_handoff,
            reason_codes=reason_codes,
            promoted=promoted,
        )
        if application is not None and not promoted:
            current_state = ApplicationState(application.state)
            if current_state in {
                ApplicationState.FILLING,
                ApplicationState.READY_TO_SUBMIT,
            }:
                target_state = (
                    ApplicationState.BLOCKED
                    if resolution.kind is TargetKind.BLOCKED
                    else ApplicationState.NEEDS_USER
                )
                validate_transition(current_state, target_state)
                application.state = target_state.value
                application.next_action = next_action
                self.session.flush()
                append_audit(
                    self.session,
                    AuditInput(
                        "system",
                        "application.target_invalidated",
                        "application",
                        application.id,
                        {
                            "from": current_state.value,
                            "to": target_state.value,
                            "target_status": recorded.target_status,
                            "reason_codes": list(reason_codes),
                        },
                    ),
                )
        # Re-read the authoritative row before returning it.  This closes the
        # request-side race where a Navigator/Apply caller could observe a
        # stale ORM object after the verified target was persisted.
        self.session.flush()
        self.session.refresh(recorded)
        promoted = bool(recorded.automation_url)
        return ResolutionOutcome(
            opportunity=recorded,
            application_id=application.id if application is not None else application_id,
            resolution=resolution,
            promoted=promoted,
            application_url=recorded.application_url,
            target_status=recorded.target_status,
            reason_codes=reason_codes,
            human_handoff_required=handoff_required,
            next_action=next_action,
            handoff=effective_handoff,
        )

    # Descriptive public alias used by source/listing callers.  Keeping the
    # short ``resolve`` name makes the service convenient in tests and older
    # integrations while this name makes the source-only boundary explicit.
    resolve_from_source = resolve

    def record_failure(
        self,
        opportunity_id: str,
        *,
        reason_code: str,
        error_type: str = "",
    ) -> Opportunity:
        """Persist one fail-closed attempt without retaining exception text."""

        opportunity = self.session.get(Opportunity, opportunity_id)
        if opportunity is None:
            raise KeyError(opportunity_id)
        self._assert_user_status_eligible(opportunity)
        evidence = {"resolver_error": error_type.casefold()[:120]} if error_type else {}
        return self.record(
            opportunity.id,
            TargetResolution(
                source_url=opportunity.navigation_url,
                final_url=opportunity.navigation_url,
                kind=TargetKind.UNRESOLVED,
                reason_codes=(str(reason_code)[:120],),
                evidence=evidence,
            ),
        )

    def record(self, opportunity_id: str, result: TargetResolution) -> Opportunity:
        opportunity = self.session.get(Opportunity, opportunity_id)
        if opportunity is None:
            raise KeyError(opportunity_id)
        self._assert_user_status_eligible(opportunity)

        now = datetime.now(timezone.utc)
        source_matches = (
            canonical_url_key(opportunity.url) == canonical_url_key(result.source_url)
        )
        reason_codes = list(result.reason_codes)
        if not source_matches:
            reason_codes.append("source_url_mismatch")
        safe_reason_codes = [_sanitise_text(str(code)) for code in reason_codes]

        attempt_kind = TargetKind.MISMATCH if not source_matches else result.kind
        previous_target_status = opportunity.target_status
        previous_application_url = opportunity.application_url
        previous_resolved_ats_type = opportunity.resolved_ats_type
        previous_resolved_at = opportunity.resolved_at
        inspection_url = validate_navigation_url(
            str(previous_application_url or opportunity.navigation_url)
        )
        inspection_provenance = (
            "application_url" if previous_application_url else "source_url"
        )
        job_identity = requisition_identity_from_url(inspection_url)
        previous_evidence: dict[str, Any] = {}
        try:
            decoded_evidence = json.loads(opportunity.resolution_evidence_json or "{}")
            if isinstance(decoded_evidence, dict):
                previous_evidence = decoded_evidence
        except (TypeError, ValueError):
            previous_evidence = {}
        previous_verified_target = previous_evidence.get("verified_target")
        if not isinstance(previous_verified_target, dict):
            previous_verified_target = {}
        try:
            previous_kind = TargetKind(previous_target_status)
        except ValueError:
            previous_kind = TargetKind.UNRESOLVED
        try:
            evidence_verified_kind = TargetKind(
                str(previous_verified_target.get("target_status") or "")
            )
        except ValueError:
            evidence_verified_kind = TargetKind.UNRESOLVED
        retained_status = (
            previous_target_status
            if previous_kind.automation_eligible
            else evidence_verified_kind.value
            if evidence_verified_kind.automation_eligible
            else previous_target_status
        )
        record_context = _resolution_context(opportunity, application_id=None)
        binding_reason = (
            _candidate_binding_reason(result, record_context)
            if result.verified_for_automation
            else ""
        )
        if binding_reason:
            safe_reason_codes.append(_sanitise_text(binding_reason))
            attempt_kind = TargetKind.UNRESOLVED
        failed_attempt = attempt_kind in {TargetKind.MISMATCH, TargetKind.UNRESOLVED}
        preserve_verified_target = bool(
            failed_attempt
            and previous_application_url
            and previous_resolved_at is not None
            and (
                previous_kind.automation_eligible
                or evidence_verified_kind.automation_eligible
                or bool(previous_resolved_ats_type)
            )
        )
        promote = bool(
            result.verified_for_automation
            and source_matches
            and attempt_kind is not TargetKind.MISMATCH
            and not binding_reason
        )
        persisted_evidence = _stamp_verified_identity(
            result.evidence,
            result=result,
            opportunity=opportunity,
            promote=promote,
            job_identity=job_identity,
            inspection_url=inspection_url,
            inspection_provenance=inspection_provenance,
        )
        opportunity.resolution_attempted_at = now
        if preserve_verified_target:
            # A bad redirect/DOM observation is a separate attempt, not proof
            # that the already verified target disappeared. Keep the last
            # verified URL/provider/time intact, but mark the current target
            # ineligible until a fresh verification succeeds.
            opportunity.target_status = attempt_kind.value
            opportunity.resolved_ats_type = previous_resolved_ats_type
            opportunity.application_url = previous_application_url
            opportunity.resolved_at = previous_resolved_at
        else:
            opportunity.target_status = attempt_kind.value
            opportunity.resolved_ats_type = _sanitise_text(result.provider)[:80]
            opportunity.application_url = (
                validate_navigation_url(result.final_url)
                if promote
                else previous_application_url
                if previous_application_url and previous_resolved_at is None
                else None
            )
            if promote and opportunity.application_url:
                opportunity.application_url_provenance = "RESOLVER_SUPPLIED"
            opportunity.resolved_at = now if promote else None
        evidence_payload = {
            "source_url": _contract_url(result.source_url),
            "final_url": _contract_url(result.final_url),
            "kind": attempt_kind.value,
            "attempt_kind": attempt_kind.value,
            "attempt_status": attempt_kind.value,
            "provider": _sanitise_text(result.provider),
            "identity_verified": bool(result.identity_verified),
            "form_verified": bool(result.form_verified),
            "promoted": promote,
            "preserved_verified_target": preserve_verified_target,
            "previous_target_status": _sanitise_text(previous_target_status),
            "verified_target": (
                {
                    "application_url": _contract_url(previous_application_url),
                    "resolved_ats_type": _sanitise_text(previous_resolved_ats_type),
                    "resolved_at": previous_resolved_at.isoformat() if previous_resolved_at else "",
                    "target_status": _sanitise_text(retained_status),
                }
                if preserve_verified_target
                else (
                    {
                        "application_url": _contract_url(result.final_url),
                        "resolved_ats_type": _sanitise_text(result.provider),
                        "resolved_at": now.isoformat(),
                        "target_status": result.kind.value,
                    }
                    if promote
                    else previous_verified_target or None
                )
            ),
            "reason_codes": safe_reason_codes,
            "evidence": persisted_evidence,
        }
        employer_backfill = persisted_evidence.get("employer_backfill")
        if isinstance(employer_backfill, Mapping):
            evidence_payload["employer_backfill"] = dict(employer_backfill)
        opportunity.resolution_evidence_json = json.dumps(
            evidence_payload, separators=(",", ":"), sort_keys=True
        )
        self.session.flush()
        audit_details: dict[str, Any] = {
            "kind": attempt_kind.value,
            "provider": _sanitise_text(result.provider),
            "promoted": promote,
            "reason_codes": safe_reason_codes,
        }
        apply_hop = persisted_evidence.get("apply_hop")
        role_identity = (
            apply_hop.get("role_identity")
            if isinstance(apply_hop, Mapping)
            else None
        )
        if isinstance(role_identity, Mapping):
            safe_role_identity = _sanitise_evidence(role_identity)
            if isinstance(safe_role_identity, Mapping):
                audit_details["role_identity"] = dict(safe_role_identity)
        append_audit(
            self.session,
            AuditInput(
                "target_resolver",
                "opportunity.target_resolved",
                "opportunity",
                opportunity.id,
                audit_details,
            ),
        )
        apply_click = persisted_evidence.get("apply_click")
        if isinstance(apply_click, Mapping):
            append_audit(
                self.session,
                AuditInput(
                    "target_resolver",
                    "opportunity.apply_click_resolution",
                    "opportunity",
                    opportunity.id,
                    self._apply_click_audit_details(
                        apply_click,
                        classification=str(
                            apply_click.get("destination_kind") or attempt_kind.value
                        ),
                    ),
                ),
            )
        return opportunity
