"""Strict URL-origin and candidate-data egress contracts.

The browser route guard uses this module as a pure policy boundary.  It must
be possible to block a third-party image without failing inspection, while a
request that could carry candidate data is always fail-closed before route
delivery.  The old public helpers remain available; the typed
``classify_egress`` result is the preferred interface for new runner code.
"""
from __future__ import annotations

import copyreg
import ipaddress
import base64
import binascii
import json
import pickle
import re
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from types import MappingProxyType
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit


# MappingProxyType is not picklable in CPython 3.13 on Windows, which breaks
# multiprocessing spawn (the default on Windows).  Register a copyreg handler
# so these objects survive process boundary serialisation.
def _rebuild_mappingproxy(items: tuple) -> MappingProxyType:
    return MappingProxyType(dict(items))


def _reduce_mappingproxy(mp: MappingProxyType) -> tuple:
    return _rebuild_mappingproxy, (tuple(mp.items()),)


copyreg.pickle(MappingProxyType, _reduce_mappingproxy)


_NETWORK_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_CANDIDATE_DEPTH = 6
_MAX_CANDIDATE_CHARS = 16_384
_MAX_DECODED_BYTES = 8_192
_MAX_CONTAINER_ITEMS = 256
_MAX_CONTAINER_DEPTH = 12
_GREENHOUSE_JOB_HOSTS = frozenset(
    {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }
)
_GREENHOUSE_JOB_PATH_RE = re.compile(r"(?:^|/)jobs/([0-9]{4,20})(?:/|$)")


def normalise_hostname(value: str) -> str:
    """Return a case-insensitive DNS hostname without a trailing dot."""

    value = str(value or "").strip().casefold().rstrip(".")
    if not value:
        return ""
    try:
        return value.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return value


def _valid_hostname(hostname: str) -> bool:
    host = normalise_hostname(hostname)
    if not host or len(host) > 253 or ".." in host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if len(labels) < 2:
            return host == "localhost"
        return all(
            label
            and len(label) <= 63
            and not label.startswith("-")
            and not label.endswith("-")
            and all(character.isalnum() or character == "-" for character in label)
            for label in labels
        )
    return True


def _parse_strict_url(raw: str, *, schemes: Iterable[str]):
    if not isinstance(raw, str) or not raw or _CONTROL_CHARS.search(raw):
        raise ValueError("URL must be a non-empty string without control characters")
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.casefold()
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("malformed URL") from exc
    allowed = {str(value).casefold() for value in schemes}
    if scheme not in allowed:
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are never allowed")
    if parsed.netloc.endswith(":"):
        raise ValueError("URL port cannot be empty")
    if any(ord(character) > 127 for character in hostname or ""):
        raise ValueError("Unicode hostnames are ambiguous; use strict ASCII IDNA")
    if not hostname or not _valid_hostname(hostname):
        raise ValueError("URL hostname is invalid")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL port is outside the valid range")
    return parsed, normalise_hostname(hostname), port


def validate_origin(raw: str) -> str:
    """Validate and canonicalize an HTTP(S) *origin*.

    Origins intentionally contain no path, query, fragment, credentials, or
    wildcard. A single trailing slash is harmless and removed.
    """

    parsed, hostname, port = _parse_strict_url(raw, schemes={"http", "https"})
    if "?" in raw or "#" in raw:
        raise ValueError("origin must not contain query or fragment delimiters")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("origin must not contain a path, query, or fragment")
    scheme = parsed.scheme.casefold()
    effective_port = port
    if effective_port == _DEFAULT_PORTS[scheme]:
        effective_port = None
    netloc = hostname
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if ":" in hostname:
            netloc = f"[{hostname}]"
    if effective_port is not None:
        netloc = f"{netloc}:{effective_port}"
    return urlunsplit((scheme, netloc, "", "", ""))


def normalize_origin(raw: str) -> str:
    """US-spelling alias for :func:`validate_origin`."""

    return validate_origin(raw)


def normalise_origin(raw: str) -> str:
    """British-spelling alias for :func:`validate_origin`."""

    return validate_origin(raw)


def origin_for_url(raw: str) -> str:
    """Return the strict canonical origin for a navigable URL."""

    parsed, _hostname, _port = _parse_strict_url(raw, schemes=_NETWORK_SCHEMES)
    origin_scheme = parsed.scheme.casefold()
    if origin_scheme == "ws":
        origin_scheme = "http"
    elif origin_scheme == "wss":
        origin_scheme = "https"
    return validate_origin(urlunsplit((origin_scheme, parsed.netloc, "", "", "")))


def _valid_wildcard(entry: str) -> bool:
    if not entry.startswith("*.") or entry.count("*") != 1:
        return False
    base = normalise_hostname(entry[2:])
    try:
        ipaddress.ip_address(base)
    except ValueError:
        pass
    else:
        return False
    return (
        "." in base
        and base not in {"localhost", "local"}
        and not base.startswith(".")
        and not base.endswith(".")
        and ".." not in base
        and all(label and label != "*" for label in base.split("."))
        and _valid_hostname(base)
    )


def _registrable_domain(hostname: str) -> str | None:
    """Extract the registrable domain from a DNS hostname using a simple heuristic.

    Returns the last two labels for standard TLDs and three labels for known
    two-part public suffixes.  This is deliberately NOT a full PSL lookup: it
    only needs to match the registrable domain of approved hosts that share
    the same parent domain as first-party service subdomains.  IP addresses
    and localhost return None (they have no registrable domain).
    """
    host = normalise_hostname(hostname)
    if not host or "." not in host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2:
        return None
    _TWO_PART_SUFFIXES = frozenset({
        "co.uk", "org.uk", "ac.uk", "gov.uk",
        "com.au", "net.au", "org.au",
        "co.jp", "ne.jp", "or.jp",
        "co.nz", "net.nz", "org.nz",
        "co.za", "net.za", "org.za",
    })
    if len(labels) >= 3:
        tail = ".".join(labels[-2:]).casefold()
        if tail in _TWO_PART_SUFFIXES:
            return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def host_matches_allowlist(hostname: str, allowlist: Iterable[str]) -> bool:
    """Match one host exactly, or a suffix only via ``*.example.com``."""

    if not isinstance(hostname, str) or any(ord(character) > 127 for character in hostname):
        return False
    host = normalise_hostname(hostname)
    if not host or not _valid_hostname(host):
        return False
    for raw_entry in allowlist:
        if not isinstance(raw_entry, str) or any(ord(character) > 127 for character in raw_entry):
            continue
        entry = normalise_hostname(raw_entry)
        if not entry:
            continue
        if "*" in entry:
            if not _valid_wildcard(entry):
                continue
            base = normalise_hostname(entry[2:])
            if host != base and host.endswith("." + base):
                return True
            continue
        if host == entry and _valid_hostname(entry):
            return True
    return False


def url_matches_allowlist(url: str, allowlist: Iterable[str]) -> bool:
    """Apply exact-host policy to a URL, rejecting malformed URLs."""

    try:
        _parsed, hostname, _port = _parse_strict_url(url, schemes=_NETWORK_SCHEMES)
    except ValueError:
        return False
    return host_matches_allowlist(hostname, allowlist)


