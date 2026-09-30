"""Shared plumbing for ARGUS headless job-board sources.

Everything here is deliberately dependency-light: ``httpx`` is imported lazily
inside :func:`http_get` so the parsing half of the package (and all unit tests)
run without any network stack installed.
"""
from __future__ import annotations

import functools
import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit, urlunsplit

from app.domain.targets import source_identity_key

if TYPE_CHECKING:
    from app.scouting.trackr import ScrapedOpportunity

logger = logging.getLogger(__name__)

# Realistic browser-ish UA: some career sites 403 obvious bot agents.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 20  # seconds — applied to every outbound call

# --- Early-careers title filter -------------------------------------------
# Keep titles that smell like spring weeks / placements / internships;
# drop graduate schemes and experienced hires outright.
EARLY_CAREERS_TITLE = re.compile(
    r"spring|placement|industrial|internship|\bintern\b|off[\s-]*cycle|"
    r"insight|year\s*-?\s*in\s*-?\s*industry|sandwich|summer|trainee",
    re.I,
)
DROP_TITLE = re.compile(
    r"graduate\s+(?:scheme|programme|program)|full\s*-?\s*time|part\s*-?\s*time|"
    r"apprenticeship|\bphd\b|postdoc|experienced\s+hire|\bsenior\b",
    re.I,
)

def is_early_careers(*texts: str | None) -> bool:
    """True when the combined text looks like an early-careers programme."""
    blob = " ".join(t for t in texts if t)
    if not blob:
        return False
    return bool(EARLY_CAREERS_TITLE.search(blob)) and not DROP_TITLE.search(blob)


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _host_matches(host: str, suffix: str) -> bool:
    """Match one registrable host without accepting lookalike boundaries."""

    return host == suffix or host.endswith(f".{suffix}")


_ATS_HOST_SUFFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("greenhouse", ("greenhouse.io",)),
    ("lever", ("lever.co",)),
    ("workday", ("myworkdayjobs.com", "myworkdaysite.com")),
    ("smartrecruiters", ("smartrecruiters.com",)),
    ("workable", ("workable.com",)),
    ("ashby", ("ashbyhq.com",)),
    ("icims", ("icims.com",)),
    ("taleo", ("taleo.net",)),
    ("successfactors", ("successfactors.com", "successfactors.eu")),
    ("oracle", ("oraclecloud.com",)),
    ("talnet", ("tal.net",)),
    ("eightfold", ("eightfold.ai",)),
    ("teamtailor", ("teamtailor.com",)),
    ("recruitee", ("recruitee.com",)),
    ("personio", ("personio.com", "personio.de")),
)


def _ats_from_url(url: str) -> str:
    """Classify a URL by its parsed hostname, without guessing from text.

    ``grnh.se`` is deliberately a resolution marker rather than confirmed
    Greenhouse identity: observed redirects can terminate on employer-owned
    sites instead of a Greenhouse form.
    """

    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").casefold().rstrip(".")
    except ValueError:
        return "unknown"
    if not host:
        return "unknown"
    if host == "grnh.se":
        return "greenhouse_shortlink"
    for provider, suffixes in _ATS_HOST_SUFFIXES:
        if any(_host_matches(host, suffix) for suffix in suffixes):
            return provider
    return "unknown"


def _parse_deadline(raw: Any) -> date | None:
    raw = _clean(raw)
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(re.sub(r"(\d)(st|nd|rd|th)", r"\1", raw), fmt)
            return parsed.date()
        except ValueError:
            continue
    return None


def normalize_url(url: str | None) -> str:
    """Canonical dedup key: lowercase scheme/host, no fragment, no trailing slash.

    Query strings are kept (some ATS put the req id there); fragments dropped.
    """
    raw = (url or "").strip()
    parts = urlsplit(raw)
    if not parts.scheme and not parts.netloc:
        return raw.rstrip("/")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, "")
    )


def http_get(url: str) -> str:
    """GET ``url`` with the shared UA + timeout; returns response text."""
    import httpx  # lazy: keeps unit tests network-stack-free

    response = httpx.get(
        url,
        timeout=HTTP_TIMEOUT,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        },
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def http_get_json(url: str) -> Any:
    """GET ``url`` and JSON-decode; raises on transport or decode errors."""
    return json.loads(http_get(url))


def dedupe(opportunities: Iterable[ScrapedOpportunity]) -> list[ScrapedOpportunity]:
    """Merge exact role/source identities while retaining roles sharing a page."""
    unique: dict[tuple[str, str, str, str, str, str], ScrapedOpportunity] = {}
    for opp in opportunities:
        key = source_identity_key(
            employer=opp.employer,
            role_title=opp.role_title,
            cycle="",
            source_url=opp.source_url,
            location=opp.location,
            division=opp.division,
        )
        if key not in unique:
            unique[key] = opp
    return list(unique.values())


