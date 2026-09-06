from __future__ import annotations

import hashlib
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class TargetKind(StrEnum):
    UNRESOLVED = "UNRESOLVED"
    MISSING_EMPLOYER_LINK = "MISSING_EMPLOYER_LINK"
    LISTING = "LISTING"
    MULTIPLE_CANDIDATE_ROLES = "MULTIPLE_CANDIDATE_ROLES"
    JOB_DETAIL = "JOB_DETAIL"
    APPLICATION_ENTRY = "APPLICATION_ENTRY"
    APPLICATION_FORM = "APPLICATION_FORM"
    AUTH_WALL = "AUTH_WALL"
    HUMAN_CHALLENGE = "HUMAN_CHALLENGE"
    NON_HTML = "NON_HTML"
    MISMATCH = "MISMATCH"
    BLOCKED = "BLOCKED"

    @property
    def automation_eligible(self) -> bool:
        return self in {TargetKind.APPLICATION_ENTRY, TargetKind.APPLICATION_FORM}


_TRACKING_KEYS = frozenset({"trid", "dcr_ci"})


def _normalised_http_parts(raw: str):
    value = raw.strip()
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Invalid navigation URL")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid navigation URL") from exc
    if parts.scheme.casefold() not in {"http", "https"} or not hostname:
        raise ValueError("Invalid navigation URL: expected http or https")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Invalid navigation URL: embedded credentials are forbidden")

    host = hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return parts, parts.scheme.casefold(), netloc


def validate_navigation_url(raw: str) -> str:
    """Return a safe-to-navigate HTTP(S) URL without discarding semantics.

    This deliberately keeps the path, query-pair order, functional parameters,
    and fragment.  It is not a canonical deduplication key.
    """

    parts, scheme, netloc = _normalised_http_parts(raw)
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


def strip_tracking_parameters(raw: str, *, preserve_fragment: bool = True) -> str:
    """Remove only explicitly known tracking pairs using parsed URL components."""

    navigation_url = validate_navigation_url(raw)
    parts = urlsplit(navigation_url)
    pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_KEYS
    ]
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(pairs),
            parts.fragment if preserve_fragment else "",
        )
    )


def canonical_url_key(raw: str, provider_hint: str = "") -> str:
    """Return a stable URL identity key that is never used for navigation."""

    del provider_hint  # reserved for narrowly documented provider rules
    cleaned = strip_tracking_parameters(raw, preserve_fragment=False)
    parts = urlsplit(cleaned)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def source_identity_key(
    *,
    employer: str,
    role_title: str,
    cycle: str,
    source_url: str,
    location: str = "",
    division: str = "",
) -> tuple[str, str, str, str, str, str]:
    """Return the exact normalized source identity used by service dedupe.

    The tuple, not its digest, is authoritative. This keeps hash collisions
    harmless and distinguishes shared programme pages by role/location/division.
    """

    try:
        url_key = canonical_url_key(source_url)
    except ValueError:
        url_key = source_url.strip().casefold()
    return (
        (employer or "").strip().casefold(),
        (role_title or "").strip().casefold(),
        (cycle or "").strip().casefold(),
        url_key,
        (location or "").strip().casefold(),
        (division or "").strip().casefold(),
    )


def source_fingerprint(
    *,
    employer: str,
    role_title: str,
    cycle: str,
    source_url: str,
    location: str = "",
    division: str = "",
) -> str:
    """Return a compact lookup hint; equality never proves exact identity."""

    material = "\x1f".join(
        source_identity_key(
            employer=employer,
            role_title=role_title,
            cycle=cycle,
            source_url=source_url,
            location=location,
            division=division,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