def _hostname_resolves_public(hostname: str) -> bool:
    """Require every resolver answer to be a globally routable address."""

    try:
        answers = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for answer in answers:
        sockaddr = answer[4] if len(answer) > 4 else ()
        raw_address = sockaddr[0] if sockaddr else ""
        try:
            addresses.append(ipaddress.ip_address(raw_address))
        except ValueError:
            return False
    return bool(addresses) and all(address.is_global for address in addresses)


def _safe_public_host(parsed) -> bool:  # noqa: ANN001 - urllib ParseResult
    if parsed.username or parsed.password:
        return False
    try:
        parsed.port
    except ValueError:
        return False
    hostname = normalise_hostname(parsed.hostname or "")
    if not hostname or hostname in {"localhost", "local", "localdomain"}:
        return False
    if hostname.endswith((".local", ".internal", ".lan")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return "." in hostname and ".." not in hostname and _hostname_resolves_public(hostname)
    return address.is_global


def safe_public_network_url(url: str) -> bool:
    """Return whether a review request targets a public network endpoint."""

    try:
        parsed, _hostname, _port = _parse_strict_url(url, schemes=_NETWORK_SCHEMES)
    except ValueError:
        return False
    return _safe_public_host(parsed)


def safe_public_navigation_url(url: str) -> bool:
    """Return whether a review may open a public HTTPS page."""

    try:
        parsed, _hostname, _port = _parse_strict_url(url, schemes={"https"})
    except ValueError:
        return False
    return _safe_public_host(parsed)


# Provider egress manifest ---------------------------------------------------
_PASSIVE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".mjs", ".woff", ".woff2", ".ttf", ".otf",
)
_PASSIVE_PATH_HINTS = (
    "/fonts/", "/images/", "/img/", "/assets/", "/static/",
    "favicon", "logo", "analytics", "telemetry", "tracking", "collect?",
    "/track", "/pixel",
)
_CANDIDATE_KEY_RE = re.compile(
    r"(?:candidate|applicant|name|first[_ -]?name|last[_ -]?name|full[_ -]?name|"
    r"email|e[-_ ]?mail|phone|mobile|address|street|city|postcode|zip|"
    r"country|nationality|dob|birth|gender|pronoun|race|ethnic|disab|"
    r"visa|sponsor|work[_ -]?auth|linkedin|github|portfolio|resume|cv|"
    r"cover|motivation|personal[_ -]?statement|education|university|school|"
    r"degree|graduat|employment|salary|reference|answer|question|essay|"
    r"consent|legal|assessment|password|secret|token|ssn|social[_ -]?security)",
    flags=re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[^\s@/]+@[^\s@/]+\.[^\s@/]+\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
_BASE64_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/_-])([A-Za-z0-9+/_-]{16,}={0,2})(?![A-Za-z0-9+/_-])")
_PII_HEADER_KEYS = frozenset(
    {
        "authorization", "proxy-authorization", "cookie", "set-cookie",
        "x-candidate", "x-applicant", "x-personal-data", "x-pii",
        "x-resume", "x-cv", "x-user-email", "x-user-phone",
    }
)
_SAFE_BROWSER_HEADER_KEYS = frozenset(
    {
        "accept", "accept-encoding", "accept-language", "cache-control",
        "connection", "content-length", "content-type", "dnt", "host",
        "priority", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
        "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
        "upgrade-insecure-requests", "user-agent",
    }
)


def is_passive_asset(url: str) -> bool:
    """Whether a request URL looks like a passive asset/telemetry GET."""

    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return False
    path = parsed.path.casefold()
    if any(path.endswith(suffix) for suffix in _PASSIVE_SUFFIXES):
        return True
    lowered = str(url).casefold()
    return any(hint in lowered for hint in _PASSIVE_PATH_HINTS)


