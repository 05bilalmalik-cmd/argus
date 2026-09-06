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
    if sources is None:
        sources = build_sources(settings)
    collected: list[ScrapedOpportunity] = []
    for name, fetch in sources:
        try:
            rows = fetch() or []
        except Exception as exc:  # noqa: BLE001 - isolation per source is the point
            logger.warning("source %s failed, skipping: %s", name, exc)
            continue
        collected.extend(rows)
    merged = dedupe(collected)
    return [opp for opp in merged if is_early_careers(opp.role_title)]
