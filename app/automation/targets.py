from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
import json
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.domain.targets import TargetKind, canonical_url_key, validate_navigation_url


# Provider identity is deliberately an allow-list.  A host is not trusted
# merely because a string contains (or ends with) a provider name.  Workday is
# the only provider here with tenant hosts, so its host grammar is explicit.
_TRUSTED_PROVIDER_HOSTS: dict[str, frozenset[str]] = {
    "greenhouse": frozenset(
        {
            "boards.greenhouse.io",
            "job-boards.greenhouse.io",
            "job-boards.eu.greenhouse.io",
            "boards-api.greenhouse.io",
        }
    ),
    "lever": frozenset({"jobs.lever.co"}),
    "smartrecruiters": frozenset({"jobs.smartrecruiters.com"}),
    "workable": frozenset({"apply.workable.com"}),
}
_WORKDAY_HOST = re.compile(
    r"^[a-z0-9][a-z0-9-]*\.wd[0-9]+\.(?:myworkdayjobs|myworkdaysite)\.com$"
)
_KNOWN_PROVIDERS = frozenset({"greenhouse", "lever", "workday", "smartrecruiters", "workable"})
_MAX_EVIDENCE_DEPTH = 20


def _normalise_provider(value: object) -> str:
    return str(value or "").strip().casefold()


def _provider_for_host(host: str) -> str:
    host = host.casefold().rstrip(".")
    for provider, hosts in _TRUSTED_PROVIDER_HOSTS.items():
        if host in hosts:
            return provider
    if _WORKDAY_HOST.fullmatch(host):
        return "workday"
    return ""