def _flatten_text(
    value: Any,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> str:
    if depth > _MAX_CONTAINER_DEPTH:
        return "[truncated]"
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    if seen is None:
        seen = set()
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in seen:
            return "[cycle]"
        seen.add(marker)
        try:
            return " ".join(
                f"{_flatten_text(k, depth=depth + 1, seen=seen)} "
                f"{_flatten_text(v, depth=depth + 1, seen=seen)}"
                for k, v in list(value.items())[:_MAX_CONTAINER_ITEMS]
            )
        finally:
            seen.discard(marker)
    if isinstance(value, (list, tuple, set, frozenset)):
        marker = id(value)
        if marker in seen:
            return "[cycle]"
        seen.add(marker)
        try:
            return " ".join(
                _flatten_text(item, depth=depth + 1, seen=seen)
                for item in list(value)[:_MAX_CONTAINER_ITEMS]
            )
        finally:
            seen.discard(marker)
    return str(value)


def _decode_percent_layers(value: Any, *, max_depth: int = 4) -> str:
    text = _flatten_text(value)
    for _ in range(max_depth):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def _candidate_text(
    value: Any,
    *,
    decode_base64: bool = True,
    depth: int = 0,
    seen: set[int] | None = None,
) -> bool:
    """Conservatively inspect bounded nested encodings and containers."""

    if depth > _MAX_CANDIDATE_DEPTH:
        return True
    if seen is None:
        seen = set()
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in seen:
            return True
        seen.add(marker)
        try:
            items = list(value.items())
            if len(items) > _MAX_CONTAINER_ITEMS:
                return True
            for key, item in items:
                key_text = _decode_percent_layers(key)
                if _CANDIDATE_KEY_RE.search(key_text):
                    return True
                if _candidate_text(
                    item,
                    decode_base64=decode_base64,
                    depth=depth + 1,
                    seen=seen,
                ):
                    return True
            return False
        finally:
            seen.discard(marker)
    if isinstance(value, (list, tuple, set, frozenset)):
        marker = id(value)
        if marker in seen:
            return True
        seen.add(marker)
        try:
            items = list(value)
            if len(items) > _MAX_CONTAINER_ITEMS:
                return True
            return any(
                _candidate_text(
                    item,
                    decode_base64=decode_base64,
                    depth=depth + 1,
                    seen=seen,
                )
                for item in items
            )
        finally:
            seen.discard(marker)

    text = _decode_percent_layers(value)
    if not text.strip():
        return False
    if len(text) > _MAX_CANDIDATE_CHARS:
        return True
    if _CANDIDATE_KEY_RE.search(text) or _EMAIL_RE.search(text) or _PHONE_RE.search(text):
        return True
    if decode_base64 and depth < _MAX_CANDIDATE_DEPTH:
        for token in _BASE64_TOKEN_RE.findall(text):
            if len(token) > _MAX_CANDIDATE_CHARS:
                return True
            padded = token + "=" * (-len(token) % 4)
            try:
                decoded = base64.b64decode(
                    padded.encode("ascii"), altchars=b"-_", validate=True
                )
            except (ValueError, binascii.Error, UnicodeEncodeError):
                continue
            if len(decoded) > _MAX_DECODED_BYTES:
                return True
            try:
                decoded_text = decoded.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if decoded_text != text and _candidate_text(
                decoded_text,
                decode_base64=True,
                depth=depth + 1,
                seen=seen,
            ):
                return True
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    # Scalar JSON values (for example the browser header value ``"1"``) do
    # not add structure to inspect. Re-parsing them recursively would revisit
    # the same scalar until the depth guard falsely classified it as PII.
    if not isinstance(parsed, (dict, list, tuple)):
        return False
    if parsed is value:
        return False
    return _candidate_text(
        parsed,
        decode_base64=decode_base64,
        depth=depth + 1,
        seen=seen,
    )


def _query_has_candidate_data(url: str) -> bool:
    """Inspect URL components that can carry candidate data.

    A path word such as ``/assessment`` is a semantic route name, not
    candidate data.  Treating every path token as a candidate key made a
    normal assessment page load look like a blocked data-bearing GET and
    prevented the runner from reaching its deliberate ``NEEDS_OA`` boundary.
    Query pairs and fragments remain recursively inspected, including
    percent-encoded and base64-wrapped values.  Request bodies and headers are
    inspected by their respective helpers below.
    """
    if not isinstance(url, str):
        return True
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return True
    if parsed.username or parsed.password:
        return True
    pairs = parse_qsl(parsed.query, keep_blank_values=True)

    def is_bound_greenhouse_job_id(key: str, value: str) -> bool:
        try:
            hostname = normalise_hostname(parsed.hostname or "")
        except ValueError:
            return False
        if hostname not in _GREENHOUSE_JOB_HOSTS or key.casefold() != "gh_jid":
            return False
        match = _GREENHOUSE_JOB_PATH_RE.search(parsed.path)
        return bool(match and value == match.group(1))

    return (
        any(
            not is_bound_greenhouse_job_id(key, value)
            and _candidate_text({key: value})
            for key, value in pairs
        )
        or _candidate_text(parsed.fragment)
    )


def _headers_have_candidate_data(
    headers: Mapping[str, Any] | Iterable[tuple[str, Any]] | None,
) -> bool:
    if not headers:
        return False
    items = headers.items() if isinstance(headers, Mapping) else headers
    for raw_key, raw_value in items:
        key = str(raw_key or "").casefold().strip()
        if key in _SAFE_BROWSER_HEADER_KEYS:
            continue
        if key in _PII_HEADER_KEYS or _CANDIDATE_KEY_RE.search(key):
            return True
        if _candidate_text(raw_value):
            return True
    return False


def _payload_has_candidate_data(payload: Any) -> bool:
    if payload is None or payload == "" or payload == b"":
        return False
    # Opaque bodies are data-bearing by default: adapter-specific keys must
    # not create a false-safe classification.
    return True


def classify_request(
    url: str,
    method: str,
    payload: Any = "",
    headers: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
) -> str:
    """Classify one outgoing browser request.

    Mutating methods are data-bearing even without a body. A GET/HEAD can also
    be data-bearing when candidate data appears in URL, body, or headers. A
    passive asset is only the no-body/no-candidate-data GET/HEAD case.
    """

    method_upper = str(method).strip().upper() if isinstance(method, str) else ""
    if method_upper not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT"}:
        return "data_bearing"
    mutating = method_upper in {"POST", "PUT", "PATCH", "DELETE"}
    candidate = (
        _query_has_candidate_data(url)
        or _payload_has_candidate_data(payload)
        or _headers_have_candidate_data(headers)
    )
    if mutating or candidate:
        return "data_bearing"
    if method_upper in {"GET", "HEAD"} and is_passive_asset(url):
        return "passive_asset"
    return "other"


@dataclass(frozen=True, slots=True)
class EgressDecision:
    """Pure egress verdict suitable for recording by the route guard."""

    url: str
    method: str
    classification: str
    origin: str | None
    approved: bool
    allowed: bool
    fatal: bool
    reason: str

    @property
    def record_only(self) -> bool:
        return not self.allowed and not self.fatal


class PathMatch(StrEnum):
    """How a manifest path is matched against a request path.

    ``EXACT`` requires the request path to match the manifest path
    character-for-character.  ``PREFIX`` requires the request path to start
    with the manifest path (used for versioned locale JSON files and CDN font
    paths whose exact filenames change between deployments).
    """

    EXACT = "exact"
    PREFIX = "prefix"


@dataclass(frozen=True, slots=True)
class FirstPartyServiceHost:
    """One exact vendor service endpoint measured during PREFILL."""

    host: str
    path: str
    resource_type: str
    candidate_data_kind: str | None = None
    path_match: str = PathMatch.EXACT


@dataclass(frozen=True, slots=True)
class VendorFirstPartyServiceRecord:
    """Literal input used to build one vendor's immutable service manifest."""

    registrable_domain: str
    job_board_hosts: Iterable[str]
    service_hosts: Iterable[FirstPartyServiceHost]


@dataclass(frozen=True, slots=True)
class FirstPartyServiceVendorManifest:
    """Normalised, immutable exact-host entries for one resolved vendor."""

    registrable_domain: str
    job_board_hosts: frozenset[str]
    service_hosts: Mapping[str, tuple[FirstPartyServiceHost, ...]]


# This is deliberately closed to the vendors that have measured service
# entries.  It keeps a record from redefining the permission boundary with a
# public suffix, an IP address, or an unrelated domain, without a dependency
# on live public-suffix data.
_AUTHORITATIVE_VENDOR_REGISTRABLE_DOMAINS = {
    "greenhouse": "greenhouse.io"
}


def _normalise_manifest_host(value: str, *, field: str) -> str:
    """Return one literal DNS name or reject an unsafe manifest entry."""

    if not isinstance(value, str) or not value.strip() or "*" in value:
        raise ValueError(f"{field} must be a non-empty literal hostname")
    hostname = normalise_hostname(value)
    if not _valid_hostname(hostname):
        raise ValueError(f"{field} is not a valid hostname")
    return hostname


def _validate_manifest_path(value: str, *, field: str) -> str:
    """Return one literal, query-free absolute endpoint path."""

    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value) > 2048
        or _CONTROL_CHARS.search(value)
        or "?" in value
        or "#" in value
    ):
        raise ValueError(f"{field} must be a query-free absolute path")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or parsed.path != value:
        raise ValueError(f"{field} must be a query-free absolute path")
    return value


def _host_within_registrable_domain(hostname: str, registrable_domain: str) -> bool:
    """Check a manifest declaration only; runtime lookup remains exact."""

    return hostname == registrable_domain or hostname.endswith("." + registrable_domain)


