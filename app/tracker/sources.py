"""Public source adapters for the standalone ARGUS tracker.

Only unauthenticated, read-only public feeds belong here.  Each adapter turns
one verified endpoint into a ``SourceResult`` and contains its own failure
boundary; a broken board cannot make another board disappear or make a valid
empty board look healthy.  This module deliberately does not import the legacy
application entry point, collection runner, browser code, or database.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from app.tracker.contracts import Listing, SourceResult, utc_now
from app.tracker.source_parsers import (
    ParseReport,
    parse_greenhouse_payload,
    parse_greenhouse_payload_report,
    parse_pinpoint_payload,
    parse_pinpoint_payload_report,
    parse_recruitee_payload,
    parse_recruitee_payload_report,
    parse_simplytk_html,
    parse_simplytk_html_report,
)

# Public endpoint observations for these hosts are recorded in
# .hermes/argus-v2/tracker-sources-evidence.md.  The constants are kept
# explicit so a future change cannot silently expand collection to arbitrary
# user-provided URLs.
GREENHOUSE_ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/{org}/jobs?content=true"
RECRUITEE_SILVERPEAK_ENDPOINT = "https://silverpeakllp.recruitee.com/api/offers"
PINPOINT_MENZIES_ENDPOINT = "https://menzies.pinpointhq.com/en/postings.json"
SIMPLYTK_ENDPOINT = (
    "https://simplytk.com/internship-tracker"
    "?programme=summer_internship%2Cindustrial_placement%2Cspring_week"
)
SIMPLYTK_MAX_PAGES = 20
SIMPLYTK_BUDGET_SECONDS = 60

# This is the current Greenhouse watchlist from the existing pure source.
# Additional boards below are direct public Greenhouse boards verified by live
# API probes on 2026-09-11; they are kept separate so coverage provenance is
# visible to callers.
GREENHOUSE_ORGS: tuple[str, ...] = (
    "point72",
    "jumptrading",
    "akunacapital",
    "optiver",
    "imc",
    "flowtraders",
    "janestreet",
    "liontree",
    "williamblair",
    "mavensecuritiesholdingltd",
    "exoduspoint",
    "schonfeld",
    "marshallwace",
)
GREENHOUSE_ADDITIONAL_ORGS: tuple[str, ...] = (
    "dvtrading",
    "celonis",
    "cambridgeconsultantslimited",
)
GREENHOUSE_SOURCE_ORGS: tuple[str, ...] = GREENHOUSE_ORGS + GREENHOUSE_ADDITIONAL_ORGS
# Compatibility-friendly name for callers that used the legacy source's
# ``ORGS`` spelling.  It includes all live-verified additions.
ORGS: tuple[str, ...] = GREENHOUSE_SOURCE_ORGS

HTTP_TIMEOUT_SECONDS = 15.0
# Short aliases make the operational boundary obvious and give tests a stable
# seam without changing the public data contract.
HTTP_TIMEOUT = HTTP_TIMEOUT_SECONDS
MAX_CONCURRENCY = 4
VALID_STATUSES = frozenset({"ok", "empty", "partial", "error"})
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 ARGUS-tracker/1.0"
)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """One fixed public source and its no-argument adapter."""

    name: str
    url: str
    fetch: Callable[[], SourceResult]


def _http_get(url: str, *, accept: str) -> Any:
    """Make one bounded, read-only public GET request.

    ``httpx`` is imported lazily so parser-only tests do not need a live
    network.  Redirects are followed by the HTTP client, but no credentials,
    cookies, browser session, or block bypass is attempted.
    """

    import httpx

    response = httpx.get(
        url,
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-GB,en;q=0.9",
        },
        follow_redirects=True,
    )
    response.raise_for_status()
    return response


def http_get_text(url: str) -> str:
    """Fetch public response text with the tracker timeout."""

    return _http_get(url, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8").text


def http_get_json(url: str) -> Any:
    """Fetch and decode one public JSON response."""

    response = _http_get(url, accept="application/json,text/plain;q=0.9,*/*;q=0.8")
    # ``json.loads`` provides a useful, deterministic decode error for the
    # adapter boundary rather than treating an HTML error page as an empty feed.
    return json.loads(response.text)


def _exception_message(exc: BaseException, url: str) -> str:
    """Describe transport/HTTP failures without losing their source semantics."""

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        reason = getattr(response, "reason_phrase", "") or ""
        suffix = f" {reason}" if reason else ""
        return f"HTTP {status_code}{suffix} from {url}"
    detail = str(exc).strip()
    if detail:
        return f"{type(exc).__name__} from {url}: {detail}"
    return f"{type(exc).__name__} from {url}"


def _result_from_report(
    *,
    name: str,
    url: str,
    report: ParseReport,
    started: float,
) -> SourceResult:
    errors = list(report.errors)
    if report.malformed_records:
        errors.append(f"{report.malformed_records} malformed record(s)")

    if report.listings:
        status = "partial" if errors else "ok"
    elif errors:
        # A 200 structured response with no jobs is handled below as empty;
        # malformed/schema-less/HTML-without-rows responses are errors.
        status = "error"
    else:
        status = "empty"

    return SourceResult(
        name=name,
        url=url,
        status=status,
        listings=list(report.listings),
        error="; ".join(errors),
        checked_at=utc_now(),
        elapsed_seconds=max(0.0, time.monotonic() - started),
    )


def _error_result(*, name: str, url: str, started: float, error: str) -> SourceResult:
    return SourceResult(
        name=name,
        url=url,
        status="error",
        listings=[],
        error=error,
        checked_at=utc_now(),
        elapsed_seconds=max(0.0, time.monotonic() - started),
    )


def _run_report(
    *,
    name: str,
    url: str,
    parse: Callable[[], ParseReport],
) -> SourceResult:
    started = time.monotonic()
    try:
        report = parse()
    except Exception as exc:  # noqa: BLE001 - source isolation is intentional
        return _error_result(
            name=name,
            url=url,
            started=started,
            error=_exception_message(exc, url),
        )
    return _result_from_report(name=name, url=url, report=report, started=started)


def greenhouse_source(org: str) -> SourceResult:
    """Collect one verified Greenhouse board, keeping only early careers roles."""

    url = GREENHOUSE_ENDPOINT.format(org=org)
    return _run_report(
        name=f"greenhouse:{org}",
        url=url,
        parse=lambda: parse_greenhouse_payload_report(http_get_json(url), org),
    )


def recruitee_silverpeak_source() -> SourceResult:
    """Collect Silverpeak's public Recruitee offers feed."""

    url = RECRUITEE_SILVERPEAK_ENDPOINT
    return _run_report(
        name="recruitee:silverpeak",
        url=url,
        parse=lambda: parse_recruitee_payload_report(
            http_get_json(url), "Silverpeak"
        ),
    )


