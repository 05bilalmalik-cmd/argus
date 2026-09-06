"""Public UK careers-aggregator parsers: Bright Network, RateMyPlacement, WikiJob.

Parsing strategy per site (robust to markup churn):
  1. PRIMARY   — JSON-LD ``<script type="application/ld+json">`` blocks whose
                 items carry ``@type == "JobPosting"``.
  2. FALLBACK  — anchor heuristics gated by the canonical ATS classifier.

Parsers take HTML text (plus an optional base URL for resolving relative
links) and return rows; the ``fetch_*`` functions own the HTTP side. Update
the module-level URL constants below when a site reshuffles its listing pages.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.domain.targets import source_identity_key
from app.scouting.programmes import classify_programme
from app.scouting.sources.base import (
    DROP_TITLE,
    _ats_from_url,
    _clean,
    _parse_deadline,
    http_get,
)
from app.scouting.trackr import ScrapedOpportunity

logger = logging.getLogger(__name__)

# --- Target pages (module-level so they are trivial to update) --------------
# NOTE: verify these paths occasionally; aggregators reorganise often.
BRIGHTNETWORK_URLS: tuple[str, ...] = (
    "https://www.brightnetwork.co.uk/graduate-jobs/spring-insight-programmes/",
    "https://www.brightnetwork.co.uk/graduate-jobs/internships/",
    "https://www.brightnetwork.co.uk/industrial-placements/",
)
RATEMYPLACEMENT_URLS: tuple[str, ...] = (
    "https://www.ratemyplacement.co.uk/placements",
    "https://www.ratemyplacement.co.uk/year-in-industry",
)
WIKIJOB_URLS: tuple[str, ...] = (
    "https://www.wikijob.co.uk/jobs/internships",
    "https://www.wikijob.co.uk/jobs/placement-year",
)

SOURCE_LABELS = {
    "brightnetwork": "brightnetwork",
    "ratemyplacement": "ratemyplacement",
    "wikijob": "wikijob",
}

# Small local brand list for the anchor fallback's employer guess (subset of
# trackr._KNOWN_FIRMS — copied, not imported, to keep this package decoupled).
_KNOWN_FIRMS = (
    "Goldman Sachs", "J.P. Morgan", "Morgan Stanley", "Bank of America",
    "Barclays", "HSBC", "Deutsche Bank", "UBS", "Evercore", "Moelis",
    "Jefferies", "Nomura", "BNP Paribas", "BlackRock", "Citadel",
    "Two Sigma", "Jane Street", "Blackstone", "Optiver", "IMC",
)


def _guess_employer(blob: str) -> str:
    best = ""
    folded = blob.casefold()
    for firm in _KNOWN_FIRMS:
        if firm.casefold() in folded and len(firm) > len(best):
            best = firm
    return best


def _iter_ldjson_items(soup: BeautifulSoup) -> list[dict]:
    """Collect dict items from every JSON-LD script, tolerating bad payloads."""
    items: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        candidates: Any
        if isinstance(payload, dict):
            candidates = payload.get("@graph", [payload])
        elif isinstance(payload, list):
            candidates = payload
        else:
            continue
        if not isinstance(candidates, list):
            continue
        items.extend(item for item in candidates if isinstance(item, dict))
    return items


def _parse_ldjson(soup: BeautifulSoup, base_url: str) -> list[ScrapedOpportunity]:
    out: list[ScrapedOpportunity] = []
    for item in _iter_ldjson_items(soup):
        job_type = item.get("@type")
        types = set(job_type) if isinstance(job_type, list) else {job_type}
        if "JobPosting" not in types:
            continue
        title = _clean(item.get("title") or item.get("name"))
        org = item.get("hiringOrganization") or {}
        employer = _clean(org.get("name") if isinstance(org, dict) else str(org or ""))
        url = _clean(item.get("url") or item.get("sameAs") or "")
        if not title or not url.startswith("http"):
            continue
        location = item.get("jobLocation")
        if isinstance(location, list):
            location = location[0] if location else None
        loc_name = ""
        if isinstance(location, dict):
            address = location.get("address")
            if isinstance(address, dict):
                loc_name = address.get("addressLocality") or address.get("addressRegion") or ""
            else:
                loc_name = str(address or "")
        elif isinstance(location, str):
            loc_name = location
        out.append(
            ScrapedOpportunity(
                employer=employer or "Unknown",
                role_title=title,
                source_url=url,
                application_url=None,
                location=_clean(loc_name),
                deadline=_parse_deadline(item.get("validThrough")),
                ats_type=_ats_from_url(url),
                programme_type=classify_programme(title),
            )
        )
    return out


def _unwrap_redirect(href: str) -> str:
    """Some aggregators route outbound links through a redirector
    (``/out?u=<urlencoded ats url>``); recover the real ATS target."""
    parts = urlsplit(href)
    if not parts.query:
        return href
    from urllib.parse import parse_qs

    params = parse_qs(parts.query)
    for key in ("url", "u", "redirect", "target", "destination"):
        candidate = (params.get(key) or [""])[0]
        if _ats_from_url(candidate) != "unknown":
            return candidate
    return href


def _parse_anchors(soup: BeautifulSoup, base_url: str) -> list[ScrapedOpportunity]:
    """Fallback: <a> tags pointing at known ATS hosts with listing-ish text."""
    out: list[ScrapedOpportunity] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for anchor in soup.find_all("a", href=True):
        href = _clean(anchor.get("href"))
        url = urljoin(base_url, href) if base_url else href
        ats_type = _ats_from_url(url)
        if ats_type == "unknown":
            url = _unwrap_redirect(url)
            ats_type = _ats_from_url(url)
            if ats_type == "unknown":
                continue
        # Climb to the smallest container holding just this one ATS link so
        # sibling cards never bleed into this row's text blob.
        container = anchor
        while container.parent is not None:
            parent = container.parent
            if len(parent.find_all("a", href=True)) > 1:
                break
            container = parent
        text_blob = _clean(container.get_text(" "))
        role_text = _clean(anchor.get_text(" ")) or text_blob[:120]
        if not role_text or DROP_TITLE.search(role_text):
            continue
        employer = _guess_employer(text_blob) or "Unknown"
        key = source_identity_key(
            employer=employer,
            role_title=role_text,
            cycle="",
            source_url=url,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(
            ScrapedOpportunity(
                employer=employer,
                role_title=role_text,
                source_url=url,
                application_url=None,
                ats_type=ats_type,
                programme_type=classify_programme(role_text),
            )
        )
    return out


def parse_listing_html(
    html: str | None,
    *,
    source_label: str,
    base_url: str = "",
) -> list[ScrapedOpportunity]:
    """Parse one aggregator listing page; JSON-LD first, anchors as fallback."""
    source_label = SOURCE_LABELS.get(source_label, source_label)
    if not html or not html.strip():
        return []
    soup = BeautifulSoup(html, "html.parser")
    rows = _parse_ldjson(soup, base_url)
    if not rows:
        rows = _parse_anchors(soup, base_url)
    for row in rows:
        row.source = source_label
    return rows


def parse_brightnetwork_html(html: str | None, base_url: str = BRIGHTNETWORK_URLS[0]) -> list[ScrapedOpportunity]:
    return parse_listing_html(html, source_label="brightnetwork", base_url=base_url)


def parse_ratemyplacement_html(html: str | None, base_url: str = RATEMYPLACEMENT_URLS[0]) -> list[ScrapedOpportunity]:
    return parse_listing_html(html, source_label="ratemyplacement", base_url=base_url)


def parse_wikijob_html(html: str | None, base_url: str = WIKIJOB_URLS[0]) -> list[ScrapedOpportunity]:
    return parse_listing_html(html, source_label="wikijob", base_url=base_url)


def _fetch_pages(urls: tuple[str, ...], parser) -> list[ScrapedOpportunity]:
    from app.scouting.sources.base import dedupe

    collected: list[ScrapedOpportunity] = []
    for url in urls:
        try:
            html = http_get(url)
        except Exception as exc:  # noqa: BLE001 - one dead page must not kill the site
            logger.warning("aggregator page %s failed: %s", url, exc)
            continue
        collected.extend(parser(html, base_url=url))
    return dedupe(collected)


def fetch_brightnetwork() -> list[ScrapedOpportunity]:
    return _fetch_pages(BRIGHTNETWORK_URLS, parse_brightnetwork_html)


def fetch_ratemyplacement() -> list[ScrapedOpportunity]:
    return _fetch_pages(RATEMYPLACEMENT_URLS, parse_ratemyplacement_html)


def fetch_wikijob() -> list[ScrapedOpportunity]:
    return _fetch_pages(WIKIJOB_URLS, parse_wikijob_html)