def load_first_party_service_manifest(
    records: Mapping[str, VendorFirstPartyServiceRecord],
) -> Mapping[str, FirstPartyServiceVendorManifest]:
    """Validate and freeze literal vendor service records at load time.

    This boundary deliberately allows no suffix or wildcard runtime permission:
    suffix comparison here verifies that an operator did not accidentally place
    a manifest entry on another registrable domain.
    """

    if not isinstance(records, Mapping):
        raise ValueError("first-party service manifest records must be a mapping")
    loaded: dict[str, FirstPartyServiceVendorManifest] = {}
    for raw_vendor, record in records.items():
        if not isinstance(raw_vendor, str) or not raw_vendor.strip():
            raise ValueError("vendor key must be a non-empty string")
        vendor = raw_vendor.strip().casefold()
        if vendor in loaded:
            raise ValueError(f"duplicate vendor record: {vendor}")
        if not isinstance(record, VendorFirstPartyServiceRecord):
            raise ValueError(f"vendor record for {vendor} is invalid")
        authoritative_domain = _AUTHORITATIVE_VENDOR_REGISTRABLE_DOMAINS.get(vendor)
        if authoritative_domain is None:
            raise ValueError(f"vendor {vendor} has no authoritative registrable domain")
        registrable_domain = _normalise_manifest_host(
            record.registrable_domain,
            field=f"registrable domain for {vendor}",
        )
        if registrable_domain != authoritative_domain:
            raise ValueError(
                f"registrable domain for {vendor} must match its authoritative domain"
            )

        job_board_hosts: set[str] = set()
        for raw_host in record.job_board_hosts:
            hostname = _normalise_manifest_host(
                raw_host,
                field=f"job-board host for {vendor}",
            )
            if not _host_within_registrable_domain(hostname, registrable_domain):
                raise ValueError(f"job-board host for {vendor} is outside its registrable domain")
            if hostname in job_board_hosts:
                raise ValueError(f"duplicate job-board host for {vendor}: {hostname}")
            job_board_hosts.add(hostname)

        service_hosts: dict[str, list[FirstPartyServiceHost]] = {}
        for endpoint in record.service_hosts:
            if not isinstance(endpoint, FirstPartyServiceHost):
                raise ValueError(f"service host record for {vendor} is invalid")
            hostname = _normalise_manifest_host(
                endpoint.host,
                field=f"service host for {vendor}",
            )
            if not _host_within_registrable_domain(hostname, registrable_domain):
                raise ValueError(f"service host for {vendor} is outside its registrable domain")
            resource_type = str(endpoint.resource_type or "").strip().casefold()
            if not resource_type:
                raise ValueError(f"service host resource type for {vendor} is required")
            path = _validate_manifest_path(
                endpoint.path,
                field=f"service host path for {vendor}",
            )
            candidate_data_kind = endpoint.candidate_data_kind
            if candidate_data_kind is not None:
                if not isinstance(candidate_data_kind, str):
                    raise ValueError(f"candidate-data kind for {vendor} is invalid")
                candidate_data_kind = candidate_data_kind.strip().casefold()
                if candidate_data_kind not in {"email", "location", ""}:
                    raise ValueError(f"candidate-data kind for {vendor} is invalid")
            path_match = endpoint.path_match
            if path_match not in (PathMatch.EXACT, PathMatch.PREFIX):
                raise ValueError(f"path_match for {vendor} endpoint {hostname} is invalid")
            existing = service_hosts.setdefault(hostname, [])
            for existing_ep in existing:
                if existing_ep.path == path and existing_ep.resource_type == resource_type:
                    raise ValueError(f"duplicate service host entry for {vendor}: {hostname} {path}")
            existing.append(FirstPartyServiceHost(
                host=hostname,
                path=path,
                resource_type=resource_type,
                candidate_data_kind=candidate_data_kind,
                path_match=path_match,
            ))
        loaded[vendor] = FirstPartyServiceVendorManifest(
            registrable_domain=registrable_domain,
            job_board_hosts=frozenset(job_board_hosts),
            service_hosts={k: tuple(v) for k, v in service_hosts.items()},
        )
    return loaded


FIRST_PARTY_SERVICE_MANIFEST = load_first_party_service_manifest(
    {
        "greenhouse": VendorFirstPartyServiceRecord(
            registrable_domain="greenhouse.io",
            job_board_hosts=(
                "boards.greenhouse.io",
                "job-boards.greenhouse.io",
                "job-boards.eu.greenhouse.io",
            ),
            service_hosts=(
                FirstPartyServiceHost(
                    "my.greenhouse.io",
                    "/users" + "/self",
                    "fetch",
                    candidate_data_kind="",
                ),
                FirstPartyServiceHost(
                    "email-address-validator.us.greenhouse.io",
                    "/address/validate",
                    "fetch",
                    "email",
                ),
                FirstPartyServiceHost(
                    "api-geocode-earth-proxy.greenhouse.io",
                    "/v1/autocomplete",
                    "fetch",
                    "location",
                ),
                FirstPartyServiceHost(
                    "job-boards.cdn.greenhouse.io",
                    "/assets/flags-a2kmUSbF.webp",
                    "image",
                ),
                # Locale JSON files carry the form's field labels; without them
                # the classifier has nothing to read.  Paths are versioned
                # (e.g. /locales/en/job_post.a1b2c3.json), matched by prefix.
                FirstPartyServiceHost(
                    "job-boards.cdn.greenhouse.io",
                    "/locales/en/",
                    "fetch",
                    path_match=PathMatch.PREFIX,
                ),
                # Presigned fields endpoint authorises the CV upload.
                # Query params like ``fields[]=resume`` are functional
                # metadata (server-side field selection), not candidate PII.
                # ``candidate_data_kind=""`` marks this as a known endpoint
                # whose normal query may match the data-bearing classifier
                # but carries no measured PII.
                FirstPartyServiceHost(
                    "boards.greenhouse.io",
                    "/uncacheable_attributes/presigned_fields",
                    "fetch",
                    candidate_data_kind="",
                ),
            ),
        )
    }
)


# CAPTCHA is deliberately not part of the vendor service manifest.  These are
# third-party challenge providers, authorized through a smaller, PREFILL-only
# boundary after the current page has independently matched a recognised ATS
# form.  Hosts and paths are literal: no suffixes, wildcards, or redirects.
CAPTCHA_PROVIDER_HOSTS = frozenset(
    {
        "www.google.com",
        "www.gstatic.com",
        "www.recaptcha.net",
    }
)
# These are the smallest host/path/type rules supported by the Phase 27 live
# evidence.  In particular, Chromium reported the gstatic release loader as
# ``other``; that type is accepted only inside the observed release prefix.
# Keeping the rules host-specific prevents an arbitrary path on a CAPTCHA
# provider host from becoming an implicit wildcard.
_CAPTCHA_PROVIDER_PATH_RULES: Mapping[
    str, tuple[tuple[str, frozenset[str]], ...]
] = {
    "www.google.com": (
        ("/recaptcha/api2/", frozenset({"document"})),
    ),
    "www.recaptcha.net": (
        ("/recaptcha/enterprise", frozenset({"document", "script"})),
    ),
    "www.gstatic.com": (
        (
            "/recaptcha/releases/",
            frozenset({"other", "script", "stylesheet"}),
        ),
        ("/recaptcha/api2/", frozenset({"image"})),
    ),
}


@dataclass(frozen=True, slots=True)
class CaptchaProviderDecision:
    """Closed authorization verdict for one CAPTCHA-provider request."""

    allowed: bool
    reason: str
    host: str = ""
    path: str = ""
    method: str = ""
    resource_type: str = ""
    carries_candidate_data: bool = False
    defect: bool = False


def request_carries_candidate_data(
    *,
    url: str,
    payload: Any = "",
    headers: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
) -> bool:
    """Detect candidate profile data without treating every CAPTCHA body as PII.

    Challenge protocol bodies and provider cookies are opaque authentication
    material, not permission to send the candidate's application fields.  We
    therefore inspect URL/query, body text and non-cookie headers for profile
    identifiers, while the caller still keeps the destination and request
    shape inside the closed CAPTCHA authorization below.
    """

    if _query_has_candidate_data(url) or _candidate_text(payload):
        return True
    if not headers:
        return False
    items = headers.items() if isinstance(headers, Mapping) else headers
    for raw_key, raw_value in items:
        key = str(raw_key or "").casefold().strip()
        if key in _SAFE_BROWSER_HEADER_KEYS or key in {
            "authorization",
            "proxy-authorization",
            "cookie",
            "set-cookie",
        }:
            continue
        if key in _PII_HEADER_KEYS or _CANDIDATE_KEY_RE.search(key):
            return True
        if _candidate_text(raw_value):
            return True
    return False


