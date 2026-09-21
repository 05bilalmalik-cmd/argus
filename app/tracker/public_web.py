"""Bounded public job-page fetching and deterministic role-page parsing.

The network client is deliberately small: it performs unauthenticated GETs only,
validates every redirect, resolves DNS once per connection, and connects to the
validated address rather than allowing a second DNS lookup.  HTML is parsed as
data; no script is executed and response bodies are never part of the public
result dictionary.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from html import unescape
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


class PublicWebError(ValueError):
    """A URL or public response violates the bounded public-web policy."""


_GENERIC_SEGMENTS = frozenset(
    {
        "careers",
        "career",
        "jobs",
        "job-listings",
        "listing",
        "listings",
        "opportunities",
        "positions",
        "roles",
        "search",
        "search-results",
        "vacancies",
    }
)
_ROLE_SEGMENTS = frozenset({"job", "jobs", "role", "roles", "posting", "postings", "position", "j"})
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "for",
        "in",
        "of",
        "on",
        "the",
        "to",
        "uk",
        "united",
        "kingdom",
    }
)
_BLOCKED_MARKERS = re.compile(
    r"access\s+denied|verify\s+(?:you\s+are|that\s+you(?:'re|\s+are))\s+human|"
    r"captcha|cloudflare|checking\s+your\s+browser|enable\s+javascript|bot\s+check",
    re.IGNORECASE,
)
_CLOSED_MARKERS = re.compile(
    r"(?:this\s+)?(?:position|role|job|vacancy)\s+(?:is\s+)?(?:closed|no\s+longer\s+available)|"
    r"applications?\s+(?:are\s+)?closed|no\s+longer\s+accept(?:ing|s)\s+applications?",
    re.IGNORECASE,
)
_APPLY_MARKERS = re.compile(
    r"\b(?:apply|application|submit\s+application|start\s+application|register\s+interest)\b",
    re.IGNORECASE,
)
_DEADLINE_LABEL = re.compile(
    r"\b(?:applications?\s+)?(?:deadline|closing\s+date|close(?:s|d)?|apply\s+by)\b"
    r"\s*(?::|[-–—])?\s*([^|\n.;]{2,100})",
    re.IGNORECASE,
)
_POSTED_LABEL = re.compile(
    r"(?:date\s+)?(?:posted|published|added)\s*(?::|[-–—])?\s*([^|\n.;]{2,100})",
    re.IGNORECASE,
)
_DATE_PATTERNS = (
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?\b"),
    re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+\d{4}\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+\d{4}\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b"),
)


@dataclass(frozen=True, slots=True)
class ResolvedURL:
    """A URL plus the public addresses resolved immediately before connecting."""

    url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


@dataclass(slots=True)
class WebPageResult:
    """Safe role facts; ``body`` is transient and excluded from ``as_dict``."""

    availability: str = "unknown"
    verification_error: str = ""
    deadline: str | None = None
    deadline_text: str = ""
    deadline_basis: str = "unknown"
    posted_at: str | None = None
    posted_text: str = ""
    evidence: list[dict[str, str]] = field(default_factory=list)
    source_url: str = ""
    final_url: str = ""
    observed_at: str = ""
    source_response_hash: str | None = None
    status_code: int | None = None
    title: str = ""
    employer: str = ""
    location: str = ""
    role_text: str = ""
    body: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.final_url:
            self.final_url = self.source_url
        if not self.observed_at:
            self.observed_at = datetime.now(timezone.utc).isoformat()
        if self.source_response_hash is None and self.body is not None:
            self.source_response_hash = hashlib.sha256(self.body).hexdigest()
        self.evidence = _safe_evidence(self.evidence, self.source_url or self.final_url, self.observed_at)

    def as_dict(self) -> dict[str, Any]:
        """Return API-safe facts without the response body."""

        return {
            "availability": self.availability,
            "verification_error": self.verification_error,
            "deadline": self.deadline,
            "deadline_text": self.deadline_text,
            "deadline_basis": self.deadline_basis,
            "posted_at": self.posted_at,
            "posted_text": self.posted_text,
            "evidence": [dict(item) for item in self.evidence],
            "source_url": self.source_url,
            "final_url": self.final_url,
            "observed_at": self.observed_at,
            "source_response_hash": self.source_response_hash,
            "status_code": self.status_code,
            "title": self.title,
            "employer": self.employer,
            "location": self.location,
            "role_text": self.role_text[:6000],
        }


def _safe_evidence(items: object, source_url: str, observed_at: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if not isinstance(items, list):
        return result
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        quote = _clean(item.get("quote"))[:500]
        if not quote:
            continue
        source = _clean(item.get("source_url")) or source_url
        observed = _clean(item.get("observed_at")) or observed_at
        key = (quote, source)
        if key in seen:
            continue
        seen.add(key)
        result.append({"quote": quote, "source_url": source, "observed_at": observed})
    return result


def _clean(value: object) -> str:
    if value is None:
        return ""
    text = unescape(str(value))
    text = re.sub(r"<[^>]*>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalised(value: object) -> str:
    text = _clean(value).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _tokens(value: object) -> set[str]:
    return {token for token in _normalised(value).split() if token not in _STOPWORDS and len(token) > 1}


def _title_score(expected: str, candidate: str) -> float:
    left, right = _normalised(expected), _normalised(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens))
    return max(overlap, SequenceMatcher(None, left, right).ratio() * 0.85)


def _is_generic_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return True
    segments = [part.casefold() for part in parsed.path.split("/") if part]
    if not segments:
        return True
    if set(part.casefold() for part in re.findall(r"[^=&]+", parsed.query)) & {
        "q",
        "query",
        "search",
        "keyword",
        "page",
        "location",
    }:
        return True
    return segments[-1] in _GENERIC_SEGMENTS or "search" in segments


def _is_role_candidate(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    raw_type = value.get("@type")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    return any(str(item).casefold() == "jobposting" for item in types)


def _iter_jsonld(value: object, depth: int = 0):
    if depth > 6:
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_jsonld(item, depth + 1)
    elif isinstance(value, Mapping):
        if _is_role_candidate(value):
            yield dict(value)
        for key in ("@graph", "mainEntity", "itemListElement", "subjectOf"):
            if key in value:
                yield from _iter_jsonld(value[key], depth + 1)


def _jsonld_postings(soup: BeautifulSoup) -> list[dict[str, Any]]:
    postings: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text()
        if not raw or len(raw) > 250_000:
            continue
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        postings.extend(_iter_jsonld(decoded))
    return postings


def _select_posting(postings: list[dict[str, Any]], expected_title: str) -> dict[str, Any] | None:
    if not postings:
        return None
    if len(postings) == 1 and not expected_title:
        return postings[0]
    scored = sorted(
        ((
            _title_score(expected_title, _clean(item.get("title") or item.get("name"))),
            index,
            item,
        ) for index, item in enumerate(postings)
 if not expected_title or not (set(re.findall(r"\b20\d{2}\b", expected_title))
     and set(re.findall(r"\b20\d{2}\b", _clean(item.get("title") or item.get("name"))))
     and set(re.findall(r"\b20\d{2}\b", expected_title)) !=
         set(re.findall(r"\b20\d{2}\b", _clean(item.get("title") or item.get("name")))))),
        reverse=True,
    )
    if not expected_title:
        return None if len(postings) > 1 else postings[0]
    if not scored:
        return None
    best_score, _, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < 0.9 or (len(scored) > 1 and best_score == second_score and best_score < 0.9):
        return None
    return best


def _nested_text(value: object, *keys: str) -> str:
    if isinstance(value, Mapping):
        for key in keys:
            text = _clean(value.get(key))
            if text:
                return text
    return _clean(value) if isinstance(value, str) else ""


def _scope(soup: BeautifulSoup) -> BeautifulSoup | Any:
    for selector in ("main", "article", "[itemtype*='JobPosting']"):
        found = soup.select_one(selector)
        if found is not None:
            return found
    body = soup.body
    if body is None:
        return soup
    for tag in body.find_all(("nav", "header", "footer", "aside", "script", "style", "noscript")):
        tag.decompose()
    return body


def _visible_text(scope: Any) -> str:
    return _clean(scope.get_text(" ", strip=True) if hasattr(scope, "get_text") else scope)


def _heading(scope: Any) -> str:
    if hasattr(scope, "find"):
        heading = scope.find(["h1", "h2"])
        if heading is not None:
            return _clean(heading.get_text(" ", strip=True))
    return ""


def _label_value(text: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(text)
    if not match:
        return ""
    value = _clean(match.group(1))
    # Role text often renders the next control immediately after a date (for
    # example, ``Deadline: 15 October Apply``). Keep the original date token,
    # not unrelated neighbouring control text.
    candidate = _find_date_candidate(value, allow_yearless=True)
    return candidate or value


def _find_date_candidate(text: str, *, allow_yearless: bool = False) -> str:
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            return _clean(match.group(0))
    if allow_yearless:
        match = re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\b", text, re.I)
        if match:
            return _clean(match.group(0))
    return ""


def _parse_date(value: object) -> str | None:
    raw = _clean(value)
    if not raw:
        return None
    raw = raw.replace("–", "-").replace("—", "-")
    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ]|$)", raw)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).isoformat()
        except ValueError:
            return None
    stripped = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw, flags=re.I)
    for fmt in (
        "%d %B %Y",
        "%d %b %Y",
        "%B %d %Y",
        "%b %d %Y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(stripped.replace(",", ""), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_timestamp(value: object) -> str | None:
    raw = _clean(value)
    if not raw:
        return None
    if re.search(r"\b(?:ago|yesterday|today|tomorrow)\b", raw, re.I):
        return None
    parsed_date = _parse_date(raw)
    if parsed_date is not None and len(raw) <= 30:
        return f"{parsed_date}T00:00:00+00:00"
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _evidence(evidence: list[dict[str, str]], quote: object, source_url: str, observed_at: str) -> None:
    clean_quote = _clean(quote)[:500]
    if not clean_quote:
        return
    evidence.append({"quote": clean_quote, "source_url": source_url, "observed_at": observed_at})


def _location_from_posting(posting: Mapping[str, Any] | None) -> str:
    if not posting:
        return ""
    value = posting.get("jobLocation") or posting.get("jobLocationType")
    if isinstance(value, list):
        values = [_location_from_posting(item) for item in value]
        return "; ".join(item for item in values if item)
    if isinstance(value, Mapping):
        address = value.get("address") or value
        if isinstance(address, Mapping):
            parts = [
                _clean(address.get(key))
                for key in ("addressLocality", "addressRegion", "addressCountry", "name")
            ]
            return ", ".join(dict.fromkeys(part for part in parts if part))
        return _clean(address)
    return _clean(value)


def _role_error(
    message: str,
    *,
    source_url: str,
    final_url: str,
    observed_at: str,
    status_code: int | None,
    body: bytes | None,
    evidence: list[dict[str, str]] | None = None,
    title: str = "",
    role_text: str = "",
) -> WebPageResult:
    return WebPageResult(
        availability="unknown",
        verification_error=message,
        source_url=source_url,
        final_url=final_url,
        observed_at=observed_at,
        status_code=status_code,
        body=body,
        evidence=evidence or [],
        title=title,
        role_text=role_text,
    )


def parse_job_page(
    html: str | bytes,
    source_url: str,
    *,
    expected_title: str = "",
    expected_employer: str = "",
    observed_at: str | None = None,
    final_url: str | None = None,
    status_code: int = 200,
    response_hash: str | None = None,
) -> WebPageResult:
    """Parse one page without executing or trusting unrelated page text."""

    observed = observed_at or datetime.now(timezone.utc).isoformat()
    target_url = final_url or source_url
    body = html if isinstance(html, bytes) else html.encode("utf-8", errors="replace")
    try:
        text = body.decode("utf-8", errors="replace")
    except AttributeError:
        text = str(html)
        body = text.encode("utf-8", errors="replace")
    digest = response_hash or hashlib.sha256(body).hexdigest()
    if status_code in {404, 410}:
        return WebPageResult(
            availability="closed",
            verification_error=f"HTTP {status_code}",
            source_url=source_url,
            final_url=target_url,
            observed_at=observed,
            status_code=status_code,
            source_response_hash=digest,
            body=body,
        )
    if status_code < 200 or status_code >= 400:
        return _role_error(
            f"HTTP {status_code}",
            source_url=source_url,
            final_url=target_url,
            observed_at=observed,
            status_code=status_code,
            body=body,
        )

    soup = BeautifulSoup(text[:1_500_000], "html.parser")
    postings = _jsonld_postings(soup)
    posting = _select_posting(postings, expected_title)
    role_scope = _scope(soup)
    structured_description = _clean(posting.get("description")) if posting else ""
    role_text = _clean(" ".join(part for part in (_visible_text(role_scope), structured_description) if part))[:6000]
    heading = _heading(role_scope)
    structured_title = _clean(posting.get("title") or posting.get("name")) if posting else ""
    title = structured_title or heading
    title_match = bool(title) and (
        not expected_title or _title_score(expected_title, title) >= 0.45
    )
    if posting is None and expected_title and heading and _title_score(expected_title, heading) >= 0.45:
        title_match = True
    if not expected_title and len(postings) == 1:
        title_match = bool(title)
    employer_value = _nested_text(posting.get("hiringOrganization") if posting else "", "name")
    if not employer_value:
        employer_value = expected_employer if title_match else ""
    location = _location_from_posting(posting)
    role_source = target_url
    evidence: list[dict[str, str]] = []
    _evidence(evidence, title, role_source, observed)
    if employer_value:
        _evidence(evidence, employer_value, role_source, observed)
    if structured_description:
        _evidence(evidence, structured_description, role_source, observed)
    if location:
        _evidence(evidence, location, role_source, observed)

    if _BLOCKED_MARKERS.search(role_text):
        return _role_error(
            "blocked or bot-check page",
            source_url=source_url,
            final_url=target_url,
            observed_at=observed,
            status_code=status_code,
            body=body,
            evidence=evidence,
            title=title,
            role_text=role_text,
        )
    if not title_match:
        reason = "generic or unrelated page" if _is_generic_url(target_url) else "role identity not verified"
        return _role_error(
            reason,
            source_url=source_url,
            final_url=target_url,
            observed_at=observed,
            status_code=status_code,
            body=body,
            evidence=evidence,
            title=title,
            role_text=role_text,
        )
    if expected_employer and employer_value:
        if _title_score(expected_employer, employer_value) < 0.45:
            return _role_error(
                "employer identity mismatch",
                source_url=source_url,
                final_url=target_url,
                observed_at=observed,
                status_code=status_code,
                body=body,
                evidence=evidence,
                title=title,
                role_text=role_text,
            )

    apply_quote = ""
    if hasattr(role_scope, "find_all"):
        for tag in role_scope.find_all(("a", "button", "input")):
            label = _clean(tag.get("value") or tag.get_text(" ", strip=True))
            if _APPLY_MARKERS.search(label) and (tag.name != "a" or tag.get("href")):
                apply_quote = label
                break
    if apply_quote:
        _evidence(evidence, apply_quote, role_source, observed)
    deadline_text = _label_value(role_text, _DEADLINE_LABEL)
    posted_text = _label_value(role_text, _POSTED_LABEL)
    structured_deadline = _clean(posting.get("validThrough")) if posting else ""
    structured_posted = _clean(posting.get("datePosted")) if posting else ""
    if not deadline_text:
        deadline_text = structured_deadline
    if not posted_text:
        posted_text = structured_posted
    deadline = _parse_date(structured_deadline) if structured_deadline else None
    deadline_basis = "explicit_valid_through" if deadline else "unknown"
    if deadline is None and deadline_text:
        deadline = _parse_date(deadline_text)
        deadline_basis = "explicit_text" if deadline else "unknown_year"
    if deadline_text:
        _evidence(evidence, deadline_text, role_source, observed)
    posted_at = _parse_timestamp(structured_posted) if structured_posted else None
    if posted_at is None and posted_text:
        posted_at = _parse_timestamp(posted_text)
    if posted_text:
        _evidence(evidence, posted_text, role_source, observed)

    if _CLOSED_MARKERS.search(role_text):
        return WebPageResult(
            availability="closed",
            verification_error="role explicitly closed",
            deadline=deadline,
            deadline_text=deadline_text,
            deadline_basis=deadline_basis,
            posted_at=posted_at,
            posted_text=posted_text,
            evidence=evidence,
            source_url=source_url,
            final_url=target_url,
            observed_at=observed,
            source_response_hash=digest,
            status_code=status_code,
            title=title,
            employer=employer_value,
            location=location,
            role_text=role_text,
            body=body,
        )
    if not apply_quote:
        return WebPageResult(
            availability="unknown",
            verification_error="role found but apply control was absent",
            deadline=deadline,
            deadline_text=deadline_text,
            deadline_basis=deadline_basis,
            posted_at=posted_at,
            posted_text=posted_text,
            evidence=evidence,
            source_url=source_url,
            final_url=target_url,
            observed_at=observed,
            source_response_hash=digest,
            status_code=status_code,
            title=title,
            employer=employer_value,
            location=location,
            role_text=role_text,
            body=body,
        )
    return WebPageResult(
        availability="open",
        verification_error="",
        deadline=deadline,
        deadline_text=deadline_text,
        deadline_basis=deadline_basis,
        posted_at=posted_at,
        posted_text=posted_text,
        evidence=evidence,
        source_url=source_url,
        final_url=target_url,
        observed_at=observed,
        source_response_hash=digest,
        status_code=status_code,
        title=title,
        employer=employer_value,
        location=location,
        role_text=role_text,
        body=body,
    )


def validate_public_url(url: str) -> str:
    """Validate URL syntax before any resolver or socket is touched."""

    if not isinstance(url, str) or not url or any(ord(char) < 32 for char in url):
        raise PublicWebError("URL must be printable text")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise PublicWebError("URL is malformed") from exc
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if scheme not in {"http", "https"} or not hostname or not parsed.netloc:
        raise PublicWebError("only absolute public http(s) URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise PublicWebError("credential-bearing URLs are not allowed")
    if port is not None and not 1 <= port <= 65535:
        raise PublicWebError("URL port is invalid")
    if hostname in {"localhost", "local", "localdomain"} or hostname.endswith(
        (".localhost", ".local", ".internal", ".lan", ".home", ".test", ".invalid")
    ):
        raise PublicWebError("special-use host is not public")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise PublicWebError("private or special-use address is not allowed")
    return url


def resolve_public_url(url: str) -> ResolvedURL:
    """Resolve every address and reject a hostname with any non-public answer."""

    validate_public_url(url)
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    try:
        port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    except ValueError as exc:
        raise PublicWebError("URL port is invalid") from exc
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            answers = socket.getaddrinfo(
                hostname,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise PublicWebError("public hostname did not resolve") from exc
        addresses = []
        for answer in answers:
            sockaddr = answer[4]
            raw = sockaddr[0] if sockaddr else ""
            try:
                address = ipaddress.ip_address(raw)
            except ValueError as exc:
                raise PublicWebError("resolver returned an invalid address") from exc
            if not address.is_global:
                raise PublicWebError("hostname resolved to a private or special-use address")
            if str(address) not in addresses:
                addresses.append(str(address))
    if not addresses:
        raise PublicWebError("public hostname did not resolve")
    return ResolvedURL(
        url=url,
        scheme=parsed.scheme.casefold(),
        hostname=hostname,
        port=port,
        addresses=tuple(addresses),
    )


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, pinned_address: str, *, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_address = pinned_address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_address, self.port), self.timeout)
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        pinned_address: str,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ):
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._pinned_address = pinned_address

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_address, self.port), self.timeout)
        if self._tunnel_host:
            self.sock = raw
            self._tunnel()
            raw = self.sock
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class PublicWebClient:
    """Bounded public GET client with no proxy/environment trust."""

    trust_env = False

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        max_body_bytes: int = 1_500_000,
        max_redirects: int = 3,
        user_agent: str = "ARGUS-tracker/2.0 (+public internship intelligence)",
    ) -> None:
        if timeout_seconds <= 0 or max_body_bytes < 1 or max_redirects < 0:
            raise ValueError("invalid public web bounds")
        self.timeout_seconds = float(timeout_seconds)
        self.max_body_bytes = int(max_body_bytes)
        self.max_redirects = int(max_redirects)
        self.user_agent = user_agent

    def _request(self, resolved: ResolvedURL) -> tuple[int, dict[str, str], bytes]:
        context = ssl.create_default_context() if resolved.scheme == "https" else None
        connection: http.client.HTTPConnection
        if resolved.scheme == "https":
            connection = _PinnedHTTPSConnection(
                resolved.hostname,
                resolved.port,
                resolved.addresses[0],
                timeout=self.timeout_seconds,
                context=context or ssl.create_default_context(),
            )
        else:
            connection = _PinnedHTTPConnection(
                resolved.hostname,
                resolved.port,
                resolved.addresses[0],
                timeout=self.timeout_seconds,
            )
        try:
            path = urlsplit(resolved.url).path or "/"
            query = urlsplit(resolved.url).query
            if query:
                path += "?" + query
            connection.request(
                "GET",
                path,
                headers={
                    "Host": resolved.hostname,
                    "User-Agent": self.user_agent,
                    "Accept": "application/json" if '/wday/cxs/' in path else "text/html,application/xhtml+xml;q=0.9",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            headers = {key.casefold(): value for key, value in response.getheaders()}
            content_length = headers.get("content-length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise PublicWebError("invalid response content length") from exc
                if declared_length < 0:
                    raise PublicWebError("invalid response content length")
                if declared_length > self.max_body_bytes:
                    raise PublicWebError("response body exceeds bound")
            chunks: list[bytes] = []
            total = 0
            started = time.monotonic()
            while True:
                if time.monotonic() - started > self.timeout_seconds:
                    raise PublicWebError("response time exceeds bound")
                chunk = response.read(min(64 * 1024, self.max_body_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_body_bytes:
                    raise PublicWebError("response body exceeds bound")
                chunks.append(chunk)
            return int(response.status), headers, b"".join(chunks)
        finally:
            connection.close()

    def fetch(
        self,
        url: str,
        *,
        expected_title: str = "",
        expected_employer: str = "",
        observed_at: str | None = None,
    ) -> WebPageResult:
        from app.tracker.workday_verification import workday_api_url, parse_workday_payload
        current = validate_public_url(url)
        original = current
        workday_api = workday_api_url(current)
        if workday_api:
            current = workday_api
        redirects = 0
        while True:
            resolved = resolve_public_url(current)
            try:
                status, headers, body = self._request(resolved)
            except PublicWebError:
                raise
            except (OSError, http.client.HTTPException, ssl.SSLError, TimeoutError) as exc:
                return _role_error(
                    f"network failure: {type(exc).__name__}",
                    source_url=original,
                    final_url=current,
                    observed_at=observed_at or datetime.now(timezone.utc).isoformat(),
                    status_code=None,
                    body=None,
                )
            if status in {301, 302, 303, 307, 308}:
                location = headers.get("location")
                if not location:
                    return _role_error(
                        "redirect response had no location",
                        source_url=original,
                        final_url=current,
                        observed_at=observed_at or datetime.now(timezone.utc).isoformat(),
                        status_code=status,
                        body=body,
                    )
                redirects += 1
                if redirects > self.max_redirects:
                    return _role_error(
                        "redirect limit reached",
                        source_url=original,
                        final_url=current,
                        observed_at=observed_at or datetime.now(timezone.utc).isoformat(),
                        status_code=status,
                        body=body,
                    )
                current = validate_public_url(urljoin(current, location))
                continue
            if workday_api:
                if urlsplit(current).netloc != urlsplit(workday_api).netloc:
                    raise PublicWebError("Workday detail redirected away from its employer host")
                return parse_workday_payload(body, original, current, expected_title=expected_title,
                    expected_employer=expected_employer, observed_at=observed_at, status_code=status)
            content_type = headers.get("content-type", "").casefold()
            if content_type and not any(kind in content_type for kind in ("text/html", "application/xhtml", "text/plain")):
                return _role_error(
                    "response was not a public HTML page",
                    source_url=original,
                    final_url=current,
                    observed_at=observed_at or datetime.now(timezone.utc).isoformat(),
                    status_code=status,
                    body=body,
                )
            return parse_job_page(
                body,
                original,
                expected_title=expected_title,
                expected_employer=expected_employer,
                observed_at=observed_at,
                final_url=current,
                status_code=status,
            )


# Friendly aliases for parent integrations and focused parser tests.
fetch_public_job = PublicWebClient().fetch
parse_public_job_page = parse_job_page


__all__ = [
    "PublicWebClient",
    "PublicWebError",
    "ResolvedURL",
    "WebPageResult",
    "fetch_public_job",
    "parse_job_page",
    "parse_public_job_page",
    "resolve_public_url",
    "validate_public_url",
]
