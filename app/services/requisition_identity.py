"""Strict, DB-originated requisition identity for target resolution.

The helpers in this module never inspect page prose or accept an ATS hint as
authority.  They derive a bounded job identity only from an exact stored
public URL whose provider host is already trusted by the target classifier.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlsplit

from app.automation.targets import trusted_provider_for_url
from app.domain.targets import validate_navigation_url


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_WORKDAY_SUFFIX = re.compile(
    r"_((?:[A-Za-z]{1,5})-?[0-9][A-Za-z0-9-]{0,63})$",
    re.IGNORECASE,
)
_EXPLICIT_REQUISITION = re.compile(
    r"(?<![A-Za-z0-9])((?:REQ|JR|R)-?[0-9][A-Za-z0-9-]{0,63})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_QUERY_KEYS = frozenset(
    {
        "gh_jid",
        "job_id",
        "jobid",
        "jobreq",
        "jobdetails",
        "posting_id",
        "postingid",
        "requisition",
        "requisition_id",
        "requisitionid",
    }
)


def canonical_requisition(value: object) -> str:
    """Normalize case/Unicode/space without erasing identity punctuation."""

    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).casefold().split()
    )


def requisitions_equal(left: object, right: object) -> bool:
    left_key = canonical_requisition(left)
    right_key = canonical_requisition(right)
    return bool(left_key and right_key and left_key == right_key)


@dataclass(frozen=True, slots=True)
class RequisitionIdentity:
    provider: str
    tenant: str
    requisition: str
    provenance: str = "stored_inspection_url"

    def __post_init__(self) -> None:
        provider = str(self.provider or "").casefold().strip()
        tenant = str(self.tenant or "").casefold().strip()
        requisition = str(self.requisition or "").strip()
        if not provider or not tenant or not requisition:
            raise ValueError("A requisition identity requires provider, tenant, and id")
        if not _SAFE_COMPONENT.fullmatch(tenant) or not _SAFE_COMPONENT.fullmatch(
            requisition
        ):
            raise ValueError("Requisition identity contains an unsafe component")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "tenant", tenant)
        object.__setattr__(self, "requisition", requisition)

    @property
    def canonical_key(self) -> str:
        return "\x1f".join(
            (
                self.provider,
                self.tenant,
                canonical_requisition(self.requisition),
            )
        )

    def as_evidence(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "tenant": self.tenant,
            "vendor_job_id": self.requisition,
            "canonical_key": self.canonical_key,
            "provenance": self.provenance,
        }


def _plain_segments(path: str) -> list[str] | None:
    segments: list[str] = []
    for raw in path.split("/"):
        if not raw:
            continue
        decoded = unquote(raw)
        # Percent-encoded identity is deliberately refused.  It makes exact
        # browser/DB equality ambiguous and has no need in supported routes.
        if decoded != raw or not _SAFE_COMPONENT.fullmatch(decoded):
            return None
        segments.append(decoded)
    return segments


def _single_query_value(pairs: list[tuple[str, str]], key: str) -> str:
    values = [value.strip() for name, value in pairs if name.casefold() == key]
    values = [value for value in values if value]
    if not values:
        return ""
    canonical = {canonical_requisition(value) for value in values}
    if len(canonical) != 1:
        return ""
    value = values[0]
    return value if _SAFE_COMPONENT.fullmatch(value) else ""


def _query_requisition(pairs: list[tuple[str, str]]) -> str:
    candidates: list[str] = []
    for key, value in pairs:
        if key.casefold().replace("-", "_") not in _QUERY_KEYS:
            continue
        text = value.strip()
        explicit = _EXPLICIT_REQUISITION.search(text)
        if explicit:
            text = explicit.group(1)
        if _SAFE_COMPONENT.fullmatch(text):
            candidates.append(text)
    canonical = {canonical_requisition(item) for item in candidates}
    return candidates[0] if len(canonical) == 1 else ""


def requisition_identity_from_url(url: str) -> RequisitionIdentity | None:
    """Return a strict provider/tenant/job id from one stored inspection URL."""

    try:
        validated = validate_navigation_url(url)
        parts = urlsplit(validated)
        if parts.scheme != "https" or parts.port not in {None, 443}:
            return None
    except (TypeError, ValueError):
        return None
    provider = trusted_provider_for_url(validated)
    if not provider:
        return None
    host = (parts.hostname or "").casefold().rstrip(".")
    segments = _plain_segments(parts.path)
    if segments is None:
        return None
    pairs = parse_qsl(parts.query, keep_blank_values=True)

    if provider == "greenhouse":
        if host == "boards-api.greenhouse.io":
            return None
        if parts.path.rstrip("/") == "/embed/job_app":
            tenant = _single_query_value(pairs, "for")
            requisition = _single_query_value(pairs, "token")
            if tenant and requisition and requisition.isdigit() and 4 <= len(requisition) <= 20:
                return RequisitionIdentity(provider, tenant, requisition)
            return None
        if len(segments) in {3, 4} and len(segments) >= 3 and segments[1].casefold() == "jobs":
            tenant, requisition = segments[0], segments[2]
            if len(segments) == 4 and segments[3].casefold() != "apply":
                return None
            if requisition.isdigit() and 4 <= len(requisition) <= 20:
                gh_jid = _single_query_value(pairs, "gh_jid")
                if gh_jid and not requisitions_equal(gh_jid, requisition):
                    return None
                return RequisitionIdentity(provider, tenant, requisition)
        return None

    if provider == "lever":
        if len(segments) not in {2, 3} or (
            len(segments) == 3 and segments[2].casefold() != "apply"
        ):
            return None
        tenant, requisition = segments[:2]
        if len(requisition) < 8:
            return None
        return RequisitionIdentity(provider, tenant, requisition)

    if provider == "smartrecruiters":
        if len(segments) not in {2, 3}:
            return None
        tenant = segments[0]
        match = re.match(r"^([0-9]{6,20})(?:-|$)", segments[1])
        if not match:
            return None
        if len(segments) == 3 and segments[2].casefold() not in {"apply", "application"}:
            return None
        return RequisitionIdentity(provider, tenant, match.group(1))

    if provider == "workable":
        if len(segments) not in {3, 4} or segments[1].casefold() != "j":
            return None
        if len(segments) == 4 and segments[3].casefold() not in {"apply", "application"}:
            return None
        return RequisitionIdentity(provider, segments[0], segments[2])

    if provider == "workday":
        lower_segments = [item.casefold() for item in segments]
        try:
            marker_index = lower_segments.index("job")
        except ValueError:
            return None
        if marker_index >= len(segments) - 1:
            return None
        match = _WORKDAY_SUFFIX.search(segments[-1])
        if not match:
            return None
        requisition = match.group(1)
        query_requisition = _query_requisition(pairs)
        if query_requisition and not requisitions_equal(query_requisition, requisition):
            return None
        return RequisitionIdentity(provider, host, requisition)

    return None


def identity_matches_url(identity: RequisitionIdentity, url: str) -> bool:
    observed = requisition_identity_from_url(url)
    return bool(observed and observed.canonical_key == identity.canonical_key)
