"""Receipt parsing and strict post-click correlation contracts."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Page

from app.automation.types import Receipt


@dataclass(frozen=True, slots=True)
class ReceiptEvidence:
    """Immutable pre/post-click evidence for one bound submission.

    The first four fields preserve the public constructor. Strict correlation
    additionally requires ``baseline_captured``, exact target identity, exact
    intent ID/nonce, and a successful request/response pair. DOM booleans are
    context only; actual fresh text/reference evidence is mandatory.
    """

    url_before_click: str
    dom_had_reference: bool = False
    dom_confirmation_text_present: bool = False
    reference_seen_before_click: bool = False
    final_url: str = ""
    request_url: str = ""
    response_status: int | None = None
    response_url: str = ""
    request_method: str = ""
    request_id: str = ""
    response_request_id: str = ""
    navigation_url: str = ""
    navigation_kind: str = ""
    baseline_captured: bool = False
    page_id: str = ""
    frame_url: str = ""
    root_selector: str = ""
    control_selector: str = ""
    control_fingerprint: str = ""
    form_action: str = ""
    target_method: str = ""
    bound_target_fingerprint: str = ""
    target_fingerprint: str = ""
    bound_target_id: str = ""
    bound_target: str = ""
    submission_target_fingerprint: str = ""
    target_url: str = ""
    bound_intent_id: str = ""
    intent_id: str = ""
    bound_intent: str = ""
    bound_intent_nonce: str = ""
    intent_nonce: str = ""
    request_target_matches: bool | None = None
    response_target_matches: bool | None = None
    navigation_target_matches: bool | None = None
    provider_success: bool | None = None
    request_succeeded: bool | None = None
    response_succeeded: bool | None = None
    reference: str = ""
    provider_reference: str = ""
    confirmation_text: str = ""
    dom_confirmation_text: str = ""
    page_crashed: bool = False
    timed_out: bool = False
    post_click_uncertain: bool = False
    submission_control_fingerprint: str = ""
    bound_control_fingerprint: str = ""
    bound_page_id: str = ""
    bound_frame_url: str = ""
    bound_root_selector: str = ""
    submission_control_selector: str = ""
    target_action: str = ""
    target_destination: str = ""
    destination: str = ""
    provider: str = ""
    target_provider: str = ""
    bound_provider: str = ""


_CONFIRMATION_PATTERNS = (
    r"application (?:has been |was )?submitted",
    r"thank you for applying",
    r"application (?:is )?complete",
    r"we (?:have )?received your application",
)
_REFERENCE_PATTERNS = (
    re.compile(
        r"\breference(?:\s+(?:number|id|code))?\s*[:#-]*\s*"
        r"([A-Z0-9][A-Z0-9_-]{3,})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:application|confirmation)\s+(?:number|id|code)\s*[:#-]*\s*"
        r"([A-Z0-9][A-Z0-9_-]{3,})",
        re.IGNORECASE,
    ),
)


def parse_receipt_text(text: str, url: str) -> Receipt | None:
    compact = " ".join(text.split())
    if not any(
        re.search(pattern, compact, flags=re.IGNORECASE)
        for pattern in _CONFIRMATION_PATTERNS
    ):
        return None
    reference = ""
    for pattern in _REFERENCE_PATTERNS:
        match = pattern.search(compact)
        if match:
            reference = match.group(1)
            break
    return Receipt(confirmation_text=compact[:2000], url=url, reference=reference)


def _normalise_receipt_url(value: str) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value)
        if parts.username is not None or parts.password is not None:
            return ""
        hostname = parts.hostname or ""
        port = parts.port
    except (TypeError, ValueError):
        return ""
    if not parts.scheme or not hostname:
        return ""
    scheme = parts.scheme.casefold()
    hostname = hostname.casefold().rstrip(".")
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if port is not None and port != {"http": 80, "https": 443}.get(scheme):
        hostname = f"{hostname}:{port}"
    return urlunsplit(
        (
            scheme,
            hostname,
            parts.path.rstrip("/") or "/",
            parts.query,
            "",
        )
    )


def receipt_has_submission_evidence(receipt: Receipt, submission_url: str) -> bool:
    """Legacy parser helper; new code must use ``receipt_is_correlated``."""

    if receipt.reference.strip():
        return True
    return _normalise_receipt_url(receipt.url) != _normalise_receipt_url(submission_url)


def detect_receipt(page: Page) -> Receipt | None:
    return parse_receipt_text(page.locator("body").inner_text(), page.url)


def _value(obj: Any, *names: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return None
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


_TARGET_REQUIRED_FIELDS = frozenset(
    {
        "target_fingerprint",
        "control_fingerprint",
        "page_id",
        "frame_url",
        "root_selector",
        "control_selector",
        "form_action",
        "method",
        "destination",
        "provider",
    }
)
_URL_TARGET_FIELDS = frozenset({"frame_url", "form_action", "destination"})
_MAX_CORRELATION_ID_LENGTH = 256
_CORRELATION_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"
)


def _alias_value(
    obj: Any,
    names: tuple[str, ...],
    *,
    url: bool = False,
    casefold: bool = False,
) -> tuple[str, bool]:
    """Return one alias value and whether supplied aliases conflict."""

    values: list[str] = []
    for name in names:
        value = _value(obj, name)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            return "", True
        values.append(value)
    if not values:
        return "", False
    if url:
        canonical = {_normalise_receipt_url(value) for value in values}
        if "" in canonical or len(canonical) != 1:
            return "", True
    elif len({value.casefold() if casefold else value for value in values}) != 1:
        return "", True
    return values[0], False


def _target_binding(bound_target: Any) -> dict[str, str] | None:
    """Extract a complete immutable target, rejecting scalar/alias ambiguity."""

    if bound_target is None or isinstance(bound_target, (str, bytes, int, float, bool)):
        return None
    aliases: dict[str, tuple[tuple[str, ...], bool]] = {
        "target_fingerprint": (
            (
                "target_fingerprint",
                "bound_target_fingerprint",
                "submission_target_fingerprint",
                "bound_target_id",
                "bound_target",
                "fingerprint",
            ),
            False,
        ),
        "control_fingerprint": (
            (
                "control_fingerprint",
                "bound_control_fingerprint",
                "submission_control_fingerprint",
                "control_id",
                "bound_control_id",
            ),
            False,
        ),
        "page_id": (("page_id", "bound_page_id"), False),
        "frame_url": (("frame_url", "bound_frame_url"), True),
        "root_selector": (("root_selector", "bound_root_selector"), False),
        "control_selector": (("control_selector", "submission_control_selector"), False),
        "form_action": (("form_action", "action", "target_action"), True),
        "method": (("method", "target_method"), False),
        "destination": (
            ("destination", "target_destination", "target_url", "bound_target_url", "receipt_target", "receipt_url"),
            True,
        ),
        "provider": (("provider", "target_provider", "bound_provider", "provider_name", "ats_provider"), False),
    }
    result: dict[str, str] = {}
    for key, (names, is_url) in aliases.items():
        value, conflict = _alias_value(
            bound_target,
            names,
            url=is_url,
            casefold=key == "method" or key == "provider",
        )
        if conflict:
            return None
        if value:
            if key == "method":
                result[key] = value.upper()
            elif key == "provider":
                result[key] = value.casefold()
            else:
                result[key] = value
    return result


def _intent_binding(bound_intent: Any) -> dict[str, str] | None:
    if bound_intent is None or isinstance(bound_intent, (str, bytes, int, float, bool)):
        return None
    intent_id, id_conflict = _alias_value(
        bound_intent,
        ("intent_id", "bound_intent_id", "id", "bound_intent"),
    )
    nonce, nonce_conflict = _alias_value(
        bound_intent,
        ("intent_nonce", "bound_intent_nonce", "nonce"),
    )
    if id_conflict or nonce_conflict:
        return None
    result: dict[str, str] = {}
    if intent_id:
        result["intent_id"] = intent_id
    if nonce:
        result["nonce"] = nonce
    return result


def _evidence_target(evidence: ReceiptEvidence) -> dict[str, str] | None:
    return _target_binding(evidence)


def _evidence_intent(evidence: ReceiptEvidence) -> dict[str, str] | None:
    return _intent_binding(evidence)


def _normalise_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).casefold()


def _receipt_aliases_consistent(evidence: ReceiptEvidence) -> bool:
    """Reject conflicting alternate captures before any alias is selected."""

    for primary, alias in (
        (evidence.reference, evidence.provider_reference),
        (evidence.confirmation_text, evidence.dom_confirmation_text),
    ):
        values = []
        for value in (primary, alias):
            if value in (None, ""):
                continue
            if not isinstance(value, str):
                return False
            values.append(_normalise_text(value))
        if len(values) == 2 and values[0] != values[1]:
            return False
    return True


def _valid_correlation_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_CORRELATION_ID_LENGTH
        and _CORRELATION_ID_PATTERN.fullmatch(value) is not None
    )


def _new_reference(before: ReceiptEvidence, after: ReceiptEvidence) -> bool:
    before_ref = _normalise_text(before.reference or before.provider_reference)
    after_ref = _normalise_text(after.reference or after.provider_reference)
    if not after_ref or after.reference_seen_before_click or before.reference_seen_before_click:
        return False
    return after_ref != before_ref


def _new_confirmation(before: ReceiptEvidence, after: ReceiptEvidence) -> bool:
    before_text = _normalise_text(before.confirmation_text or before.dom_confirmation_text)
    after_text = _normalise_text(after.confirmation_text or after.dom_confirmation_text)
    if not after_text or before_text == after_text:
        return False
    if not any(re.search(pattern, after_text, flags=re.IGNORECASE) for pattern in _CONFIRMATION_PATTERNS):
        return False
    return after_text != before_text


def _target_url_matches(url: str, expected_url: str) -> bool:
    return bool(url and expected_url) and _normalise_receipt_url(url) == _normalise_receipt_url(expected_url)


def _binding_matches(
    before: ReceiptEvidence,
    after: ReceiptEvidence,
    *,
    bound_target: Any,
    bound_intent: Any,
) -> bool:
    expected_target = _target_binding(bound_target)
    if expected_target is None or not _TARGET_REQUIRED_FIELDS.issubset(expected_target):
        return False
    expected_intent = _intent_binding(bound_intent)
    if expected_intent is None or set(expected_intent) != {"intent_id", "nonce"}:
        return False
    baseline_target = _evidence_target(before)
    observed_target = _evidence_target(after)
    baseline_intent = _evidence_intent(before)
    observed_intent = _evidence_intent(after)
    if baseline_target is None or observed_target is None:
        return False
    if baseline_intent is None or observed_intent is None:
        return False
    if not _TARGET_REQUIRED_FIELDS.issubset(baseline_target):
        return False
    if not _TARGET_REQUIRED_FIELDS.issubset(observed_target):
        return False
    for key, expected in expected_target.items():
        if key in _URL_TARGET_FIELDS:
            baseline_matches = _target_url_matches(baseline_target.get(key, ""), expected)
            observed_matches = _target_url_matches(observed_target.get(key, ""), expected)
        else:
            baseline_matches = baseline_target.get(key) == expected
            observed_matches = observed_target.get(key) == expected
        if not baseline_matches or not observed_matches:
            return False
    if not _target_url_matches(after.request_url, expected_target["form_action"]):
        return False
    for key, expected in expected_intent.items():
        if baseline_intent.get(key) != expected or observed_intent.get(key) != expected:
            return False
    if before.request_url and not _target_url_matches(after.request_url, before.request_url):
        return False
    return True


def receipt_is_correlated(
    before: ReceiptEvidence,
    after: ReceiptEvidence,
    *,
    bound_target: Any = None,
    bound_intent: Any = None,
    navigation_only: bool = False,
    submission_target: Any = None,
    intent: Any = None,
    target: Any = None,
    intent_id: str = "",
    intent_nonce: str = "",
    expected_final_url: str = "",
) -> bool:
    """Return true only for fresh, exact, successful post-click evidence."""

    if bound_target is None:
        bound_target = submission_target if submission_target is not None else target
    if bound_intent is None:
        if intent is not None:
            bound_intent = intent
        elif intent_id or intent_nonce:
            bound_intent = {"id": intent_id, "nonce": intent_nonce}
    expected_target = _target_binding(bound_target)
    expected_intent = _intent_binding(bound_intent)
    if expected_target is None or not _TARGET_REQUIRED_FIELDS.issubset(expected_target):
        return False
    if expected_intent is None or set(expected_intent) != {"intent_id", "nonce"}:
        return False
    if (
        not before.baseline_captured
        or not before.url_before_click
        or not after.url_before_click
        or _normalise_receipt_url(before.url_before_click)
        != _normalise_receipt_url(after.url_before_click)
    ):
        return False
    if not _receipt_aliases_consistent(before) or not _receipt_aliases_consistent(after):
        return False
    # A baseline that already contains any reference or confirmation wording
    # cannot prove that the same-looking or merely different wording arrived
    # after this click.  Boolean DOM flags are also treated as pre-existing
    # evidence and therefore fail closed.
    if (
        before.dom_had_reference
        or before.dom_confirmation_text_present
        or _normalise_text(before.reference)
        or _normalise_text(before.provider_reference)
        or _normalise_text(before.confirmation_text)
        or _normalise_text(before.dom_confirmation_text)
    ):
        return False
    if before.reference_seen_before_click or after.reference_seen_before_click:
        return False
    if any((after.page_crashed, after.timed_out, after.post_click_uncertain)):
        return False
    if navigation_only or str(after.navigation_kind or "").casefold() in {
        "intermediate",
        "step",
        "next",
        "progress",
    }:
        return False
    if not _binding_matches(before, after, bound_target=bound_target, bound_intent=bound_intent):
        return False
    if after.request_target_matches is False or after.response_target_matches is False:
        return False
    if after.navigation_target_matches is False:
        return False
    request_method = str(after.request_method or "").casefold()
    if request_method not in {"post", "put", "patch", "delete"}:
        return False
    if after.target_method and after.target_method.casefold() != request_method:
        return False
    if (
        not after.request_url
        or not after.response_url
        or not _target_url_matches(after.response_url, after.request_url)
    ):
        return False
    if before.request_id or before.response_request_id:
        if (
            not _valid_correlation_id(before.request_id)
            or not _valid_correlation_id(before.response_request_id)
            or before.request_id != before.response_request_id
        ):
            return False
    if (
        not _valid_correlation_id(after.request_id)
        or not _valid_correlation_id(after.response_request_id)
        or after.request_id != after.response_request_id
    ):
        return False
    if before.request_id and after.request_id == before.request_id:
        return False
    if before.response_request_id and after.response_request_id == before.response_request_id:
        return False
    try:
        response_status = int(after.response_status) if after.response_status is not None else 0
    except (TypeError, ValueError):
        return False
    if not 200 <= response_status < 300:
        return False
    if after.provider_success is False or after.request_succeeded is False or after.response_succeeded is False:
        return False
    if expected_final_url and not (after.navigation_url or after.final_url):
        return False
    navigation_urls = [url for url in (after.navigation_url, after.final_url) if url]
    if navigation_urls:
        if len({_normalise_receipt_url(url) for url in navigation_urls}) != 1:
            return False
        expected_navigation = expected_final_url or expected_target.get("destination", "")
        if not expected_navigation or any(
            not _target_url_matches(url, expected_navigation) for url in navigation_urls
        ):
            return False
    if not (_new_reference(before, after) or _new_confirmation(before, after)):
        return False
    return True