def pinpoint_menzies_source() -> SourceResult:
    """Collect Menzies' public Pinpoint postings JSON feed."""

    url = PINPOINT_MENZIES_ENDPOINT
    return _run_report(
        name="pinpoint:menzies",
        url=url,
        parse=lambda: parse_pinpoint_payload_report(http_get_json(url), "Menzies"),
    )


def simplytk_source() -> SourceResult:
    """Follow bounded public pagination and prove the advertised capture count.

    A changed count, repeated page, missing row or later HTTP error retains
    observed roles but reports partial. No authenticated endpoint is used.
    """
    url = SIMPLYTK_ENDPOINT
    started = time.monotonic()
    try:
        first = parse_simplytk_html_report(http_get_text(url), url)
    except Exception as exc:
        return _error_result(name='simplytk', url=url, started=started,
                             error=_exception_message(exc, url))
    if not first.page_count or first.page_count <= 1 or not first.listings:
        return _result_from_report(name='simplytk', url=url, report=first, started=started)
    reports = [first]
    errors = []
    if first.page_number != 1:
        errors.append('first response was not page 1')
    else:
        for number in range(2, min(first.page_count, SIMPLYTK_MAX_PAGES) + 1):
            if time.monotonic() - started >= SIMPLYTK_BUDGET_SECONDS:
                errors.append('pagination time budget reached')
                break
            page_url = f'{url}&page={number}'
            try:
                report = parse_simplytk_html_report(http_get_text(page_url), page_url)
            except Exception as exc:
                errors.append(_exception_message(exc, page_url))
                break
            if (report.page_number != number or report.page_count != first.page_count
                    or report.advertised_count != first.advertised_count):
                errors.append(f'page {number} pagination/count changed or repeated')
                break
            reports.append(report)
    unique = {}
    for report in reports:
        for listing in report.listings:
            key = (listing.employer.casefold(), listing.title.casefold(), listing.url, listing.location.casefold())
            unique[key] = listing
    # The fixed query requests only the three supported programme groups.
    # Therefore an unparsed/filtered row prevents completeness, not a silent
    # success based solely on adding up row counts from overlapping pages.
    if (len(reports) != first.page_count or len(unique) != first.advertised_count
            or any(report.malformed_records for report in reports)):
        errors.append(f'captured {len(unique)} of advertised {first.advertised_count} openings '
                      f'across {len(reports)}/{first.page_count} pages')
    return _result_from_report(name='simplytk', url=url, started=started,
                               report=ParseReport(listings=tuple(unique.values()), errors=tuple(errors)))