def authorize_captcha_request(
    *,
    url: str,
    method: str,
    resource_type: str,
    is_navigation_request: bool | None,
    is_main_frame_navigation: bool | None,
    mode: str,
    resolved_vendor: str | None,
    current_page_url: str | None,
    carries_candidate_data: bool,
    allowance: Iterable[str] = CAPTCHA_PROVIDER_HOSTS,
    vendor_manifest: Mapping[str, FirstPartyServiceVendorManifest] = (
        FIRST_PARTY_SERVICE_MANIFEST
    ),
) -> CaptchaProviderDecision:
    """Authorize the minimum exact CAPTCHA surface for recognised PREFILL forms."""

    method_value = str(method or "").strip().upper()
    type_value = str(resource_type or "").strip().casefold()
    if str(mode or "").strip().casefold() != "prefill":
        return CaptchaProviderDecision(False, "captcha_allowance_not_prefill")
    vendor_key = (
        resolved_vendor.strip().casefold()
        if isinstance(resolved_vendor, str)
        else ""
    )
    if not vendor_key:
        return CaptchaProviderDecision(False, "captcha_vendor_unresolved")
    vendor = vendor_manifest.get(vendor_key)
    if vendor is None or not isinstance(current_page_url, str):
        return CaptchaProviderDecision(
            False,
            "captcha_page_not_recognised_ats_form",
        )
    try:
        page, page_host, page_port = _parse_strict_url(
            current_page_url,
            schemes={"https"},
        )
    except ValueError:
        return CaptchaProviderDecision(
            False,
            "captcha_page_not_recognised_ats_form",
        )
    if (
        page_port not in (None, _DEFAULT_PORTS[page.scheme.casefold()])
        or page_host not in vendor.job_board_hosts
    ):
        return CaptchaProviderDecision(
            False,
            "captcha_page_not_recognised_ats_form",
        )
    try:
        request, request_host, request_port = _parse_strict_url(
            url,
            schemes={"https"},
        )
    except ValueError:
        return CaptchaProviderDecision(False, "captcha_request_invalid")
    path = request.path or "/"
    common = {
        "host": request_host,
        "path": path,
        "method": method_value,
        "resource_type": type_value,
    }
    allowed_hosts = frozenset(
        normalise_hostname(value)
        for value in allowance
        if isinstance(value, str) and value.strip() and "*" not in value
    )
    if request_port not in (None, _DEFAULT_PORTS[request.scheme.casefold()]):
        return CaptchaProviderDecision(False, "captcha_request_invalid", **common)
    if request_host not in CAPTCHA_PROVIDER_HOSTS or request_host not in allowed_hosts:
        return CaptchaProviderDecision(False, "captcha_host_not_allowed", **common)
    if carries_candidate_data:
        return CaptchaProviderDecision(
            False,
            "captcha_candidate_data_defect",
            carries_candidate_data=True,
            defect=True,
            **common,
        )
    path_value = path.casefold()
    path_rule = next(
        (
            resource_types
            for prefix, resource_types in _CAPTCHA_PROVIDER_PATH_RULES.get(
                request_host, ()
            )
            if path_value.startswith(prefix)
        ),
        None,
    )
    if path_rule is None:
        return CaptchaProviderDecision(False, "captcha_path_not_allowed", **common)
    if method_value not in {"GET", "HEAD", "POST"}:
        return CaptchaProviderDecision(False, "captcha_method_not_allowed", **common)
    if type_value not in path_rule:
        return CaptchaProviderDecision(
            False,
            "captcha_resource_type_not_allowed",
            **common,
        )
    if is_navigation_request is not False and is_main_frame_navigation is not False:
        return CaptchaProviderDecision(
            False,
            "captcha_main_frame_navigation_not_allowed",
            **common,
        )
    return CaptchaProviderDecision(
        True,
        "captcha_provider_exact_host",
        **common,
    )


class BlockedRequestImpact(StrEnum):
    """Consequence of an already-refused browser request during PREFILL.

    This classification never authorizes a request.  It answers only whether
    the owner may continue filling after the route guard has blocked it.
    """

    TOLERABLE = "tolerable"
    FATAL = "fatal"
    FIRST_PARTY_SERVICE = "first_party_service"


class BlockedResourceConsequence(StrEnum):
    """Whether a blocked resource prevents completion of the application."""

    ESSENTIAL = "essential"
    OPTIONAL = "optional"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BlockedResourceConsequenceRecord:
    """One literal resource whose completion consequence was measured."""

    host: str
    path: str
    method: str
    resource_type: str
    consequence: BlockedResourceConsequence
    reason: str


@dataclass(frozen=True, slots=True)
class BlockedRequestImpactDecision:
    impact: BlockedRequestImpact
    reason: str
    consequence: BlockedResourceConsequence = BlockedResourceConsequence.UNKNOWN
    carries_candidate_data: bool = False
    candidate_data_kind: str | None = None


# Consequence is not inferred from Chromium's resource type.  Every optional
# row below is backed by a measured provider resource whose absence does not
# prevent completion.  A miss is unknown and therefore fatal.  These rows do
# not authorize delivery: optional resources remain blocked by the route
# guard, and this table controls only whether PREFILL may continue.
_AQUATIC_BANNER_PATH = (
    "/job_board_renderer/job_board_configurations/banners/400/028/300/"
    "original/Aquatic-Logo_Color-Color-BG.png"
)
_BLOCKED_RESOURCE_CONSEQUENCE_ROWS = (
    BlockedResourceConsequenceRecord(
        "www.recaptcha.net",
        "/recaptcha/enterprise.js",
        "GET",
        "script",
        BlockedResourceConsequence.OPTIONAL,
        "captcha_provider_resource_blocked",
    ),
    BlockedResourceConsequenceRecord(
        "www.dropbox.com",
        "/static/api/2/dropins.js",
        "GET",
        "script",
        BlockedResourceConsequence.OPTIONAL,
        "optional_third_party_file_picker",
    ),
    BlockedResourceConsequenceRecord(
        "apis.google.com",
        "/js/api.js",
        "GET",
        "script",
        BlockedResourceConsequence.OPTIONAL,
        "optional_social_sign_in",
    ),
    BlockedResourceConsequenceRecord(
        "accounts.google.com",
        "/gsi/client",
        "GET",
        "script",
        BlockedResourceConsequence.OPTIONAL,
        "optional_social_sign_in",
    ),
    BlockedResourceConsequenceRecord(
        "c.spl.greenhouse.io",
        "/com.snowplowanalytics.snowplow/tp2",
        "POST",
        "fetch",
        BlockedResourceConsequence.OPTIONAL,
        "optional_telemetry",
    ),
    BlockedResourceConsequenceRecord(
        "s2-recruiting.cdn.greenhouse.io",
        _AQUATIC_BANNER_PATH,
        "GET",
        "image",
        BlockedResourceConsequence.OPTIONAL,
        "optional_decorative_image",
    ),
    BlockedResourceConsequenceRecord(
        "fonts.gstatic.com",
        "/s/roboto/v48/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3yUBA.woff2",
        "GET",
        "font",
        BlockedResourceConsequence.OPTIONAL,
        "optional_font",
    ),
    # The stylesheet that names the webfont above.  Blocking it was already
    # measured on the Point72 Greenhouse form (run ebe5b090): every control
    # rendered and stayed operable, only the typeface changed.  Registering the
    # font FILE as optional while leaving its STYLESHEET unmeasured made a
    # missing typeface fatal to prefill -- one blocked GET for CSS aborted a
    # 28-field form.  This row does not authorize delivery: the request stays
    # blocked by the route guard.  Only /css2 is listed because only /css2 was
    # measured; the v1 /css path stays unknown and therefore fatal.
    BlockedResourceConsequenceRecord(
        "fonts.googleapis.com",
        "/css2",
        "GET",
        "stylesheet",
        BlockedResourceConsequence.OPTIONAL,
        "optional_font",
    ),
    # Google Drive file picker — same treatment as the Dropbox picker
    # already above.  The file picker UI is optional; ARGUS attaches a
    # local file instead.  These stay BLOCKED but do not abort prefill.
    BlockedResourceConsequenceRecord(
        "apis.google.com",
        "/js/googleapis.proxy.js",
        "GET",
        "script",
        BlockedResourceConsequence.OPTIONAL,
        "optional_third_party_file_picker",
    ),
    BlockedResourceConsequenceRecord(
        "content.googleapis.com",
        "/static/proxy.html",
        "GET",
        "document",
        BlockedResourceConsequence.OPTIONAL,
        "optional_third_party_file_picker",
    ),
    BlockedResourceConsequenceRecord(
        "content.googleapis.com",
        "/discovery/v1/apis/drive/v3/rest",
        "GET",
        "fetch",
        BlockedResourceConsequence.OPTIONAL,
        "optional_third_party_file_picker",
    ),
    BlockedResourceConsequenceRecord(
        "content.googleapis.com",
        "/discovery/v1/apis/drive/v3/rest",
        "GET",
        "xhr",
        BlockedResourceConsequence.OPTIONAL,
        "optional_third_party_file_picker",
    ),
)
BLOCKED_RESOURCE_CONSEQUENCE_TABLE = {
    (record.host, record.path, record.method, record.resource_type): record
    for record in _BLOCKED_RESOURCE_CONSEQUENCE_ROWS
}