def trusted_provider_for_url(url: str) -> str:
    """Return the provider proven by an exact trusted host, if any.

    This helper intentionally does not use broad suffix matching.  In
    particular, ``jobs.lever.co.attacker.test`` and ``greenhouse.io.example``
    are not provider hosts.  The narrow Workday grammar accepts only a tenant
    label, ``wd<number>``, and the exact Workday registrable domain.
    """

    try:
        parts = urlsplit(url)
        if parts.scheme.casefold() != "https" or parts.username or parts.password:
            return ""
        if parts.port not in {None, 443}:
            return ""
        host = (parts.hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""
    return _provider_for_host(host)


def _html_providers(html: str) -> frozenset[str]:
    providers = {
        _normalise_provider(match)
        for match in re.findall(
            r"data-ats\s*=\s*['\"]\s*([a-z][a-z0-9_-]*)\s*['\"]",
            html,
            flags=re.IGNORECASE,
        )
    }
    return frozenset(provider for provider in providers if provider in _KNOWN_PROVIDERS)


def _freeze_evidence(value: Any, *, depth: int = 0) -> Any:
    """Recursively convert evidence into immutable, bounded containers."""

    if depth > _MAX_EVIDENCE_DEPTH:
        return "[truncated]"
    if isinstance(value, MappingABC):
        return MappingProxyType(
            {
                str(key)[:120]: _freeze_evidence(item, depth=depth + 1)
                for key, item in list(value.items())[:200]
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_evidence(item, depth=depth + 1) for item in value[:200])
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_evidence(item, depth=depth + 1) for item in list(value)[:200])
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    return str(value)[:2000]


def _immutable_evidence(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    frozen = _freeze_evidence(value or {})
    if isinstance(frozen, MappingABC):
        return frozen
    return MappingProxyType({})


def _iter_evidence(value: Any, *, depth: int = 0, seen: set[int] | None = None):
    if depth > _MAX_EVIDENCE_DEPTH:
        return
    if seen is None:
        seen = set()
    if isinstance(value, MappingABC):
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        for key, item in value.items():
            yield str(key), item
            yield from _iter_evidence(item, depth=depth + 1, seen=seen)
    elif isinstance(value, (list, tuple, set, frozenset)):
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        for item in list(value)[:200]:
            yield from _iter_evidence(item, depth=depth + 1, seen=seen)


def _evidence_value(value: Mapping[str, Any], aliases: frozenset[str]) -> str:
    for key, item in _iter_evidence(value):
        normalised_key = re.sub(r"[^a-z0-9]", "", key.casefold())
        if normalised_key not in aliases:
            continue
        if isinstance(item, bool):
            if item:
                return "true"
            continue
        text = str(item).strip()
        if text and text.casefold() not in {"none", "null", "false"}:
            return text
    return ""


# These are the one shared vocabulary for persisted target proof.  Keys are
# compared after removing punctuation/case, so the spelling variants below
# deliberately use their compact form.
PROVIDER_EVIDENCE_KEYS = frozenset(
    {
        "provider",
        "ats",
        "atstype",
        "atsverified",
        "atsidentity",
        "atsprovider",
        "providerverified",
        "provideridentity",
        "boundprovider",
    }
)
EMPLOYER_EVIDENCE_KEYS = frozenset(
    {
        "employer",
        "employername",
        "employerverified",
        "company",
        "companyname",
        "boundemployer",
    }
)
ROLE_EVIDENCE_KEYS = frozenset(
    {
        "role",
        "roletitle",
        "roleverified",
        "jobtitle",
        "position",
        "positiontitle",
        "boundrole",
    }
)
REQUISITION_EVIDENCE_KEYS = frozenset(
    {
        "requisition",
        "requisitionid",
        "jobid",
        "jobidentifier",
        "postingid",
        "req",
        "reqid",
        "boundrequisition",
    }
)
PATH_EVIDENCE_KEYS = frozenset(
    {
        "path",
        "targetpath",
        "applicationpath",
        "boundtargetpath",
        "requisitionpath",
        "boundpath",
    }
)
FORM_IDENTITY_EVIDENCE_KEYS = frozenset(
    {
        "form",
        "formid",
        "formidentity",
        "applicationform",
        "boundformidentity",
        "formhandle",
    }
)
ROOT_SELECTOR_EVIDENCE_KEYS = frozenset(
    {"formselector", "applicationroot", "rootselector", "root"}
)
ROOT_TOKEN_EVIDENCE_KEYS = frozenset({"roottoken", "boundroottoken"})
FRAME_URL_EVIDENCE_KEYS = frozenset({"frameurl", "boundframeurl"})
BOUND_TARGET_URL_EVIDENCE_KEYS = frozenset({"boundtargeturl"})
ORIGIN_EVIDENCE_KEYS = frozenset(
    {
        "origin",
        "applicationorigin",
        "verifiedorigin",
        "canonicalorigin",
        "boundorigin",
    }
)

# Backward-compatible private names used by the classifier in this module.
_ATS_EVIDENCE_KEYS = PROVIDER_EVIDENCE_KEYS
_EMPLOYER_EVIDENCE_KEYS = EMPLOYER_EVIDENCE_KEYS
_ROLE_EVIDENCE_KEYS = ROLE_EVIDENCE_KEYS
_REQUISITION_EVIDENCE_KEYS = REQUISITION_EVIDENCE_KEYS
_FORM_EVIDENCE_KEYS = FORM_IDENTITY_EVIDENCE_KEYS | ROOT_SELECTOR_EVIDENCE_KEYS


def normalise_evidence_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def canonical_target_contract_url(value: str) -> str:
    """Canonicalize a persisted target binding without changing navigation URLs."""

    validated = validate_navigation_url(value)
    parts = urlsplit(validated)
    scheme = parts.scheme.casefold()
    hostname = (parts.hostname or "").casefold().rstrip(".")
    port = parts.port
    if port in {80 if scheme == "http" else 443}:
        port = None
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = urlencode(parse_qsl(parts.query, keep_blank_values=True), doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def _custom_identity_evidence(
    provider: str, evidence: Mapping[str, Any], html: str = ""
) -> bool:
    """Require independent ATS + employer/role + requisition/form proof."""

    provider = _normalise_provider(provider)
    markers = _html_providers(html)
    ats = _evidence_value(evidence, _ATS_EVIDENCE_KEYS)
    employer = _evidence_value(evidence, _EMPLOYER_EVIDENCE_KEYS)
    role = _evidence_value(evidence, _ROLE_EVIDENCE_KEYS)
    requisition_or_form = _evidence_value(
        evidence, _REQUISITION_EVIDENCE_KEYS | _FORM_EVIDENCE_KEYS
    )
    # ``custom_domain_verified`` is written only by classify_target after the
    # HTML marker and all identity fields have been checked.  It also makes a
    # manually constructed resolution fail closed unless its caller presents
    # the same explicit proof.
    ats_ok = ats.casefold() == provider if ats else False
    marker_ok = bool(evidence.get("custom_domain_verified")) or (
        bool(markers) and provider in markers
    ) or ats_ok
    return bool(provider and marker_ok and ats_ok and employer and role and requisition_or_form)


def _trusted_identity_evidence(
    evidence: Mapping[str, Any], provider: str = ""
) -> bool:
    structured = _evidence_value(evidence, frozenset({"structuredfeed"}))
    if structured:
        structured_provider = _normalise_provider(structured.split(":", 1)[0])
        if not provider or structured_provider == _normalise_provider(provider):
            return True
    # A verified form handle is independent identity evidence for a trusted
    # provider host; a bare form id/selector is not.  Require the inspection
    # counts and visible final control that the adapter records.
    for key, item in _iter_evidence(evidence):
        normalised_key = re.sub(r"[^a-z0-9]", "", key.casefold())
        if normalised_key not in _FORM_EVIDENCE_KEYS or not isinstance(item, MappingABC):
            continue
        try:
            control_count = int(item.get("control_count", 0) or 0)
        except (TypeError, ValueError):
            control_count = 0
        if (
            control_count > 0
            and bool(item.get("submit_present"))
            and bool(item.get("root_selector"))
            and bool(item.get("binding_verified"))
            and bool(str(item.get("bound_target_url") or "").strip())
            and bool(str(item.get("bound_provider") or "").strip())
            and bool(str(item.get("root_token") or item.get("form_identity") or "").strip())
        ):
            return True
    return bool(
        _evidence_value(evidence, _EMPLOYER_EVIDENCE_KEYS)
        and _evidence_value(evidence, _ROLE_EVIDENCE_KEYS)
        and _evidence_value(evidence, _REQUISITION_EVIDENCE_KEYS | _FORM_EVIDENCE_KEYS)
    )


def _is_loopback_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class FormHandle:
    page_id: str
    frame_url: str
    root_selector: str
    provider: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.page_id or not self.root_selector:
            raise ValueError("A form handle requires page and root identities")
        if not self.frame_url:
            raise ValueError("A form handle requires a frame URL")
        object.__setattr__(self, "provider", _normalise_provider(self.provider))
        object.__setattr__(self, "evidence", _immutable_evidence(self.evidence))


@dataclass(frozen=True, slots=True)
class SubmissionTarget:
    page_id: str
    frame_url: str
    root_selector: str
    provider: str
    control_selector: str
    control_fingerprint: str
    form_action: str
    method: str
    provider_step: str = ""
    employer: str = ""
    role: str = ""
    requisition: str = ""
    destination: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.page_id or not self.root_selector or not self.control_selector:
            raise ValueError("A submission target requires page, root, and control identities")
        if not self.frame_url:
            raise ValueError("A submission target requires a frame URL")
        object.__setattr__(self, "provider", _normalise_provider(self.provider))
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "evidence", _immutable_evidence(self.evidence))


@dataclass(frozen=True, slots=True)
class TargetResolution:
    source_url: str
    final_url: str
    kind: TargetKind
    provider: str = ""
    identity_verified: bool = False
    form_verified: bool = False
    reason_codes: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_url", validate_navigation_url(self.source_url))
        object.__setattr__(self, "final_url", validate_navigation_url(self.final_url))
        object.__setattr__(self, "provider", _normalise_provider(self.provider))
        object.__setattr__(self, "reason_codes", tuple(dict.fromkeys(self.reason_codes)))
        object.__setattr__(self, "evidence", _immutable_evidence(self.evidence))
        if self.form_verified and self.kind is not TargetKind.APPLICATION_FORM:
            raise ValueError("Form verification is valid only for APPLICATION_FORM")

    @property
    def verified_for_automation(self) -> bool:
        if not self.kind.automation_eligible or not self.identity_verified:
            return False
        if self.kind is TargetKind.APPLICATION_FORM and not self.form_verified:
            return False
        trusted_provider = trusted_provider_for_url(self.final_url)
        origin_provider = _provider_for_host((urlsplit(self.final_url).hostname or ""))
        if origin_provider and not trusted_provider:
            return False
        if trusted_provider:
            return self.provider == trusted_provider and _trusted_identity_evidence(
                self.evidence, self.provider
            )
        if _is_loopback_url(self.final_url) and self.evidence.get("synthetic_lab") is True:
            return True
        return _custom_identity_evidence(self.provider, self.evidence)


def _resolution_identity_text(value: object) -> str:
    """Canonicalize structured target identity without trusting display text."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(
        "".join(character if character.isalnum() else " " for character in text).split()
    )


def _resolution_path_identity(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if "://" in text:
        try:
            text = urlsplit(validate_navigation_url(text)).path
        except ValueError:
            return ""
    return _resolution_identity_text(text.rstrip("/"))


def _resolution_scalar_values(
    value: object,
    aliases: frozenset[str],
    *,
    depth: int = 0,
):
    """Yield scalar values below explicit evidence keys, bounded and immutable."""

    if depth > _MAX_EVIDENCE_DEPTH:
        return
    if isinstance(value, MappingABC):
        for raw_key, item in list(value.items())[:200]:
            key = normalise_evidence_key(raw_key)
            # Apply-hop/click evidence describes the browser control and its
            # navigation attempt.  Keys such as ``role=link`` or
            # ``role=button`` are accessibility telemetry, never the
            # opportunity's job-role identity.  Target/form URLs in these
            # subtrees are still checked below by ``_resolution_mapping_nodes``.
            if key in {"applyhop", "applyclick"}:
                continue
            if key in aliases and not isinstance(
                item, (MappingABC, list, tuple, set, frozenset, bool)
            ):
                text = str(item or "").strip()
                if text and text.casefold() not in {"none", "null", "false"}:
                    yield text
            yield from _resolution_scalar_values(item, aliases, depth=depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in list(value)[:200]:
            yield from _resolution_scalar_values(item, aliases, depth=depth + 1)


def _resolution_mapping_nodes(value: object, *, depth: int = 0):
    if depth > _MAX_EVIDENCE_DEPTH:
        return
    if isinstance(value, MappingABC):
        yield value
        for item in list(value.values())[:200]:
            yield from _resolution_mapping_nodes(item, depth=depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in list(value)[:200]:
            yield from _resolution_mapping_nodes(item, depth=depth + 1)


def _resolution_contract_origin(value: object) -> str:
    contract = canonical_target_contract_url(str(value or ""))
    parts = urlsplit(contract)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def validate_target_resolution_contract(resolution: TargetResolution) -> None:
    """Reject contradictory nested proof before any browser worker exists.

    Persisted target envelopes and caller-supplied ``TargetResolution``
    instances must use the same identity binding rules.  The persisted path
    additionally compares the envelope with authoritative opportunity columns;
    this shared validator covers the portion that can be checked from the
    immutable resolution itself: provider, identity aliases, requisition/path,
    form binding, frame/bound-target URLs, and application origin.

    It intentionally raises ``ValueError`` so the public Navigator boundary
    can refuse a direct forged resolution before constructing a worker or
    opening Playwright.
    """

    if not isinstance(resolution, TargetResolution):
        raise ValueError("Target resolution contract is not a TargetResolution")
    if not resolution.kind.automation_eligible or not resolution.identity_verified:
        raise ValueError("Target resolution is not eligible for automation")
    if resolution.kind is TargetKind.APPLICATION_FORM and not resolution.form_verified:
        raise ValueError("Application form target is not form-verified")

    try:
        final_contract = canonical_target_contract_url(resolution.final_url)
        expected_origin = _resolution_contract_origin(resolution.final_url)
    except (TypeError, ValueError) as exc:
        raise ValueError("Target resolution URL contract is malformed") from exc

    provider = _normalise_provider(resolution.provider)
    if not provider:
        raise ValueError("Target resolution provider is missing")

    evidence = resolution.evidence

    def values(aliases: frozenset[str]) -> list[str]:
        return list(_resolution_scalar_values(evidence, aliases))

    def require_same(items: list[str], label: str) -> None:
        if not items:
            return
        expected = _resolution_identity_text(items[0])
        if not expected or any(_resolution_identity_text(item) != expected for item in items):
            raise ValueError(f"Target resolution {label} aliases contradict one another")

    provider_values = values(PROVIDER_EVIDENCE_KEYS)
    if any(_normalise_provider(item) != provider for item in provider_values):
        raise ValueError("Target resolution provider evidence contradicts the verified provider")
    structured_values = values(frozenset({"structuredfeed"}))
    if any(
        _normalise_provider(item.split(":", 1)[0]) != provider
        for item in structured_values
        if ":" in item
    ):
        raise ValueError("Target resolution structured provider evidence contradicts the verified provider")

    for aliases, label in (
        (EMPLOYER_EVIDENCE_KEYS, "employer"),
        (ROLE_EVIDENCE_KEYS, "role"),
        (FORM_IDENTITY_EVIDENCE_KEYS, "form identity"),
        (ROOT_TOKEN_EVIDENCE_KEYS, "root token"),
        (ROOT_SELECTOR_EVIDENCE_KEYS, "root selector"),
    ):
        require_same(values(aliases), label)

    requisition_values = values(REQUISITION_EVIDENCE_KEYS)
    path_values = values(PATH_EVIDENCE_KEYS)
    path_like_requisitions = [
        item
        for item in requisition_values
        if item.startswith(("/", "http://", "https://")) or "/" in item
    ]
    identifier_requisitions = [
        item for item in requisition_values if item not in path_like_requisitions
    ]
    require_same(identifier_requisitions, "requisition")
    require_same(path_like_requisitions, "requisition path")
    require_same(path_values, "path")
    final_path = _resolution_path_identity(urlsplit(resolution.final_url).path)
    # A bare ``requisition`` value may be a provider identifier or a route
    # fragment (some synthetic/provider resolvers use a stable step path that
    # differs from the landing URL).  Persisted envelopes bind this more
    # tightly against authoritative opportunity columns before this shared
    # check runs.  For direct resolutions, only explicit path aliases are
    # required to match the final target path.
    if any(_resolution_path_identity(item) != final_path for item in path_values):
        raise ValueError("Target resolution path evidence is not bound to the verified target")

    frame_values = values(FRAME_URL_EVIDENCE_KEYS)
    frame_contracts: list[str] = []
    for item in frame_values:
        try:
            frame_contracts.append(canonical_target_contract_url(item))
            if _resolution_contract_origin(item) != expected_origin:
                raise ValueError("Target resolution frame URL escapes the verified origin")
        except ValueError as exc:
            if "escapes" in str(exc):
                raise
            raise ValueError("Target resolution frame URL evidence is malformed") from exc
    if frame_contracts and any(item != frame_contracts[0] for item in frame_contracts):
        raise ValueError("Target resolution frame URL aliases contradict one another")

    bound_target_values = values(BOUND_TARGET_URL_EVIDENCE_KEYS)
    for item in bound_target_values:
        try:
            if canonical_target_contract_url(item) != final_contract:
                raise ValueError("Target resolution bound target URL contradicts the verified target")
        except ValueError as exc:
            if "contradict" in str(exc):
                raise
            raise ValueError("Target resolution bound target URL evidence is malformed") from exc

    form_contract = False
    for node in _resolution_mapping_nodes(evidence):
        bound_provider_values = [
            str(item)
            for raw_key, item in node.items()
            if normalise_evidence_key(raw_key) == "boundprovider"
            and not isinstance(item, (MappingABC, list, tuple, set, frozenset, bool))
            and str(item or "").strip()
        ]
        if any(_normalise_provider(item) != provider for item in bound_provider_values):
            raise ValueError("Target resolution form binding provider contradicts the verified provider")
        binding_values = [
            str(item).strip().casefold()
            for raw_key, item in node.items()
            if normalise_evidence_key(raw_key) == "bindingverified"
            and not isinstance(item, (MappingABC, list, tuple, set, frozenset, bool))
            and str(item or "").strip()
        ]
        node_targets = [
            str(item).strip()
            for raw_key, item in node.items()
            if normalise_evidence_key(raw_key) in BOUND_TARGET_URL_EVIDENCE_KEYS
            and not isinstance(item, (MappingABC, list, tuple, set, frozenset, bool))
            and str(item or "").strip()
        ]
        node_roots = [
            str(item).strip()
            for raw_key, item in node.items()
            if normalise_evidence_key(raw_key) in ROOT_TOKEN_EVIDENCE_KEYS
            and not isinstance(item, (MappingABC, list, tuple, set, frozenset, bool))
            and str(item or "").strip()
        ]
        for item in node_targets:
            try:
                if canonical_target_contract_url(item) != final_contract:
                    raise ValueError("Target resolution form binding target contradicts the verified target")
            except ValueError as exc:
                if "contradict" in str(exc):
                    raise
                raise ValueError("Target resolution form binding target is malformed") from exc
        require_same(node_roots, "form binding root")
        if binding_values and all(item == "true" for item in binding_values):
            if node_targets and bound_provider_values and node_roots:
                form_contract = True

    origin_values = values(ORIGIN_EVIDENCE_KEYS) + frame_values + bound_target_values
    if not origin_values:
        raise ValueError("Target resolution application origin evidence is missing")
    for item in origin_values:
        try:
            if _resolution_contract_origin(item) != expected_origin:
                raise ValueError("Target resolution origin evidence contradicts the verified origin")
        except ValueError as exc:
            if "contradict" in str(exc):
                raise
            raise ValueError("Target resolution origin evidence is malformed") from exc

    # A valid direct resolution still needs the same minimum identity bundle
    # enforced by the Navigator's existing completeness check.  This function
    # is the contradiction gate; the caller keeps the final completeness gate.
    del form_contract


def _provider_from_url(url: str, html: str) -> str:
    trusted = trusted_provider_for_url(url)
    if trusted:
        return trusted
    providers = _html_providers(html)
    return next(iter(providers)) if len(providers) == 1 else ""


def _unresolved(
    source: str,
    final: str,
    reason: str,
    provider: str,
    evidence: Mapping[str, Any],
) -> TargetResolution:
    return TargetResolution(
        source,
        final,
        TargetKind.UNRESOLVED,
        provider,
        False,
        False,
        (reason,),
        evidence,
    )


_MAX_LISTING_CANDIDATES = 50
_MAX_LISTING_TEXT = 240
_GREENHOUSE_EMBED_PATH = "/embed/job_app"


def _listing_text(value: object, *, limit: int = _MAX_LISTING_TEXT) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())[:limit]


def _listing_attr(node: Any, names: tuple[str, ...]) -> str:
    attrs = getattr(node, "attrs", {})
    if not isinstance(attrs, MappingABC):
        return ""
    for name in names:
        value = attrs.get(name)
        if value not in (None, ""):
            text = _listing_text(value)
            if text:
                return text
    return ""


def _listing_node_value(node: Any, *, attributes: tuple[str, ...], selectors: tuple[str, ...]) -> str:
    value = _listing_attr(node, attributes)
    if value:
        return value
    for selector in selectors:
        child = node.select_one(selector) if hasattr(node, "select_one") else None
        if child is None:
            continue
        value = _listing_text(child.get_text(" ", strip=True))
        if value:
            return value
    return ""


@dataclass(frozen=True, slots=True)
class ListingCandidate:
    """One URL-backed role enumerated from a source listing.

    These values are observations only.  In particular, a candidate is never
    selected or promoted by this class; a person must choose the exact URL.
    """

    title: str
    location: str = ""
    programme_type: str = ""
    url: str = ""

    def __post_init__(self) -> None:
        title = _listing_text(self.title)
        url = validate_navigation_url(self.url)
        if not title or not url:
            raise ValueError("A listing candidate requires a title and URL")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "location", _listing_text(self.location))
        object.__setattr__(self, "programme_type", _listing_text(self.programme_type))
        object.__setattr__(self, "url", url)

    def as_evidence(self) -> dict[str, str]:
        return {
            "title": self.title,
            "location": self.location,
            "programme_type": self.programme_type,
            "url": self.url,
        }


def _listing_containers(soup: BeautifulSoup) -> list[Any]:
    containers: list[Any] = []
    selectors = (
        "[data-job-listing]",
        "[data-source-listing] article",
        "[data-source-listing] li",
        "[data-job-card]",
        "[data-role-card]",
        "article",
        "li",
    )
    for selector in selectors:
        for node in soup.select(selector)[:_MAX_LISTING_CANDIDATES * 2]:
            if node not in containers:
                containers.append(node)
    if not containers:
        for node in soup.select("[data-source-listing], [data-job-results], .job-listings")[:4]:
            if node.select_one("a[href]"):
                containers.append(node)
    return containers[:_MAX_LISTING_CANDIDATES * 2]


def _listing_candidate_from_node(node: Any, base_url: str) -> ListingCandidate | None:
    anchor = node if getattr(node, "name", "") == "a" else node.select_one("a[href]")
    if anchor is None:
        anchor = node.select_one('[role="link"][href]') if hasattr(node, "select_one") else None
    if anchor is None:
        return None
    raw_href = str(anchor.get("href") or "").strip()
    if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    try:
        resolved_url = validate_navigation_url(urljoin(base_url, raw_href))
        if canonical_url_key(resolved_url) == canonical_url_key(base_url):
            return None
    except (TypeError, ValueError):
        return None

    title = _listing_node_value(
        node,
        attributes=(
            "data-role-title",
            "data-job-title",
            "data-title",
            "data-role",
        ),
        selectors=("h1", "h2", "h3", "h4", '[role="heading"]', "[data-role-title]"),
    )
    if not title:
        title = _listing_node_value(
            anchor,
            attributes=("data-role-title", "data-job-title", "data-title"),
            selectors=(),
        )
    if not title:
        title = _listing_text(anchor.get_text(" ", strip=True))
    if not title or title.casefold() in {"view role", "view job", "apply", "apply now"}:
        return None
    location = _listing_node_value(
        node,
        attributes=("data-location", "data-job-location", "data-office"),
        selectors=("[data-location]", "[data-job-location]", ".location", ".job-location", '[itemprop="jobLocation"]'),
    )
    programme_type = _listing_node_value(
        node,
        attributes=(
            "data-programme-type",
            "data-program-type",
            "data-programme",
            "data-program",
            "data-programme-group",
        ),
        selectors=("[data-programme-type]", "[data-programme]", ".programme-type", ".programme"),
    )
    try:
        return ListingCandidate(title, location, programme_type, resolved_url)
    except ValueError:
        return None


def _json_ld_listing_candidates(soup: BeautifulSoup, base_url: str) -> list[ListingCandidate]:
    candidates: list[ListingCandidate] = []
    for script in soup.select('script[type="application/ld+json"]')[:20]:
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        items: list[Any] = []
        if isinstance(payload, list):
            items.extend(payload)
        elif isinstance(payload, MappingABC):
            items.append(payload)
            graph = payload.get("@graph")
            if isinstance(graph, list):
                items.extend(graph)
        for item in items[:_MAX_LISTING_CANDIDATES]:
            if not isinstance(item, MappingABC):
                continue
            item_type = str(item.get("@type") or "").casefold()
            if "jobposting" not in item_type:
                continue
            title = _listing_text(item.get("title"))
            raw_url = str(item.get("url") or "").strip()
            if not title or not raw_url:
                continue
            location = item.get("jobLocation")
            if isinstance(location, MappingABC):
                address = location.get("address")
                if isinstance(address, MappingABC):
                    location = ", ".join(
                        _listing_text(address.get(key))
                        for key in ("addressLocality", "addressRegion", "addressCountry")
                        if _listing_text(address.get(key))
                    )
            elif isinstance(location, list):
                location = ", ".join(_listing_text(value) for value in location)
            try:
                candidates.append(
                    ListingCandidate(
                        title=title,
                        location=_listing_text(location),
                        programme_type=_listing_text(item.get("employmentType")),
                        url=validate_navigation_url(urljoin(base_url, raw_url)),
                    )
                )
            except (TypeError, ValueError):
                continue
    return candidates


def enumerate_candidate_roles(html: str, base_url: str) -> tuple[ListingCandidate, ...]:
    """Enumerate bounded, URL-backed roles without choosing one.

    Only explicit links and structured JobPosting URLs are returned.  Missing
    fields remain blank because prose or URL invention is not evidence.
    """

    try:
        base = validate_navigation_url(base_url)
    except (TypeError, ValueError):
        return ()
    markup = str(html or "")[:200_000]
    if not markup:
        return ()
    soup = BeautifulSoup(markup, "html.parser")
    candidates = [
        candidate
        for node in _listing_containers(soup)
        if (candidate := _listing_candidate_from_node(node, base)) is not None
    ]
    candidates.extend(_json_ld_listing_candidates(soup, base))
    deduplicated: dict[str, ListingCandidate] = {}
    for candidate in candidates:
        try:
            key = canonical_url_key(candidate.url)
        except ValueError:
            continue
        deduplicated.setdefault(key, candidate)
        if len(deduplicated) >= _MAX_LISTING_CANDIDATES:
            break
    return tuple(deduplicated.values())


def _is_listing_surface(path: str, query: str, markup: str, candidate_count: int) -> bool:
    if (
        re.search(r"/(?:jobs|careers|search)/?$", path)
        or path.endswith("_careers")
        or ("/jobs" in path and not re.search(r"/(?:job|jobs)/[^/]+", path))
        or any(key in query for key in ("q=", "query=", "keywords=", "search="))
    ):
        return True
    if candidate_count > 0 and re.search(
        r"data-(?:source-listing|job-listing|job-card|role-card)|"
        r"(?:search-results|job-listings|career-listings)",
        markup,
        re.IGNORECASE,
    ):
        return True
    return candidate_count > 1 and bool(re.search(r"<(?:article|li)\b", markup, re.IGNORECASE))


def _greenhouse_embed_query_value(pairs: list[tuple[str, str]], key: str) -> str:
    values = [str(value).strip() for name, value in pairs if name.casefold() == key]
    values = [value for value in values if value]
    if not values or len({_resolution_identity_text(value) for value in values}) != 1:
        return ""
    return values[0]


def _resolution_identity_text(value: object) -> str:
    return " ".join(
        "".join(character if character.isalnum() else " " for character in
                unicodedata.normalize("NFKC", str(value or "")).casefold()).split()
    )


def _greenhouse_embed_form_proof(
    final_url: str,
    html: str,
    evidence: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """Return strict DOM corroboration for a Greenhouse embedded form."""

    parts = urlsplit(final_url)
    if trusted_provider_for_url(final_url) != "greenhouse" or parts.path.rstrip("/") != _GREENHOUSE_EMBED_PATH:
        return None, None, ""
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    organization = _greenhouse_embed_query_value(pairs, "for")
    token = _greenhouse_embed_query_value(pairs, "token")
    if not organization or not token or not token.isdigit() or not 4 <= len(token) <= 20:
        return None, None, "greenhouse_embed_identity_missing"
    soup = BeautifulSoup(str(html or "")[:200_000], "html.parser")
    forms = soup.select("form")
    if len(forms) != 1:
        return None, None, "greenhouse_embed_form_missing_or_ambiguous"
    form = forms[0]
    ancestors: list[Any] = [form]
    parent = getattr(form, "parent", None)
    for _ in range(5):
        if parent is None or parent in ancestors:
            break
        ancestors.append(parent)
        parent = getattr(parent, "parent", None)

    observed_organizations: list[str] = []
    observed_tokens: list[str] = []
    observed_employers: list[str] = []
    observed_roles: list[str] = []
    for node in ancestors:
        attrs = getattr(node, "attrs", {})
        if not isinstance(attrs, MappingABC):
            continue
        for raw_key, raw_value in attrs.items():
            key = str(raw_key).casefold().replace("_", "-")
            value = _listing_text(raw_value)
            if not value:
                continue
            if "organization" in key or key in {"data-org", "data-company-slug", "data-board"}:
                observed_organizations.append(value)
            if "greenhouse-token" in key or "job-post-id" in key or "requisition" in key or key in {"data-token", "data-job-id"}:
                observed_tokens.append(value)
            if key in {"data-employer", "data-company", "data-company-name"}:
                observed_employers.append(value)
            if key in {"data-role", "data-role-title", "data-job-title"}:
                observed_roles.append(value)
    supplied_embed = evidence.get("greenhouse_embed")
    if isinstance(supplied_embed, MappingABC):
        supplied_org = _listing_text(
            supplied_embed.get("organization") or supplied_embed.get("org") or supplied_embed.get("for")
        )
        supplied_token = _listing_text(
            supplied_embed.get("token") or supplied_embed.get("job_post_id") or supplied_embed.get("requisition")
        )
        if supplied_org:
            observed_organizations.append(supplied_org)
        if supplied_token:
            observed_tokens.append(supplied_token)

    form_action = urljoin(final_url, str(form.get("action") or final_url).strip())
    try:
        action = validate_navigation_url(form_action)
        action_pairs = parse_qsl(urlsplit(action).query, keep_blank_values=True)
    except (TypeError, ValueError):
        return None, None, "greenhouse_embed_form_action_invalid"
    action_org = _greenhouse_embed_query_value(action_pairs, "for")
    action_token = _greenhouse_embed_query_value(action_pairs, "token")
    if any(_resolution_identity_text(value) != _resolution_identity_text(organization) for value in observed_organizations):
        return None, None, "greenhouse_embed_organization_mismatch"
    if any(_resolution_identity_text(value) != _resolution_identity_text(token) for value in observed_tokens):
        return None, None, "greenhouse_embed_token_mismatch"
    if action_org and _resolution_identity_text(action_org) != _resolution_identity_text(organization):
        return None, None, "greenhouse_embed_organization_mismatch"
    if action_token and _resolution_identity_text(action_token) != _resolution_identity_text(token):
        return None, None, "greenhouse_embed_token_mismatch"
    if not observed_organizations and not action_org:
        return None, None, "greenhouse_embed_organization_uncorroborated"
    if not observed_tokens and not action_token:
        return None, None, "greenhouse_embed_token_uncorroborated"

    expected_employer = _evidence_value(evidence, EMPLOYER_EVIDENCE_KEYS)
    expected_role = _evidence_value(evidence, ROLE_EVIDENCE_KEYS)
    observed_employer = next((value for value in observed_employers if value), "")
    observed_role = next((value for value in observed_roles if value), "")
    if expected_employer and observed_employer and _resolution_identity_text(expected_employer) != _resolution_identity_text(observed_employer):
        return None, None, "greenhouse_embed_employer_mismatch"
    if expected_role and observed_role and _resolution_identity_text(expected_role) != _resolution_identity_text(observed_role):
        return None, None, "greenhouse_embed_role_mismatch"
    controls = form.select("input, select, textarea, button")
    submit_present = bool(form.select('button[type="submit"], input[type="submit"]'))
    if not submit_present:
        submit_present = any(
            str(control.get("type") or "").casefold() == "submit"
            for control in controls
        )
    if not controls or not submit_present:
        return None, None, "greenhouse_embed_form_controls_missing"
    form_id = _listing_text(form.get("id"))
    form_class = next(
        (_listing_text(value) for value in (form.get("class") or []) if _listing_text(value)),
        "",
    )
    root_selector = (
        f"form#{form_id}" if form_id and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", form_id)
        else f"form.{form_class}" if form_class and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", form_class)
        else "form"
    )
    form_proof = {
        "organization": organization,
        "token": token,
        "organization_match": True,
        "token_match": True,
        "employer": observed_employer,
        "role": observed_role,
        "form_action": action,
        "form_selector": root_selector,
    }
    form_evidence = {
        "frame_url": final_url,
        "root_selector": root_selector,
        "control_count": len(controls),
        "submit_present": True,
        "root_token": token,
        "form_identity": token,
        "binding_verified": True,
        "bound_target_url": final_url,
        "bound_provider": "greenhouse",
        "bound_role": observed_role or expected_role,
        "bound_requisition": token,
        "bound_form_identity": token,
        "form_action": action,
    }
    return form_proof, form_evidence, ""


def classify_target(
    source_url: str,
    final_url: str | None = None,
    *,
    html: str = "",
    content_type: str = "text/html",
    content_disposition: str = "",
    http_status: int | None = None,
    provider_hint: str = "",
    identity_verified: bool = False,
    form_handle: FormHandle | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> TargetResolution:
    """Classify navigation evidence without performing any I/O or mutation."""

    source = validate_navigation_url(source_url)
    final = validate_navigation_url(final_url or source_url)
    parts = urlsplit(final)
    host = (parts.hostname or "").casefold().rstrip(".")
    path = parts.path.casefold()
    query = parts.query.casefold()
    markup = html.casefold()
    source_provider = trusted_provider_for_url(source)
    trusted_provider = trusted_provider_for_url(final)
    origin_provider = _provider_for_host(host)
    html_providers = _html_providers(html)
    html_provider = next(iter(html_providers)) if len(html_providers) == 1 else ""
    hint = _normalise_provider(provider_hint)
    base_evidence: dict[str, Any] = dict(evidence or {})
    base_evidence.update(
        {
            "content_type": content_type.split(";", 1)[0].strip().casefold(),
            "content_disposition": content_disposition[:300],
        }
    )
    if http_status is not None:
        base_evidence["http_status"] = int(http_status)
    evidence_provider = _normalise_provider(
        _evidence_value(base_evidence, _ATS_EVIDENCE_KEYS)
    )

    # Objective transport evidence precedes advisory provider hints.  A real
    # ATS host returning 404/410 is closed/missing, not an untrusted hint; an
    # error body can never be promoted by provider-looking markup.
    if http_status is not None:
        status = int(http_status)
        status_provider = trusted_provider or origin_provider
        if status in {401, 403}:
            return TargetResolution(
                source, final, TargetKind.AUTH_WALL, status_provider, False, False,
                ("http_authentication_required",), base_evidence,
            )
        if status in {404, 410}:
            return TargetResolution(
                source, final, TargetKind.BLOCKED, status_provider, False, False,
                ("http_not_found" if status == 404 else "http_gone",),
                base_evidence,
            )
        if status >= 400:
            return _unresolved(
                source, final, f"http_error_{status}", status_provider, base_evidence
            )

    disposition = content_disposition.casefold()
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if "attachment" in disposition:
        return TargetResolution(
            source, final, TargetKind.NON_HTML, trusted_provider, False, False,
            ("attachment_response",), base_evidence,
        )
    if media_type and media_type not in {"text/html", "application/xhtml+xml"}:
        return TargetResolution(
            source, final, TargetKind.NON_HTML, trusted_provider, False, False,
            ("non_html_content",), base_evidence,
        )

    if html_providers and len(html_providers) > 1:
        return _unresolved(source, final, "conflicting_provider_markers", "", base_evidence)
    if trusted_provider and html_provider and trusted_provider != html_provider:
        return TargetResolution(
            source,
            final,
            TargetKind.MISMATCH,
            trusted_provider,
            False,
            False,
            ("provider_dom_marker_mismatch",),
            base_evidence,
        )
    if source_provider and trusted_provider and source_provider != trusted_provider:
        return TargetResolution(
            source,
            final,
            TargetKind.MISMATCH,
            trusted_provider,
            False,
            False,
            ("provider_redirect_mismatch",),
            base_evidence,
        )
    if trusted_provider and evidence_provider and trusted_provider != evidence_provider:
        return TargetResolution(
            source,
            final,
            TargetKind.MISMATCH,
            trusted_provider,
            False,
            False,
            ("provider_evidence_mismatch",),
            base_evidence,
        )
    if hint and hint not in _KNOWN_PROVIDERS:
        return _unresolved(source, final, "untrusted_provider_hint", "", base_evidence)
    provider = trusted_provider or html_provider or evidence_provider
    if hint and provider and hint != provider:
        return TargetResolution(
            source,
            final,
            TargetKind.MISMATCH,
            provider,
            False,
            False,
            ("provider_hint_mismatch",),
            base_evidence,
        )
    if hint and not provider:
        return _unresolved(source, final, "untrusted_provider_hint", "", base_evidence)

    if origin_provider and not trusted_provider:
        return _unresolved(
            source,
            final,
            "provider_origin_untrusted",
            origin_provider,
            base_evidence,
        )
    if path.endswith((".pdf", ".doc", ".docx", ".zip")):
        return TargetResolution(
            source, final, TargetKind.NON_HTML, provider, False, False,
            ("non_html_extension",), base_evidence,
        )
    if host in {"linkedin.com", "www.linkedin.com", "linkedin.co.uk", "www.linkedin.co.uk"}:
        return TargetResolution(
            source, final, TargetKind.LISTING, "", False, False,
            ("linkedin_source_only",), base_evidence,
        )
    if provider and re.search(
        r"(?:"
        r"the page you are looking for (?:doesn['’]?t|does not) exist|"
        r"this job posting is no longer available|"
        r"this job is no longer available|"
        r"the job you are looking for is no longer available|"
        r"this position has been filled|"
        r"no longer accepting applications"
        r")",
        markup,
    ):
        return TargetResolution(
            source,
            final,
            TargetKind.BLOCKED,
            provider,
            False,
            False,
            ("job_closed_or_missing",),
            base_evidence,
        )
    if re.search(
        r"(?:"
        r"\bclass\s*=\s*['\"][^'\"]*(?<![\w-])(?:g-recaptcha|h-captcha)"
        r"(?![\w-])[^'\"]*['\"]|"
        r"<iframe\b[^>]*(?:captcha|recaptcha|hcaptcha)[^>]*(?:challenge|dialog)|"
        r"<iframe\b[^>]*(?:challenge|dialog)[^>]*(?:captcha|recaptcha|hcaptcha)|"
        r"\b(?:id|class)\s*=\s*['\"][^'\"]*captcha-(?:challenge|dialog|frame)\b|"
        r"\bdata-sitekey\s*=|"
        r"verify you are human|complete the captcha"
        r")",
        markup,
    ):
        return TargetResolution(
            source, final, TargetKind.HUMAN_CHALLENGE, provider, identity_verified, False,
            ("human_challenge",), base_evidence,
        )
    if (
        'type="password"' in markup
        or "type='password'" in markup
        or re.search(r"/(?:sign-?in|login|auth)(?:/|$)", path)
    ):
        return TargetResolution(
            source, final, TargetKind.AUTH_WALL, provider, identity_verified, False,
            ("authentication_wall",), base_evidence,
        )

    if form_handle is not None:
        handle_provider = _normalise_provider(form_handle.provider)
        try:
            final_contract = canonical_target_contract_url(final)
            frame_contract = canonical_target_contract_url(form_handle.frame_url)
        except (TypeError, ValueError):
            return TargetResolution(
                source, final, TargetKind.MISMATCH, provider or handle_provider, False, False,
                ("form_frame_mismatch",), base_evidence,
            )
        if frame_contract != final_contract:
            return TargetResolution(
                source, final, TargetKind.MISMATCH, provider or handle_provider, False, False,
                ("form_frame_mismatch",), base_evidence,
            )
        if provider and handle_provider and provider != handle_provider:
            return TargetResolution(
                source, final, TargetKind.MISMATCH, provider, False, False,
                ("form_provider_mismatch",), base_evidence,
            )
        root_token = str(form_handle.evidence.get("root_token", "") or "").strip()
        if not root_token:
            return _unresolved(
                source,
                final,
                "form_root_token_missing",
                provider or handle_provider,
                base_evidence,
            )
        if not bool(form_handle.evidence.get("binding_verified")):
            return _unresolved(
                source,
                final,
                "form_resolution_binding_missing",
                provider or handle_provider,
                base_evidence,
            )
        bound_target_url = str(form_handle.evidence.get("bound_target_url", "") or "").strip()
        try:
            bound_target_contract = canonical_target_contract_url(bound_target_url)
        except (TypeError, ValueError):
            bound_target_contract = ""
        if not bound_target_contract or bound_target_contract != final_contract:
            return TargetResolution(
                source,
                final,
                TargetKind.MISMATCH,
                provider or handle_provider,
                False,
                False,
                ("form_resolution_target_mismatch",),
                base_evidence,
            )
        bound_provider = _normalise_provider(form_handle.evidence.get("bound_provider"))
        if not bound_provider or (provider and bound_provider != provider):
            return TargetResolution(
                source,
                final,
                TargetKind.MISMATCH,
                provider or handle_provider,
                False,
                False,
                ("form_resolution_provider_mismatch",),
                base_evidence,
            )
        control_count = int(form_handle.evidence.get("control_count", 0) or 0)
        submit_present = bool(form_handle.evidence.get("submit_present"))
        root_found = bool(form_handle.evidence.get("root_found", True))
        if root_found and submit_present and control_count > 0:
            base_evidence["form"] = {
                "frame_url": form_handle.frame_url,
                "root_selector": form_handle.root_selector,
                "control_count": control_count,
                "submit_present": submit_present,
                "root_token": root_token,
                "form_identity": root_token,
                "binding_verified": True,
                "bound_target_url": bound_target_url,
                "bound_provider": bound_provider,
                "bound_role": str(form_handle.evidence.get("bound_role", "") or ""),
                "bound_requisition": str(form_handle.evidence.get("bound_requisition", "") or ""),
                "bound_form_identity": str(form_handle.evidence.get("bound_form_identity", "") or ""),
            }
            if not provider:
                provider = handle_provider

    embed_form_proof, embed_form_evidence, embed_reason = _greenhouse_embed_form_proof(
        final,
        html,
        base_evidence,
    )
    if embed_reason:
        if embed_reason.endswith("_mismatch"):
            return TargetResolution(
                source,
                final,
                TargetKind.MISMATCH,
                provider or trusted_provider or "greenhouse",
                False,
                False,
                (embed_reason,),
                base_evidence,
            )
        return _unresolved(
            source,
            final,
            embed_reason,
            provider or trusted_provider or "greenhouse",
            base_evidence,
        )
    if embed_form_proof is not None:
        base_evidence["greenhouse_embed"] = embed_form_proof
        if form_handle is None and embed_form_evidence is not None:
            base_evidence["form"] = embed_form_evidence

    custom_identity = bool(
        provider
        and not trusted_provider
        and _custom_identity_evidence(provider, base_evidence, html)
    )
    if custom_identity:
        base_evidence["custom_domain_verified"] = True
    identity_ok = bool(identity_verified) and (
        (bool(trusted_provider) and _trusted_identity_evidence(base_evidence, provider))
        or custom_identity
        or (_is_loopback_url(final) and base_evidence.get("synthetic_lab") is True)
    )

    # Any custom-domain ATS claim remains review-only until the independent
    # identity bundle above is present.  This catches forms/redirects that
    # merely copy an ATS label onto a marketing or attacker page.
    if provider and not trusted_provider and not custom_identity:
        return _unresolved(source, final, "custom_domain_identity_unverified", provider, base_evidence)
    if source_provider and not trusted_provider and not custom_identity:
        return _unresolved(source, final, "source_provider_redirect_untrusted", provider, base_evidence)

    if form_handle is not None:
        control_count = int(form_handle.evidence.get("control_count", 0) or 0)
        submit_present = bool(form_handle.evidence.get("submit_present"))
        root_found = bool(form_handle.evidence.get("root_found", True))
        if identity_ok and root_token and root_found and submit_present and control_count > 0:
            return TargetResolution(
                source,
                final,
                TargetKind.APPLICATION_FORM,
                _normalise_provider(form_handle.provider) or provider,
                True,
                True,
                ("verified_application_form",),
                base_evidence,
            )

    if embed_form_evidence is not None:
        if identity_ok:
            return TargetResolution(
                source,
                final,
                TargetKind.APPLICATION_FORM,
                provider or "greenhouse",
                True,
                True,
                ("verified_greenhouse_embed_form",),
                base_evidence,
            )
        return _unresolved(
            source,
            final,
            "greenhouse_embed_identity_unverified",
            provider or "greenhouse",
            base_evidence,
        )

    candidate_roles = enumerate_candidate_roles(html, final)
    if _is_listing_surface(path, query, markup, len(candidate_roles)):
        if candidate_roles:
            base_evidence["candidate_count"] = len(candidate_roles)
            base_evidence["candidate_roles"] = [
                candidate.as_evidence() for candidate in candidate_roles
            ]
            base_evidence["human_choice_required"] = True
            return TargetResolution(
                source,
                final,
                TargetKind.MULTIPLE_CANDIDATE_ROLES,
                provider,
                False,
                False,
                ("multiple_candidate_roles", "human_choice_required"),
                base_evidence,
            )
        return TargetResolution(
            source, final, TargetKind.LISTING, provider, False, False,
            ("search_or_listing_page",), base_evidence,
        )

    # Known source-only/search/listing surfaces.
    if (
        re.search(r"/(?:jobs|careers|search)/?$", path)
        or path.endswith("_careers")
        or ("/jobs" in path and not re.search(r"/(?:job|jobs)/[^/]+", path))
        or any(key in query for key in ("q=", "query=", "keywords="))
    ):
        return TargetResolution(
            source, final, TargetKind.LISTING, provider, False, False,
            ("search_or_listing_page",), base_evidence,
        )

    if custom_identity and re.search(r"/(?:apply|application)(?:/|$)", path):
        kind, reason = TargetKind.APPLICATION_ENTRY, "verified_custom_ats_job"
    elif provider == "greenhouse" and re.search(r"/jobs/\d+(?:/|$)", path):
        kind, reason = TargetKind.APPLICATION_ENTRY, "direct_ats_job"
    elif provider == "lever" and len([part for part in path.split("/") if part]) >= 2:
        kind, reason = TargetKind.APPLICATION_ENTRY, "direct_ats_job"
    elif provider == "workday" and re.search(r"/(?:job|jobs)/[^/]+", path):
        kind, reason = TargetKind.APPLICATION_ENTRY, "direct_ats_job"
    elif re.search(r"/(?:jobs?|position)/[^/]+", path):
        kind, reason = TargetKind.JOB_DETAIL, "employer_job_detail"
    else:
        kind, reason = TargetKind.JOB_DETAIL, "unverified_job_detail"

    if provider and not trusted_provider and not custom_identity:
        return _unresolved(source, final, "custom_domain_identity_unverified", provider, base_evidence)

    return TargetResolution(
        source, final, kind, provider, identity_ok, False,
        (reason,), base_evidence,
    )