# Alternate descriptive aliases make the individual adapters discoverable
# without creating additional source registrations.
fetch_greenhouse = greenhouse_source
fetch_recruitee_silverpeak = recruitee_silverpeak_source
fetch_pinpoint_menzies = pinpoint_menzies_source
fetch_simplytk = simplytk_source


def _build_source_specs() -> tuple[SourceSpec, ...]:
    greenhouse_specs = tuple(
        SourceSpec(
            name=f"greenhouse:{org}",
            url=GREENHOUSE_ENDPOINT.format(org=org),
            fetch=lambda org=org: greenhouse_source(org),
        )
        for org in GREENHOUSE_SOURCE_ORGS
    )
    return greenhouse_specs + (
        SourceSpec(
            name="recruitee:silverpeak",
            url=RECRUITEE_SILVERPEAK_ENDPOINT,
            fetch=recruitee_silverpeak_source,
        ),
        SourceSpec(
            name="pinpoint:menzies",
            url=PINPOINT_MENZIES_ENDPOINT,
            fetch=pinpoint_menzies_source,
        ),
        SourceSpec(
            name="simplytk",
            url=SIMPLYTK_ENDPOINT,
            fetch=simplytk_source,
        ),
    )


SOURCE_SPECS: tuple[SourceSpec, ...] = _build_source_specs()


def _run_spec(spec: SourceSpec) -> SourceResult:
    started = time.monotonic()
    try:
        result = spec.fetch()
    except Exception as exc:  # noqa: BLE001 - one source must not sink a sweep
        return _error_result(
            name=spec.name,
            url=spec.url,
            started=started,
            error=_exception_message(exc, spec.url),
        )

    if not isinstance(result, SourceResult):
        return _error_result(
            name=spec.name,
            url=spec.url,
            started=started,
            error=(
                "adapter returned "
                f"{type(result).__name__}, expected SourceResult"
            ),
        )
    if result.status not in VALID_STATUSES:
        return _error_result(
            name=spec.name,
            url=spec.url,
            started=started,
            error=f"adapter returned invalid status {result.status!r}",
        )
    return result


def collect_sources() -> list[SourceResult]:
    """Run all fixed public sources with bounded concurrency.

    Results retain registration order for deterministic refresh evidence while
    the calls themselves run in at most four worker threads.  HTTP calls carry
    a 15-second timeout, and every adapter failure is represented as an error
    result rather than raised or converted to an empty successful board.
    """

    specs = tuple(SOURCE_SPECS)
    if not specs:
        return []

    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENCY, len(specs))) as pool:
        futures = [pool.submit(_run_spec, spec) for spec in specs]
        # Retrieving in spec order gives callers stable source health ordering;
        # it does not serialize the actual network work above.
        return [future.result() for future in futures]


def collect_all_sources() -> list[SourceResult]:
    """Compose the bounded general and finance-specific primary collectors."""
    from app.tracker.additional_sources import collect_additional_sources
    regular = collect_sources()
    direct = collect_additional_sources()
    results = regular + direct
    if len({item.name for item in results}) != len(results):
        raise ValueError('Duplicate public source registrations')
    return results


__all__ = [
    "collect_all_sources",
    "GREENHOUSE_ENDPOINT",
    "GREENHOUSE_ORGS",
    "GREENHOUSE_ADDITIONAL_ORGS",
    "GREENHOUSE_SOURCE_ORGS",
    "ORGS",
    "RECRUITEE_SILVERPEAK_ENDPOINT",
    "PINPOINT_MENZIES_ENDPOINT",
    "SIMPLYTK_ENDPOINT",
    "HTTP_TIMEOUT_SECONDS",
    "HTTP_TIMEOUT",
    "MAX_CONCURRENCY",
    "SourceSpec",
    "SOURCE_SPECS",
    "collect_sources",
    "greenhouse_source",
    "recruitee_silverpeak_source",
    "pinpoint_menzies_source",
    "simplytk_source",
    "http_get_json",
    "http_get_text",
    "parse_greenhouse_payload",
    "parse_recruitee_payload",
    "parse_pinpoint_payload",
    "parse_simplytk_html",
]