def _blocked_resource_consequence_record(
    *,
    url: str,
    method: str,
    resource_type: str,
) -> BlockedResourceConsequenceRecord | None:
    """Return an exact measured consequence row; never suffix-match."""

    try:
        parsed, hostname, port = _parse_strict_url(url, schemes={"https"})
    except ValueError:
        return None
    if port not in (None, _DEFAULT_PORTS[parsed.scheme.casefold()]):
        return None
    key = (
        hostname,
        parsed.path or "/",
        str(method or "").strip().upper(),
        str(resource_type or "").strip().casefold(),
    )
    return BLOCKED_RESOURCE_CONSEQUENCE_TABLE.get(key)


def canonical_first_party_service_evidence_path(
    *,
    host: str,
    path: str,
    resource_type: str,
    manifest: Mapping[str, FirstPartyServiceVendorManifest] | None = None,
) -> str | None:
    """Canonicalize a measured service path or redact an unmeasured one.

    ``None`` means the exact host is not governed by the first-party service
    manifest.  An empty string means the host is governed but the path/type is
    not the exact measured endpoint and therefore must not enter evidence.
    The scan is intentionally independent of supplied vendor metadata so a
    malformed record cannot bypass the privacy boundary.
    """

    normalized_host = normalise_hostname(host) if isinstance(host, str) else ""
    manifests = FIRST_PARTY_SERVICE_MANIFEST if manifest is None else manifest
    # Collect every endpoint for the given host across all vendors
    candidates: list[FirstPartyServiceHost] = []
    for vendor in manifests.values():
        if isinstance(vendor, FirstPartyServiceVendorManifest):
            eps = vendor.service_hosts.get(normalized_host)
            if eps:
                candidates.extend(eps)
    if not candidates:
        return None
    normalized_type = str(resource_type or "").strip().casefold()
    candidate_path = str(path or "").split("?", 1)[0].split("#", 1)[0]
    for endpoint in candidates:
        if normalized_type != endpoint.resource_type:
            continue
        if endpoint.path_match == PathMatch.EXACT:
            if candidate_path == endpoint.path:
                return endpoint.path
        elif endpoint.path_match == PathMatch.PREFIX:
            if candidate_path.startswith(endpoint.path):
                return endpoint.path
    return ""


def _first_party_service_endpoint(
    *,
    url: str,
    method: str,
    resource_type: str,
    is_navigation_request: bool | None,
    resolved_vendor: str | None,
    current_page_url: str | None,
    manifest: Mapping[str, FirstPartyServiceVendorManifest],
) -> FirstPartyServiceHost | None:
    """Return an exact manifest endpoint only when every precondition holds."""

    if is_navigation_request is not False:
        return None
    if not isinstance(resolved_vendor, str) or not resolved_vendor.strip():
        return None
    vendor = manifest.get(resolved_vendor.strip().casefold())
    if vendor is None or not isinstance(current_page_url, str):
        return None
    try:
        page, page_host, page_port = _parse_strict_url(
            current_page_url,
            schemes={"https"},
        )
        request, request_host, request_port = _parse_strict_url(url, schemes={"https"})
    except ValueError:
        return None
    if page_port not in (None, _DEFAULT_PORTS[page.scheme.casefold()]):
        return None
    if request_port not in (None, _DEFAULT_PORTS[request.scheme.casefold()]):
        return None
    if page_host not in vendor.job_board_hosts:
        return None
    endpoints = vendor.service_hosts.get(request_host)
    if not endpoints:
        return None
    request_path = request.path or "/"
    request_method = str(method or "").strip().upper()
    request_type = str(resource_type or "").strip().casefold()
    if request_method not in {"GET", "HEAD"}:
        return None
    for endpoint in endpoints:
        if request_type != endpoint.resource_type:
            continue
        if endpoint.path_match == PathMatch.EXACT:
            if request_path != endpoint.path:
                continue
        elif endpoint.path_match == PathMatch.PREFIX:
            if not request_path.startswith(endpoint.path):
                continue
        else:
            continue
        return endpoint
    return None


