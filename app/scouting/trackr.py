"""Trackr HTML parser + opportunity ingestion for ARGUS.

The user saves Trackr listing/search pages as HTML (Ctrl+S in the browser) into
a watched folder, or drops a single file. This module parses listings out of
that saved HTML, normalises them into Opportunity records and inserts them with
exact source-identity duplicate protection. Multiple roles may intentionally
share one listing URL; the normalized employer, role, cycle, URL, location, and
division tuple is authoritative rather than a hash or URL alone.

Parsing strategy is defensive: Trackr's markup shifts, so we try several
strategies in order and keep whatever yields structured results:
  1. JSON embedded in <script type="application/ld+json"> (JobPosting schema)
  2. Anchor-based heuristics: links to external application URLs whose link
     text / surrounding card looks like an internship listing.

Also parses the CSV export template ARGUS already supports.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from bs4 import BeautifulSoup

from app.domain.targets import source_identity_key
from app.scouting.programmes import ProgrammeType, classify_programme
from app.scouting.sources.base import _ats_from_url

logger = logging.getLogger(__name__)

CYCLE = "2026-27"  # recruitment cycle label used across imports

# Words that disqualify a listing from auto-processing entirely.
EXCLUDE = re.compile(
    r"graduate\s+scheme|graduate\s+programme|graduate\s+program|"
    r"full\s*-?\s*time\s+(?:role|position|job)|part\s*-?\s*time|"
    r"apprenticeship(?!.*finance)|\bphd\b|postdoc",
    re.I,
)


@dataclass(slots=True, init=False)
class ScrapedOpportunity:
    employer: str
    role_title: str
    source_url: str
    application_url: str | None
    location: str = ""
    deadline: date | None = None
    ats_type: str = "unknown"
    programme_type: ProgrammeType = ProgrammeType.OTHER
    division: str = ""
    source: str = "trackr_html"

    def __init__(
        self,
        employer: str,
        role_title: str,
        url: str = "",
        location: str = "",
        deadline: date | None = None,
        ats_type: str = "unknown",
        programme_type: ProgrammeType = ProgrammeType.OTHER,
        division: str = "",
        source: str = "trackr_html",
        *,
        source_url: str = "",
        application_url: str | None = None,
    ) -> None:
        if url and source_url and url != source_url:
            raise ValueError("url and source_url must identify the same source page")
        self.employer = employer
        self.role_title = role_title
        self.source_url = source_url or url
        self.application_url = application_url or None
        self.location = location
        self.deadline = deadline
        self.ats_type = ats_type
        self.programme_type = programme_type
        self.division = division
        self.source = source

    @property
    def url(self) -> str:
        """Compatibility alias: legacy ``url`` always means source URL."""

        return self.source_url

    @url.setter
    def url(self, value: str) -> None:
        self.source_url = value


@dataclass(slots=True)
class ParseReport:
    found: int
    inserted: int
    duplicates: int
    excluded: int
    errors: tuple[str, ...] = field(default_factory=tuple)


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_deadline(raw: str) -> date | None:
    raw = _clean(raw)
    if not raw:
        return None
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(re.sub(r"(\d)(st|nd|rd|th)", r"\1", raw), fmt)
            return parsed.date()
        except ValueError:
            continue
    return None


def parse_ldjson_jobpostings(soup: BeautifulSoup) -> list[ScrapedOpportunity]:
    out: list[ScrapedOpportunity] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = payload.get("@graph", [payload]) if isinstance(payload, dict) else payload
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("@type") not in {"JobPosting"}:
                continue
            title = _clean(item.get("title") or item.get("name"))
            org = item.get("hiringOrganization") or {}
            employer = _clean(org.get("name") if isinstance(org, dict) else str(org))
            url = _clean(item.get("url") or item.get("sameAs") or "")
            if not (title and employer and url.startswith("http")):
                continue
            out.append(
                ScrapedOpportunity(
                    employer=employer,
                    role_title=title,
                    url=url,
                    location=_clean(
                        (item.get("jobLocation") or {}).get("address", {}).get("addressLocality", "")
                        if isinstance(item.get("jobLocation"), dict)
                        else ""
                    ),
                    deadline=_parse_deadline(str(item.get("validThrough") or "")),
                    ats_type=_ats_from_url(url),
                    programme_type=classify_programme(title),
                )
            )
    return out


def parse_anchor_listings(soup: BeautifulSoup) -> list[ScrapedOpportunity]:
    """Heuristic: find <a> tags whose href points at a known ATS and whose text
    looks like a finance early-careers programme."""
    out: list[ScrapedOpportunity] = []
    seen_identities: set[tuple[str, str, str, str, str, str]] = set()
    for anchor in soup.find_all("a", href=True):
        url = _clean(anchor["href"])
        ats_type = _ats_from_url(url)
        if ats_type == "unknown":
            continue
        # card context: climb only while the container holds just this one
        # ATS link, so sibling cards never bleed into this listing's text
        container = anchor
        while container.parent is not None:
            parent = container.parent
            if len(parent.find_all("a", href=True)) > 1:
                break
            container = parent
        text_blob = _clean(container.get_text(" "))
        role_text = _clean(anchor.get_text(" ")) or text_blob[:120]
        if not role_text or EXCLUDE.search(role_text):
            continue
        # Employer heuristic: strongest brand name present in the blob
        employer = _guess_employer(text_blob)
        identity = source_identity_key(
            employer=employer or "Unknown",
            role_title=role_text,
            cycle=CYCLE,
            source_url=url,
        )
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        out.append(
            ScrapedOpportunity(
                employer=employer or "Unknown",
                role_title=role_text,
                url=url,
                ats_type=ats_type,
                programme_type=classify_programme(role_text),
            )
        )
    return out


_KNOWN_FIRMS = [
    "Goldman Sachs", "J.P. Morgan", "JP Morgan", "Morgan Stanley", "Bank of America",
    "Barclays", "HSBC", "Deutsche Bank", "UBS", "Lazard", "Rothschild & Co",
    "Evercore", "Moelis", "Jefferies", "Nomura", "Societe Generale", "Société Générale",
    "BNP Paribas", "BlackRock", "Schroders", "M&G", "Fidelity", "abrdn", "Man Group",
    "Marshall Wace", "Brevan Howard", "Millennium", "Citadel", "Two Sigma",
    "BlueCrest", "Winton", "Capital Group", "Baillie Gifford", "Blackstone", "KKR",
    "Apollo", "Carlyle", "CVC Capital Partners", "Permira", "Bridgepoint", "Cinven",
    "PwC", "Deloitte", "EY", "KPMG", "London Stock Exchange Group", "LSEG",
    "Aviva", "Legal & General", "Prudential", "Aon", "Willis Towers Watson", "WTW",
    "American Express", "Bloomberg", "Wellington Management", "T. Rowe Price",
    "PIMCO", "Fidelity International", "Janus Henderson", "Amundi", "Ares",
]


def _guess_employer(blob: str) -> str:
    best = ""
    for firm in _KNOWN_FIRMS:
        if firm.casefold() in blob.casefold() and len(firm) > len(best):
            best = firm
    return best


def parse_trackr_html(html: str) -> list[ScrapedOpportunity]:
    soup = BeautifulSoup(html, "html.parser")
    opportunities = parse_ldjson_jobpostings(soup)
    if not opportunities:
        opportunities = parse_anchor_listings(soup)
    # De-dupe exact listings, never every role that shares a programme page.
    unique: dict[tuple[str, str, str, str, str, str], ScrapedOpportunity] = {}
    for opp in opportunities:
        key = source_identity_key(
            employer=opp.employer,
            role_title=opp.role_title,
            cycle=CYCLE,
            source_url=opp.source_url,
            location=opp.location,
            division=opp.division,
        )
        if key not in unique:
            unique[key] = opp
    return list(unique.values())


def scrape_trackr_file(html: str, *, source_label: str = "trackr.html") -> list[ScrapedOpportunity]:
    parsed = parse_trackr_html(html)
    for opp in parsed:
        opp.source = f"trackr_html:{source_label}"[:120]
    return parsed


def scrape_folder(folder: Path) -> list[ScrapedOpportunity]:
    all_opps: dict[tuple[str, str, str, str, str, str], ScrapedOpportunity] = {}
    for path in sorted(folder.rglob("*.html")) + sorted(folder.rglob("*.htm")):
        try:
            html = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("unreadable trackr save %s: %s", path.name, exc)
            continue
        for opp in parse_trackr_html(html):
            opp.source = f"trackr_html:{path.name}"[:120]
            identity = source_identity_key(
                employer=opp.employer,
                role_title=opp.role_title,
                cycle=CYCLE,
                source_url=opp.source_url,
                location=opp.location,
                division=opp.division,
            )
            all_opps.setdefault(identity, opp)
    return list(all_opps.values())