Source = tuple[str, Callable[[], list["ScrapedOpportunity"]]]

# Rows must carry the attributes the merge/filter below read; anything else
# is a malformed source row, rejected per source (never a sweep-wide crash).
_REQUIRED_ROW_ATTRS = ("employer", "role_title", "source_url", "location", "division")


def _safe_source_error(exc: BaseException) -> str:
    """Fixed whitelist: exception type + known category, never message text.

    Transport messages routinely echo request URLs and credentials, so no
    arbitrary exception message is ever retained — only the type name plus a
    fixed category. HTTP status codes are preserved as useful diagnostics.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        try:
            code = int(status_code)
        except (TypeError, ValueError):
            code = None
        if code is not None:
            return f"{type(exc).__name__}: HTTP {code}"
    kind = type(exc).__name__
    if "Timeout" in kind or "TimedOut" in kind:
        return f"{kind}: timed out"
    if "Connect" in kind:
        return f"{kind}: connection failed"
    if "SSL" in kind or "Certificate" in kind:
        return f"{kind}: TLS failed"
    return kind


class PartialFetchResult(list):
    """List subclass carrying partial page-failure metadata for aggregators.

    Allows the collector to distinguish between:
    - healthy empty (all pages succeeded, zero rows)
    - partial success (some pages failed, some rows collected)
    - total failure (all pages failed, metadata carried without raising)
    """

    __slots__ = ("failed_pages", "total_pages", "failed_urls", "collector_error")

    def __init__(
        self,
        rows: list["ScrapedOpportunity"],
        *,
        failed_pages: int,
        total_pages: int,
        failed_urls: tuple[str, ...] = (),
        collector_error: str | None = None,
    ):
        super().__init__(rows)
        self.failed_pages = failed_pages
        self.total_pages = total_pages
        self.failed_urls = failed_urls
        self.collector_error = collector_error


def _malformed_row_reason(rows: list) -> str | None:
    # Every attribute read by source_identity_key (dedupe) and the
    # early-careers filter must already be text: a non-string value would
    # crash the global merge and discard every earlier good source.
    for row in rows:
        for attr in _REQUIRED_ROW_ATTRS:
            if not isinstance(getattr(row, attr, None), str):
                return (
                    f"malformed row: {type(row).__name__} "
                    f"has a non-text {attr}"
                )
    return None


def build_sources(settings: Mapping | None = None) -> list[Source]:
    """Registered source callables for one sweep (watchlist-driven)."""
    from app.scouting.sources import aggregators, greenhouse, lever, watchlist

    watchlist_map = watchlist.load_watchlist(settings)
    gh_orgs: set[str] = set(greenhouse.ORGS)
    lev_companies: set[str] = set(lever.COMPANIES)
    for spec in watchlist_map.values():
        gh_orgs.update(spec.get("greenhouse") or [])
        lev_companies.update(spec.get("lever") or [])

    sources: list[Source] = [
        (f"greenhouse:{org}", functools.partial(greenhouse.fetch_org, org))
        for org in sorted(gh_orgs)
    ]
    sources += [
        (f"lever:{company}", functools.partial(lever.fetch_company, company))
        for company in sorted(lev_companies)
    ]
    sources.append(("brightnetwork", aggregators.fetch_brightnetwork))
    sources.append(("ratemyplacement", aggregators.fetch_ratemyplacement))
    sources.append(("wikijob", aggregators.fetch_wikijob))
    return sources


def collect_all_report(
    settings: Mapping | None = None,
    *,
    sources: Iterable[Source] | None = None,
    deadline_seconds: float | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[list[ScrapedOpportunity], dict[str, object]]:
    """Run every registered source and report truthful per-source outcomes.

    Returns ``(items, report)`` where ``items`` is exactly what
    :func:`collect_all` would return (deduped + early-careers filtered) and
    ``report`` carries ``registered/attempted/succeeded/failed`` counters plus
    ``per_source`` raw row counts and ``errors`` keyed by source name.

    A failing source contributes nothing and never sinks the sweep; an
    all-failed sweep returns ``items == []`` with ``failed == attempted`` so
    callers must not mistake it for an empty-success board. A source whose
    rows fail materialization or validation is rejected in isolation with a
    per-source error — earlier good sources are never discarded by the later
    global dedupe. Duplicate source names are explicitly reported in
    ``duplicate_sources`` (counts merged under the shared name so counters
    stay reconcilable). Error text carries the exception type plus a safe
    category only — never a raw URL that could embed query secrets.

    ``deadline_seconds`` bounds the harvest wall-clock cooperatively: it is
    checked between sources only, so one active source making several HTTP
    calls can overrun it (each call still honors the shared ``HTTP_TIMEOUT``).
    There is deliberately no threadpool — inflight work is always exactly the
    one request currently executing, which is also why the bound cannot
    preempt a running source. ``is_cancelled`` likewise stops the sweep
    between sources. Truncated runs list the unattempted names in ``skipped``
    with ``truncated`` set to ``"time_budget"`` or ``"cancelled"`` — callers
    must treat those as explicitly partial, never empty-success.
    ``deadline_seconds=None`` is the explicit backward-compatible unlimited
    mode; any other value must be finite and non-negative, otherwise nothing
    is fetched and the report carries a ``collector_error``. A raising
    cancellation callback fails closed: fetching stops at once and the error
    is recorded. Counters distinguish ``registered`` (known sources) from
    genuinely ``attempted`` (``succeeded + failed``); ``registered`` always
    equals ``attempted + len(skipped)``.
    """
    import math
    import time

    try:
        specs = list(build_sources(settings) if sources is None else sources)
    except Exception as exc:  # noqa: BLE001 - construction failure is itself the report
        logger.warning("source registry failed: %s", type(exc).__name__)
        return [], {
            "registered": 0,
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "per_source": {},
            "errors": {},
            "total_raw": 0,
            "total": 0,
            "skipped": [],
            "truncated": None,
            "duplicate_sources": [],
            "collector_error": _safe_source_error(exc),
        }

    if deadline_seconds is not None:
        try:
            budget = float(deadline_seconds)
        except (TypeError, ValueError):
            budget = float("nan")
        if not math.isfinite(budget) or budget < 0:
            # Build skipped list with duplicate detection for invalid/negative budget
            seen_names: set[str] = set()
            duplicate_sources: list[str] = []
            skipped: list[str] = []
            for spec in specs:
                if isinstance(spec, tuple) and len(spec) == 2:
                    name = spec[0]
                else:
                    name = "<malformed>"
                if name in seen_names and name not in duplicate_sources:
                    duplicate_sources.append(name)
                seen_names.add(name)
                skipped.append(name)
            return [], {
                "registered": len(specs),
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
                "per_source": {},
                "errors": {"duplicate_sources": "duplicate source registrations: " + ", ".join(sorted(duplicate_sources))} if duplicate_sources else {},
                "total_raw": 0,
                "total": 0,
                "skipped": skipped,
                "truncated": "time_budget",
                "duplicate_sources": duplicate_sources,
                "collector_error": (
                    "invalid collection budget: "
                    f"{type(deadline_seconds).__name__}"
                ),
            }
        deadline = time.monotonic() + budget
    else:
        deadline = None
    seen_names: set[str] = set()
    duplicate_sources: list[str] = []
    partial_sources: list[str] = []
    per_source: dict[str, int] = {}
    errors: dict[str, str] = {}
    skipped: list[str] = []
    truncated: str | None = None
    succeeded = 0
    failed = 0
    malformed_count = 0
    collected: list[ScrapedOpportunity] = []
    for index, spec in enumerate(specs):
        # Validate spec structure before unpacking to catch malformed registrations
        if not isinstance(spec, tuple) or len(spec) != 2 or not callable(spec[1]):
            # Sanitize log: don't log the callable or full spec, just the index and type
            spec_type = type(spec).__name__
            logger.warning("source registration malformed at index %d: %s (expected 2-tuple with callable)", index, spec_type)
            # Malformed spec is not attempted (callable never invoked), so don't increment failed
            # Use a synthetic name for the malformed entry
            malformed_count += 1
            malformed_name = f"<malformed:{index}>"
            per_source[malformed_name] = 0
            errors[malformed_name] = "malformed source registration: expected (name, fetch) tuple with callable"
            continue
        name, fetch = spec
        if is_cancelled is not None:
            try:
                cancelled = bool(is_cancelled())
            except Exception as exc:  # noqa: BLE001 - fail closed on a broken stop guard
                logger.warning(
                    "cancellation callback failed (%s); stopping harvest",
                    type(exc).__name__,
                )
                errors["cancellation_callback"] = (
                    f"{type(exc).__name__}: cancellation check failed"
                )
                truncated = "cancelled"
                # Build skipped list with duplicate detection for remaining specs
                for i, s in enumerate(specs[index:], start=index):
                    if isinstance(s, tuple) and len(s) == 2:
                        skip_name = s[0]
                    else:
                        skip_name = f"<malformed:{i}>"
                    if skip_name in seen_names and skip_name not in duplicate_sources:
                        duplicate_sources.append(skip_name)
                    seen_names.add(skip_name)
                    skipped.append(skip_name)
                break
            if cancelled:
                truncated = "cancelled"
                # Build skipped list with duplicate detection for remaining specs
                for i, s in enumerate(specs[index:], start=index):
                    if isinstance(s, tuple) and len(s) == 2:
                        skip_name = s[0]
                    else:
                        skip_name = f"<malformed:{i}>"
                    if skip_name in seen_names and skip_name not in duplicate_sources:
                        duplicate_sources.append(skip_name)
                    seen_names.add(skip_name)
                    skipped.append(skip_name)
                break
        if deadline is not None and time.monotonic() >= deadline:
            truncated = "time_budget"
            # Build skipped list with duplicate detection for remaining specs
            for i, s in enumerate(specs[index:], start=index):
                if isinstance(s, tuple) and len(s) == 2:
                    skip_name = s[0]
                else:
                    skip_name = f"<malformed:{i}>"
                if skip_name in seen_names and skip_name not in duplicate_sources:
                    duplicate_sources.append(skip_name)
                seen_names.add(skip_name)
                skipped.append(skip_name)
            break
        if name in seen_names and name not in duplicate_sources:
            duplicate_sources.append(name)
        seen_names.add(name)
        try:
            raw_result = fetch()
            if isinstance(raw_result, PartialFetchResult):
                rows = list(raw_result)
                if raw_result.collector_error:
                    # All pages failed: count as a failed source
                    failed += 1
                    per_source[name] = per_source.get(name, 0)
                    errors[name] = raw_result.collector_error
                    continue
                else:
                    # Partial page failure: some succeeded, some failed
                    partial_sources.append(name)
                    partial_msg = (
                        f"partial: {raw_result.failed_pages} of {raw_result.total_pages} pages failed"
                    )
                    errors[name] = partial_msg
            else:
                rows = list(raw_result or [])
        except Exception as exc:  # noqa: BLE001 - isolation per source is the point
            logger.warning("source %s failed, skipping: %s", name, type(exc).__name__)
            failed += 1
            per_source[name] = per_source.get(name, 0)
            errors.setdefault(name, _safe_source_error(exc))
            continue
        reason = _malformed_row_reason(rows)
        if reason is not None:
            logger.warning("source %s rejected: %s", name, reason)
            failed += 1
            per_source[name] = per_source.get(name, 0)
            errors.setdefault(name, reason)
            continue
        per_source[name] = per_source.get(name, 0) + len(rows)
        succeeded += 1
        collected.extend(rows)
    if duplicate_sources:
        errors["duplicate_sources"] = (
            "duplicate source registrations: " + ", ".join(sorted(duplicate_sources))
        )
    try:
        merged = dedupe(collected)
        items = [opp for opp in merged if is_early_careers(opp.role_title)]
    except Exception as exc:  # noqa: BLE001 - global merge failure is explicit, not silent
        logger.warning("source merge failed: %s", type(exc).__name__)
        attempted = succeeded + failed
        return [], {
            "registered": attempted + len(skipped) + malformed_count,
            "attempted": attempted,
            "succeeded": succeeded,
            "failed": failed,
            "per_source": per_source,
            "errors": {**errors, "collector": _safe_source_error(exc)},
            "total_raw": len(collected),
            "total": 0,
            "skipped": skipped,
            "truncated": truncated,
            "duplicate_sources": duplicate_sources,
            "partial_sources": partial_sources,
            "collector_error": _safe_source_error(exc),
        }
    attempted = succeeded + failed
    report: dict[str, object] = {
        "registered": attempted + len(skipped) + malformed_count,
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "per_source": per_source,
        "errors": errors,
        "total_raw": len(collected),
        "total": len(items),
        "skipped": skipped,
        "truncated": truncated,
        "duplicate_sources": duplicate_sources,
        "partial_sources": partial_sources,
        "collector_error": None,
    }
    return items, report


def collect_all(
    settings: Mapping | None = None,
    *,
    sources: Iterable[Source] | None = None,
) -> list[ScrapedOpportunity]:
    """Run every registered source and merge by exact source identity.

    A failing source logs a warning and contributes nothing — one flaky board
    never sinks the sweep. Non-early-careers rows are dropped at the end so the
    pipeline only ever sees relevant programmes.
    """
    items, _ = collect_all_report(settings, sources=sources)
    return items