def classify_blocked_request_impact(
    *,
    url: str,
    method: str,
    resource_type: str,
    is_navigation_request: bool | None,
    mode: str,
    policy_classification: str | None = None,
    resolved_vendor: str | None = None,
    current_page_url: str | None = None,
    manifest: Mapping[str, FirstPartyServiceVendorManifest] | None = None,
    carries_candidate_data: bool | None = None,
    approved_hosts: Iterable[str] | None = None,
    captcha_provider_hosts: Iterable[str] | None = None,
) -> BlockedRequestImpactDecision:
    """Fail-closed impact label for a request the route guard already blocked.

    When captcha_provider_hosts is ``None`` the module-level
    CAPTCHA_PROVIDER_HOSTS is used.  Pass an explicit empty frozenset to
    disable the CAPTCHA exception (the navigator does this when the caller
    has not approved any CAPTCHA provider).
    """

    if str(mode or "").casefold() != "prefill":
        return BlockedRequestImpactDecision(
            BlockedRequestImpact.FATAL,
            "impact_classification_not_prefill",
            BlockedResourceConsequence.UNKNOWN,
        )
    endpoint = _first_party_service_endpoint(
        url=url,
        method=method,
        resource_type=resource_type,
        is_navigation_request=is_navigation_request,
        resolved_vendor=resolved_vendor,
        current_page_url=current_page_url,
        manifest=FIRST_PARTY_SERVICE_MANIFEST if manifest is None else manifest,
    )
    if endpoint is not None and not (
        endpoint.candidate_data_kind is None
        and str(policy_classification or "").casefold() == "data_bearing"
    ):
        return BlockedRequestImpactDecision(
            BlockedRequestImpact.FIRST_PARTY_SERVICE,
            "first_party_service_manifest_match",
            BlockedResourceConsequence.ESSENTIAL,
            carries_candidate_data=bool(endpoint.candidate_data_kind),
            candidate_data_kind=endpoint.candidate_data_kind or None,
        )
    try:
        parsed = urlsplit(url)
        valid_url = parsed.scheme.casefold() in {"http", "https"} and bool(
            parsed.hostname
        )
    except (AttributeError, TypeError, ValueError):
        valid_url = False
    if not valid_url:
        return BlockedRequestImpactDecision(
            BlockedRequestImpact.FATAL,
            "invalid_request_url",
            BlockedResourceConsequence.UNKNOWN,
        )
    if is_navigation_request is not False:
        # Navigation to a known CAPTCHA provider resource is tolerable.
        _captcha_hosts = (
            captcha_provider_hosts
            if captcha_provider_hosts is not None
            else CAPTCHA_PROVIDER_HOSTS
        )
        try:
            _parsed = urlsplit(url)
            _request_host = normalise_hostname(_parsed.hostname or "")
            if _request_host in _captcha_hosts:
                _path = _parsed.path or "/"
                _path_lower = _path.casefold()
                _rules = _CAPTCHA_PROVIDER_PATH_RULES.get(_request_host, ())
                for _prefix, _rtype_set in _rules:
                    if _path_lower.startswith(_prefix):
                        _rt = str(resource_type or "").strip().casefold()
                        if _rt in _rtype_set:
                            return BlockedRequestImpactDecision(
                                BlockedRequestImpact.TOLERABLE,
                                "captcha_provider_resource_blocked",
                                BlockedResourceConsequence.OPTIONAL,
                            )
        except (AttributeError, TypeError, ValueError):
            pass
        # Navigation to a known Google Drive file picker path is tolerable.
        try:
            _parsed = urlsplit(url)
            _gpath = _parsed.path or "/"
            _ghost = normalise_hostname(_parsed.hostname or "")
            for _record in _BLOCKED_RESOURCE_CONSEQUENCE_ROWS:
                if (
                    _record.host == _ghost
                    and _record.path == _gpath
                    and _record.method.upper() == "GET"
                    and _record.consequence is BlockedResourceConsequence.OPTIONAL
                ):
                    return BlockedRequestImpactDecision(
                        BlockedRequestImpact.TOLERABLE,
                        _record.reason,
                        _record.consequence,
                    )
        except (AttributeError, TypeError, ValueError):
            pass
        return BlockedRequestImpactDecision(
            BlockedRequestImpact.FATAL,
            "navigation_or_unknown_navigation_blocked",
            BlockedResourceConsequence.ESSENTIAL,
        )
    measured = _blocked_resource_consequence_record(
        url=url,
        method=method,
        resource_type=resource_type,
    )
    if measured is not None and measured.consequence is BlockedResourceConsequence.OPTIONAL:
        return BlockedRequestImpactDecision(
            BlockedRequestImpact.TOLERABLE,
            measured.reason,
            measured.consequence,
            carries_candidate_data=bool(carries_candidate_data),
        )
    if measured is not None:
        return BlockedRequestImpactDecision(
            BlockedRequestImpact.FATAL,
            measured.reason,
            measured.consequence,
            carries_candidate_data=bool(carries_candidate_data),
        )
    # CAPTCHA provider resources not in the consequence table — these match
    # the known CAPTCHA path/type rules but are NOT the exact recaptcha
    # enterprise.js entry (which is handled by the ESSENTIAL consequence
    # table above).  Making them tolerable allows known-innocuous CAPTCHA
    # assets to be blocked without aborting prefill, while the explicit
    # essential_captcha_unavailable entry still fires for the first-party
    # renderer that a CAPTCHA-aware navigator should have allowed through.
    try:
        _parsed = urlsplit(url)
        _request_host = normalise_hostname(_parsed.hostname or "")
        _captcha_hosts = (
            captcha_provider_hosts
            if captcha_provider_hosts is not None
            else CAPTCHA_PROVIDER_HOSTS
        )
        if _request_host in _captcha_hosts:
            _path = _parsed.path or "/"
            _path_lower = _path.casefold()
            _rules = _CAPTCHA_PROVIDER_PATH_RULES.get(_request_host, ())
            for _prefix, _rtype_set in _rules:
                if _path_lower.startswith(_prefix):
                    _rt = str(resource_type or "").strip().casefold()
                    if _rt in _rtype_set:
                        return BlockedRequestImpactDecision(
                            BlockedRequestImpact.TOLERABLE,
                            "captcha_provider_resource_blocked",
                            BlockedResourceConsequence.OPTIONAL,
                        )
    except (AttributeError, TypeError, ValueError):
        pass
    # Google Drive picker SCS loader scripts — dynamically versioned paths
    # like /_/scs/abc-static/_/js/k=gapi.*.  Can't exact-match in the
    # consequence table; use a path-prefix rule instead.
    try:
        _parsed = urlsplit(url)
        _request_host = normalise_hostname(_parsed.hostname or "")
        if _request_host == "apis.google.com":
            _path_lower = (_parsed.path or "/").casefold()
            if _path_lower.startswith("/_/scs/"):
                _rt = str(resource_type or "").strip().casefold()
                if _rt in ("script", "other"):
                    return BlockedRequestImpactDecision(
                        BlockedRequestImpact.TOLERABLE,
                        "optional_third_party_file_picker",
                        BlockedResourceConsequence.OPTIONAL,
                    )
    except (AttributeError, TypeError, ValueError):
        pass
    # Same registrable domain as an approved host = first-party service
    # infrastructure essential for the ATS form to render (locale JSON,
    # CDN assets, session endpoints).  Placed before the data_bearing
    # checks so even a GET carrying candidate data (e.g. session endpoint) or
    # a data-bearing query (e.g. presigned_fields) on the same registrable
    # domain does not abort prefill.
    #
    # This check only fires when resolved_vendor is not provided (the
    # diagnostic and direct-invocation paths).  The navigator passes
    # resolved_vendor and relies on the first-party service manifest
    # (with exact endpoint matching) rather than a broad RD rule.
    if approved_hosts is not None:
        _rv = (
            resolved_vendor.strip().casefold()
            if isinstance(resolved_vendor, str) and resolved_vendor.strip()
            else ""
        )
        if not _rv:
            try:
                _parsed = urlsplit(url)
                _request_host = normalise_hostname(_parsed.hostname or "")
                if _request_host:
                    for _ah in approved_hosts:
                        _ah_norm = normalise_hostname(str(_ah))
                        _rd = _registrable_domain(_request_host)
                        _ah_rd = _registrable_domain(_ah_norm)
                        if _rd and _ah_rd and _rd == _ah_rd:
                            return BlockedRequestImpactDecision(
                                BlockedRequestImpact.TOLERABLE,
                                "first_party_service_on_same_registrable_domain",
                                BlockedResourceConsequence.OPTIONAL,
                            )
            except (AttributeError, TypeError, ValueError):
                pass
    # Same-registrable-domain passive asset — fires on the PRODUCTION path
    # (when resolved_vendor IS supplied).  Content-hashed CDN assets (React
    # bundles, CSS, fonts, images) on the same registrable domain as the job
    # posting are essential for the ATS form to render.  This is narrower than
    # "allow the host": a data-bearing request (condition 1 fails) or a fetch
    # resource type on the same host stays FATAL.
    _spas = str(policy_classification or "").casefold()
    if _spas == "passive_asset":
        _spas_rt = str(resource_type or "").strip().casefold()
        if _spas_rt in {"script", "stylesheet", "font", "image", "other"}:
            try:
                _sparsed = urlsplit(url)
                _srequest_host = normalise_hostname(_sparsed.hostname or "")
                if _srequest_host and isinstance(current_page_url, str) and current_page_url.strip():
                    _scp = urlsplit(current_page_url)
                    _spage_host = normalise_hostname(_scp.hostname or "")
                    if _spage_host:
                        _sreq_rd = _registrable_domain(_srequest_host)
                        _spage_rd = _registrable_domain(_spage_host)
                        if _sreq_rd and _spage_rd and _sreq_rd == _spage_rd:
                            return BlockedRequestImpactDecision(
                                BlockedRequestImpact.FIRST_PARTY_SERVICE,
                                "first_party_service_on_same_registrable_domain",
                                BlockedResourceConsequence.ESSENTIAL,
                            )
            except (AttributeError, TypeError, ValueError):
                pass
    if carries_candidate_data is True:
        return BlockedRequestImpactDecision(
            BlockedRequestImpact.FATAL,
            "data_bearing_request_blocked",
            BlockedResourceConsequence.ESSENTIAL,
            carries_candidate_data=True,
        )
    if str(policy_classification or "").casefold() == "data_bearing":
        return BlockedRequestImpactDecision(
            BlockedRequestImpact.FATAL,
            "data_bearing_request_blocked",
            BlockedResourceConsequence.ESSENTIAL,
            carries_candidate_data=bool(carries_candidate_data),
        )
    # A passive asset on an already-approved host is tolerable: the request
    # is still blocked and recorded, but the run is not aborted for a logo
    # or font that did not load.  See brief_hermes_egress.md TASK 1.
    if (
        approved_hosts is not None
        and str(policy_classification or "").casefold() == "passive_asset"
    ):
        try:
            parsed = urlsplit(url)
            if parsed.hostname and host_matches_allowlist(
                parsed.hostname, approved_hosts
            ):
                return BlockedRequestImpactDecision(
                    BlockedRequestImpact.TOLERABLE,
                    "passive_asset_on_approved_host",
                    BlockedResourceConsequence.OPTIONAL,
                )
        except (AttributeError, TypeError, ValueError):
            pass
    return BlockedRequestImpactDecision(
        BlockedRequestImpact.FATAL,
        "unknown_resource_blocked",
        BlockedResourceConsequence.UNKNOWN,
    )


def _approved_destination(
    url: str,
    *,
    origin: str | None,
    approved_hosts: Iterable[str],
    approved_origins: Iterable[str] | None,
    classification: str,
) -> bool:
    """Match exact canonical origins, retaining bare-host compatibility."""

    if not origin:
        return False
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        port = parsed.port
    except (TypeError, ValueError):
        return False
    entries = tuple(approved_origins or ()) + tuple(approved_hosts)
    for raw_entry in entries:
        entry = str(raw_entry or "").strip()
        if not entry:
            continue
        if "://" in entry:
            try:
                if validate_origin(entry) == origin:
                    return True
            except ValueError:
                continue
            continue
        if not host_matches_allowlist(hostname, (entry,)):
            continue
        # A bare host is a compatibility shorthand, but it never authorizes a
        # non-default effective port for any traffic, including passive assets.
        default_port = _DEFAULT_PORTS.get(parsed.scheme.casefold())
        if port not in (None, default_port):
            continue
        return True
    return False


def classify_egress(
    url: str,
    method: str,
    payload: Any = "",
    *,
    approved_hosts: Iterable[str],
    approved_origins: Iterable[str] | None = None,
    headers: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
) -> EgressDecision:
    """Classify and authorize one request without performing I/O."""

    method_value = str(method).strip().upper() if isinstance(method, str) else ""
    if not isinstance(url, str) or not url:
        return EgressDecision(
            url=url if isinstance(url, str) else "",
            method=method_value,
            classification="data_bearing",
            origin=None,
            approved=False,
            allowed=False,
            fatal=True,
            reason="invalid_url_input",
        )
    try:
        classification = classify_request(url, method, payload, headers)
    except (AttributeError, RecursionError, TypeError, ValueError, UnicodeError):
        return EgressDecision(
            url=url,
            method=method_value,
            classification="data_bearing",
            origin=None,
            approved=False,
            allowed=False,
            fatal=True,
            reason="invalid_egress_input",
        )
    method_known = method_value in {
        "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT"
    }
    try:
        origin = origin_for_url(url)
        approved = method_known and _approved_destination(
            url,
            origin=origin,
            approved_hosts=approved_hosts,
            approved_origins=approved_origins,
            classification=classification,
        )
    except ValueError:
        origin = None
        approved = False
    if approved and classification == "data_bearing" and method_value in {"GET", "HEAD"}:
        # A verified origin proves where a request would go, not that a
        # candidate-bearing read is safe to deliver. Passive assets remain
        # eligible below; approved mutating form actions retain their explicit
        # submission path. GET/HEAD carrying PII require a future explicit
        # approval class rather than inheriting origin approval.
        return EgressDecision(
            url=url,
            method=method_value,
            classification=classification,
            origin=origin,
            approved=True,
            allowed=False,
            fatal=True,
            reason="approved_origin_data_bearing_get_requires_explicit_approval",
        )
    if approved:
        return EgressDecision(
            url=url,
            method=method_value,
            classification=classification,
            origin=origin,
            approved=True,
            allowed=True,
            fatal=False,
            reason="approved_origin",
        )
    if classification == "passive_asset" and origin is not None:
        return EgressDecision(
            url=url,
            method=method_value,
            classification=classification,
            origin=origin,
            approved=False,
            allowed=False,
            fatal=False,
            reason="passive_asset_unapproved_record_only",
        )
    return EgressDecision(
        url=url,
        method=method_value,
        classification=classification,
        origin=origin,
        approved=False,
        allowed=False,
        fatal=True,
        reason=(
            "data_bearing_unapproved_origin"
            if classification == "data_bearing"
            else "unapproved_nonpassive_request"
        ),
    )


def egress_allowed(
    url: str,
    method: str,
    payload: Any = "",
    *,
    approved_hosts: Iterable[str],
    approved_origins: Iterable[str] | None = None,
    headers: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
) -> bool:
    """Compatibility boolean: only approved requests are delivered."""

    return classify_egress(
        url,
        method,
        payload,
        approved_hosts=approved_hosts,
        approved_origins=approved_origins,
        headers=headers,
    ).allowed
