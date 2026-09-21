"""Pure parsing and classification helpers for the tracker source adapters.

The tracker validates each public response shape before extracting records.  It
keeps the provider-specific parsers here so a changed board cannot be reported
as a successful empty feed, and it returns tracker ``Listing`` objects without
network, database, browser, or candidate-data work.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.scouting.programmes import classify_programme
from app.tracker.contracts import Listing

UTC = timezone.utc

# These are deliberately narrower than a general job-search matcher.  A source
# may advertise a job as full-time while the role itself is a bona fide summer
# internship or placement; employment commitment is therefore never a reason
# to reject an otherwise explicit early-careers title.
_EARLY_MARKER = re.compile(
    r"spring\s+(?:week|insight|programme|program)|"
    r"insight\s+(?:week|programme|program)|"
    r"industrial\s+placement|"
    r"year\s*-?\s*in\s*-?\s*industry|"
    r"placement\s+(?:student|year)|"
    r"sandwich\s+(?:year|placement)|"
    r"off[-\s]?cycle|"
    r"summer(?:\s+\d{4}|\s+(?:internship|placement|analyst|associate|intern))|"
    r"\binternship\b|\bintern\b|\bplacement\b",
    re.IGNORECASE,
)
_GRADUATE = re.compile(r"\bgraduate\b", re.IGNORECASE)
_GRADUATE_PROGRAMME = re.compile(
    r"\bgraduate\s+(?:scheme|programme|program|role)\b", re.IGNORECASE
)
_EXPERIENCED = re.compile(
    r"\bexperienced\s+hire\b|\bsenior\b|\bdirector\b|\bvice\s+president\b|"
    r"\bvp\b|\bhead\s+of\b|\bprincipal\b",
    re.IGNORECASE,
)

_PROGRAMME_ALIASES = {
    "industrial placement": "year_in_industry",
    "industrial_placement": "year_in_industry",
    "year in industry": "year_in_industry",
    "year_in_industry": "year_in_industry",
    "placement year": "year_in_industry",
    "placement_year": "year_in_industry",
    "sandwich placement": "year_in_industry",
    "sandwich year": "year_in_industry",
    "spring week": "spring_week",
    "spring_week": "spring_week",
    "spring insight": "spring_week",
    "spring_insight": "spring_week",
    "insight week": "spring_week",
    "summer internship": "summer",
    "summer_internship": "summer",
    "summer placement": "summer",
    "summer_placement": "summer",
    "summer": "summer",
    "off-cycle internship": "summer",
    "off_cycle_internship": "summer",
    "off-cycle": "summer",
    "off_cycle": "summer",
    "other": "other",
}


@dataclass(frozen=True, slots=True)
class ParseReport:
    """Parser output plus enough evidence for an honest source status."""

    listings: tuple[Listing, ...] = field(default_factory=tuple)
    total_records: int | None = None
    malformed_records: int = 0
    filtered_records: int = 0
    duplicate_records: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)
    advertised_count: int | None = None
    page_number: int | None = None
    page_count: int | None = None
    captured_records: int | None = None


def clean(value: Any) -> str:
    """Collapse markup whitespace without inventing values."""

    if value is None:
        return ""
    return re.sub(r"\s+", " ", unescape(str(value))).strip()


def valid_url(value: Any) -> bool:
    """Accept only absolute public HTTP(S) URLs."""

    raw = clean(value)
    if not raw:
        return False
    try:
        parts = urlsplit(raw)
    except ValueError:
        return False
    return parts.scheme.casefold() in {"http", "https"} and bool(parts.hostname)


def normalize_url(value: Any) -> str:
    """Canonicalize a URL for source-local duplicate detection."""

    raw = clean(value)
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if not parts.scheme or not parts.netloc:
        return raw.rstrip("/")
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path.rstrip("/"),
            parts.query,
            "",
        )
    )


def parse_deadline(value: Any) -> str | None:
    """Parse an explicit calendar date, never a relative phrase or yearless date."""

    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = clean(value)
    if not raw:
        return None

    # An ISO timestamp is an explicit date even when a source appends a time.
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ]|$)", raw)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(1)).isoformat()
        except ValueError:
            return None

    normalized = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw, flags=re.IGNORECASE)
    formats = (
        "%d %B %Y",
        "%d %b %Y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%B %d, %Y",
        "%b %d, %Y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(normalized, fmt).date().isoformat()
        except ValueError:
            continue
    # In particular, "15 Oct", "tomorrow", and "3 days to apply" stay None.
    return None


def parse_timestamp(value: Any) -> str | None:
    """Parse an explicit creation/publication timestamp as UTC ISO text.

    Callers pass only fields whose names carry creation/publication semantics;
    this function intentionally has no fallback to ``updated_at`` or relative
    prose.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            seconds = float(value) / (1000 if abs(float(value)) >= 10**11 else 1)
            parsed = datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        raw = clean(value)
        if not raw or re.search(r"\b(?:ago|yesterday|today|tomorrow)\b", raw, re.I):
            return None
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = None
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            for fmt in (
                "%Y-%m-%d %H:%M:%S UTC",
                "%Y-%m-%d %H:%M:%S %Z",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
            ):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def normalize_programme(programme: Any = "", title: Any = "") -> str:
    """Map source labels to the shared tracker programme values."""

    programme_text = clean(programme).casefold().replace("–", "-")
    title_text = clean(title)
    direct = _PROGRAMME_ALIASES.get(programme_text)
    if direct:
        return direct

    # Exact source labels sometimes carry a suffix, e.g. "Summer internship
    # 2027".  Match the label before consulting the shared title classifier.
    if "industrial placement" in programme_text or "year in industry" in programme_text:
        return "year_in_industry"
    if "spring week" in programme_text or "spring insight" in programme_text:
        return "spring_week"
    if "summer internship" in programme_text or "summer placement" in programme_text:
        return "summer"

    classified = classify_programme(title_text, clean(programme))
    return getattr(classified, "value", str(classified))


def is_early_careers(title: Any = "", programme: Any = "") -> bool:
    """Keep internships/placements/insight programmes, not graduate hires."""

    title_text = clean(title)
    programme_text = clean(programme)
    blob = f"{title_text} {programme_text}".strip()
    if not blob or _EXPERIENCED.search(blob):
        return False

    programme_value = normalize_programme(programme_text, title_text)
    has_marker = bool(_EARLY_MARKER.search(blob)) or programme_value in {
        "year_in_industry",
        "spring_week",
        "summer",
    }
    if not has_marker:
        return False

    # A graduate scheme/programme is never an internship.  A title such as
    # "Graduate Research Intern" is retained because the explicit intern marker
    # is stronger evidence of an internship than the broad word graduate.
    if _GRADUATE_PROGRAMME.search(title_text):
        return False
    if _GRADUATE.search(title_text) and not re.search(
        r"\b(?:intern|internship|placement|spring|insight)\b", title_text, re.I
    ):
        return False
    return True


def _display_employer(org: str, value: Any = "") -> str:
    explicit = clean(value)
    if explicit:
        return explicit
    known = {
        "akunacapital": "Akuna Capital",
        "dvtrading": "DV Trading",
        "exoduspoint": "ExodusPoint Capital",
        "flowtraders": "Flow Traders",
        "imc": "IMC Trading",
        "janestreet": "Jane Street",
        "jumptrading": "Jump Trading",
        "liontree": "LionTree",
        "marshallwace": "Marshall Wace",
        "mavensecuritiesholdingltd": "Maven Securities",
        "optiver": "Optiver",
        "point72": "Point72",
        "schonfeld": "Schonfeld Strategic Advisors",
        "williamblair": "William Blair",
    }
    return known.get(org.casefold(), org.replace("-", " ").strip().title())


def _location(value: Any) -> str:
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        for key in ("city", "location", "name", "addressLocality"):
            candidate = clean(value.get(key))
            if candidate:
                return candidate
    return ""


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _public_text(value: Any) -> str:
    """Retain source-provided text without turning markup into a description."""

    if isinstance(value, dict):
        value = _first(value, "text", "value", "content", "html")
    return clean(value)


def _description(mapping: dict[str, Any]) -> str:
    """Extract a public role description from common ATS response shapes."""

    value = _first(
        mapping,
        "description",
        "description_text",
        "job_description",
        "jobDescription",
        "description_html",
        "descriptionHtml",
        "content",
    )
    if value in (None, "") and isinstance(mapping.get("content"), dict):
        value = _first(mapping["content"], "description", "descriptionHtml", "text")
    text = _public_text(value)
    if "<" in text and ">" in text:
        text = clean(BeautifulSoup(text, "html.parser").get_text(" ", strip=True))
    return text


def _greenhouse_decoded(raw: Any) -> tuple[Any, list[str]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, (f"invalid JSON payload: {exc.msg}",)
    if isinstance(raw, dict):
        jobs = raw.get("jobs")
        if not isinstance(jobs, list):
            return None, ("schema change: expected a 'jobs' list",)
        return raw, ()
    if isinstance(raw, list):
        # This variant exists in older public Greenhouse embeds and is safe to
        # accept as a structured posting list.
        return {"jobs": raw}, ()
    return None, ("schema change: expected a JSON object with a 'jobs' list",)


def parse_greenhouse_payload_report(raw: Any, org: str) -> ParseReport:
    """Parse one Greenhouse board and retain shape errors for the adapter."""

    payload, errors = _greenhouse_decoded(raw)
    if errors:
        return ParseReport(errors=errors)
    jobs = payload["jobs"]
    malformed = 0

    listings: list[Listing] = []
    for job in jobs:
        if not isinstance(job, dict):
            malformed += 1
            continue
        source_url = clean(job.get("absolute_url") or job.get("url"))
        title = clean(job.get("title"))
        if not title or not valid_url(source_url):
            malformed += 1
            continue
        programme = normalize_programme(
            _first(job, "programme", "programme_type", "programme_name"), title
        )
        if not is_early_careers(title, programme):
            continue
        location = _location(job.get("location"))
        employer = _display_employer(org, job.get("company_name"))
        posted_raw = _first(
            job,
            "published_at",
            "publication_date",
            "first_published",
            "first_published_at",
            "created_at",
            "date_posted",
            "posted_at",
        )
        deadline_raw = _first(
            job,
            "deadline",
            "deadline_at",
            "application_deadline",
            "close_at",
            "valid_through",
        )
        job_id = clean(job.get("id")) or normalize_url(source_url)
        listings.append(
            Listing(
                employer=employer,
                title=title,
                url=source_url,
                location=location,
                programme=programme,
                source_id=f"greenhouse:{org}:{job_id}",
                posted_at=parse_timestamp(posted_raw),
                deadline=parse_deadline(deadline_raw),
                description=_description(job),
                deadline_text=_public_text(deadline_raw),
                posted_text=_public_text(posted_raw),
            )
        )

    unique: dict[tuple[str, str, str, str], Listing] = {}
    duplicates = 0
    for listing in listings:
        key = (
            listing.employer.casefold(),
            listing.title.casefold(),
            normalize_url(listing.url),
            listing.location.casefold(),
        )
        if key in unique:
            duplicates += 1
        else:
            unique[key] = listing
    return ParseReport(
        listings=tuple(unique.values()),
        total_records=len(jobs),
        malformed_records=malformed,
        duplicate_records=duplicates,
        filtered_records=max(0, len(jobs) - malformed - len(listings)),
    )


def parse_greenhouse_payload(raw: Any, org: str) -> list[Listing]:
    return list(parse_greenhouse_payload_report(raw, org).listings)


def _recruitee_decoded(raw: Any) -> tuple[Any, list[str]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, (f"invalid JSON payload: {exc.msg}",)
    if not isinstance(raw, dict) or not isinstance(raw.get("offers"), list):
        return None, ("schema change: expected an 'offers' list",)
    return raw, ()


def parse_recruitee_payload_report(raw: Any, company: str = "Silverpeak") -> ParseReport:
    payload, errors = _recruitee_decoded(raw)
    if errors:
        return ParseReport(errors=errors)

    malformed = 0
    filtered = 0
    listings: list[Listing] = []
    for offer in payload["offers"]:
        if not isinstance(offer, dict):
            malformed += 1
            continue
        status = clean(offer.get("status")).casefold()
        if status and status not in {"published", "active", "open"}:
            filtered += 1
            continue
        title = clean(
            offer.get("title")
            or offer.get("sharing_title")
            or ((offer.get("translations") or {}).get("en") or {}).get("title")
        )
        source_url = clean(offer.get("careers_url") or offer.get("url"))
        apply_url = clean(
            offer.get("careers_apply_url")
            or offer.get("apply_url")
            or source_url
        )
        if not title or not valid_url(source_url) or not valid_url(apply_url):
            malformed += 1
            continue
        programme = normalize_programme(
            _first(offer, "programme", "programme_type", "programme_name"), title
        )
        if not is_early_careers(title, programme):
            filtered += 1
            continue
        location = _location(offer.get("location"))
        if not location:
            location = ", ".join(
                part for part in (clean(offer.get("city")), clean(offer.get("country"))) if part
            )
        posted_raw = _first(
            offer,
            "published_at",
            "publication_date",
            "first_published_at",
            "created_at",
            "date_posted",
            "posted_at",
        )
        deadline_raw = _first(
            offer,
            "deadline",
            "deadline_at",
            "application_deadline",
            "close_at",
        )
        offer_id = clean(offer.get("id") or offer.get("guid") or offer.get("slug"))
        listings.append(
            Listing(
                employer=clean(offer.get("company_name")) or company,
                title=title,
                url=apply_url,
                location=location,
                programme=programme,
                source_id=f"recruitee:{company.casefold()}:{offer_id or normalize_url(source_url)}",
                posted_at=parse_timestamp(posted_raw),
                deadline=parse_deadline(deadline_raw),
                description=_description(offer),
                deadline_text=_public_text(deadline_raw),
                posted_text=_public_text(posted_raw),
            )
        )

    unique: dict[tuple[str, str, str, str], Listing] = {}
    duplicates = 0
    for listing in listings:
        key = (
            listing.employer.casefold(),
            listing.title.casefold(),
            normalize_url(listing.url),
            listing.location.casefold(),
        )
        if key in unique:
            duplicates += 1
        else:
            unique[key] = listing
    return ParseReport(
        listings=tuple(unique.values()),
        total_records=len(payload["offers"]),
        malformed_records=malformed,
        filtered_records=filtered,
        duplicate_records=duplicates,
    )


def parse_recruitee_payload(raw: Any, company: str = "Silverpeak") -> list[Listing]:
    return list(parse_recruitee_payload_report(raw, company).listings)


def _pinpoint_decoded(raw: Any) -> tuple[Any, list[str]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, (f"invalid JSON payload: {exc.msg}",)
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
        return None, ("schema change: expected a 'data' list",)
    return raw, ()


def parse_pinpoint_payload_report(raw: Any, company: str = "Menzies") -> ParseReport:
    payload, errors = _pinpoint_decoded(raw)
    if errors:
        return ParseReport(errors=errors)

    malformed = 0
    filtered = 0
    listings: list[Listing] = []
    for posting in payload["data"]:
        if not isinstance(posting, dict):
            malformed += 1
            continue
        title = clean(posting.get("title") or posting.get("name"))
        source_url = clean(posting.get("url"))
        if not title or not valid_url(source_url):
            malformed += 1
            continue
        programme = normalize_programme(
            _first(posting, "programme", "programme_type", "programme_name"), title
        )
        if not is_early_careers(title, programme):
            filtered += 1
            continue
        location = _location(posting.get("location"))
        posted_raw = _first(
            posting,
            "published_at",
            "publication_date",
            "first_published_at",
            "created_at",
            "date_posted",
            "posted_at",
        )
        deadline_raw = _first(
            posting,
            "deadline",
            "deadline_at",
            "application_deadline",
            "close_at",
        )
        posting_id = clean(posting.get("id") or posting.get("uuid"))
        listings.append(
            Listing(
                employer=clean(posting.get("company_name")) or company,
                title=title,
                url=source_url,
                location=location,
                programme=programme,
                source_id=f"pinpoint:{company.casefold()}:{posting_id or normalize_url(source_url)}",
                posted_at=parse_timestamp(posted_raw),
                deadline=parse_deadline(deadline_raw),
                description=_description(posting),
                deadline_text=_public_text(deadline_raw),
                posted_text=_public_text(posted_raw),
            )
        )

    unique: dict[tuple[str, str, str, str], Listing] = {}
    duplicates = 0
    for listing in listings:
        key = (
            listing.employer.casefold(),
            listing.title.casefold(),
            normalize_url(listing.url),
            listing.location.casefold(),
        )
        if key in unique:
            duplicates += 1
        else:
            unique[key] = listing
    return ParseReport(
        listings=tuple(unique.values()),
        total_records=len(payload["data"]),
        malformed_records=malformed,
        filtered_records=filtered,
        duplicate_records=duplicates,
    )


def parse_pinpoint_payload(raw: Any, company: str = "Menzies") -> list[Listing]:
    return list(parse_pinpoint_payload_report(raw, company).listings)


def _table_header_map(table: Any) -> dict[str, int] | None:
    header_row = table.find("tr")
    if header_row is None:
        return None
    cells = header_row.find_all(["th", "td"], recursive=False)
    headers = [clean(cell.get_text(" ", strip=True)).casefold() for cell in cells]
    aliases = {
        "company": {"company", "employer"},
        "title": {"role", "title", "job", "job title"},
        "programme": {"programme", "program", "programme type"},
        "location": {"location", "locations"},
        "description": {"description", "job description", "details"},
        "deadline": {"deadline", "closing date", "apply by"},
        "posted": {"posted", "date posted", "published", "posted date"},
        "apply": {"apply", "application", "link"},
    }
    result: dict[str, int] = {}
    for field_name, accepted in aliases.items():
        for index, header in enumerate(headers):
            if header in accepted:
                result[field_name] = index
                break
    required = {"company", "title", "programme", "location", "deadline", "apply"}
    return result if required.issubset(result) else None


def _cell_text(cell: Any, *, prefer_title: bool = False) -> str:
    if prefer_title:
        title_attr = clean(cell.get("title"))
        if title_attr:
            return title_attr
    for span in cell.find_all("span"):
        classes = set(span.get("class") or [])
        value = clean(span.get_text(" ", strip=True))
        if value and "new" not in value.casefold() and (
            "truncate" in classes or len(span.find_all("span")) == 0
        ):
            return value
    return clean(cell.get_text(" ", strip=True)).replace("NEW", "").strip()


def _company_cell_text(cell: Any) -> str:
    anchor = cell.find("a", href=True)
    if anchor:
        return clean(anchor.get_text(" ", strip=True))
    return _cell_text(cell)


def _apply_cell_url(cell: Any, base_url: str) -> str:
    candidates = cell.find_all("a", href=True)
    for anchor in candidates:
        label = clean(anchor.get_text(" ", strip=True)).casefold()
        if "apply" in label:
            href = clean(anchor.get("href"))
            return urljoin(base_url, href)
    if candidates:
        return urljoin(base_url, clean(candidates[-1].get("href")))
    return ""


def _page_evidence(soup: BeautifulSoup) -> tuple[int | None, int | None, int | None]:
    text = clean(soup.get_text(" ", strip=True))
    count_match = re.search(
        r"\b([\d,]+)\s+(?:(?:live|active)\s+)?(?:(?:UK|United Kingdom)\s+)?openings?\b",
        text,
        re.IGNORECASE,
    )
    advertised = int(count_match.group(1).replace(",", "")) if count_match else None
    page_match = re.search(r"\bpage\s+(\d+)\s+of\s+(\d+)\b", text, re.IGNORECASE)
    if not page_match:
        return advertised, None, None
    return advertised, int(page_match.group(1)), int(page_match.group(2))


def parse_simplytk_html_report(html: str | None, base_url: str) -> ParseReport:
    if not html or not html.strip():
        return ParseReport(errors=("no HTML response",))
    soup = BeautifulSoup(html, "html.parser")
    advertised, page_number, page_count = _page_evidence(soup)
    table_count = 0
    raw_row_keys: set[tuple[str, str, str, str]] = set()
    malformed = 0
    filtered = 0
    listings: list[Listing] = []

    for table in soup.find_all("table"):
        header_map = _table_header_map(table)
        if header_map is None:
            continue
        table_count += 1
        body_rows = table.select("tbody tr")
        if not body_rows:
            body_rows = table.find_all("tr")[1:]
        for row in body_rows:
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            try:
                company = _company_cell_text(cells[header_map["company"]])
                title = _cell_text(cells[header_map["title"]])
                programme_label = _cell_text(
                    cells[header_map["programme"]], prefer_title=True
                )
                location = _cell_text(cells[header_map["location"]], prefer_title=True)
                description = (
                    _cell_text(cells[header_map["description"]])
                    if "description" in header_map
                    else ""
                )
                deadline_raw = clean(cells[header_map["deadline"]].get_text(" ", strip=True))
                posted_raw = (
                    clean(cells[header_map["posted"]].get_text(" ", strip=True))
                    if "posted" in header_map
                    else ""
                )
                apply_url = _apply_cell_url(cells[header_map["apply"]], base_url)
            except (IndexError, AttributeError):
                malformed += 1
                continue

            row_key = (
                company.casefold(),
                title.casefold(),
                normalize_url(apply_url),
                location.casefold(),
            )
            if row_key in raw_row_keys:
                continue
            raw_row_keys.add(row_key)
            if not company or not title or not valid_url(apply_url):
                malformed += 1
                continue
            programme = normalize_programme(programme_label, title)
            if not is_early_careers(title, programme_label):
                filtered += 1
                continue
            listings.append(
                Listing(
                    employer=company,
                    title=title,
                    url=apply_url,
                    location=location,
                    programme=programme,
                    # The table has no durable requisition ID. A URL-only
                    # alias would merge different roles on one careers page.
                    source_id='',
                    posted_at=parse_timestamp(posted_raw),
                    deadline=parse_deadline(deadline_raw),
                    description=description,
                    deadline_text=deadline_raw,
                    posted_text=posted_raw,
                )
            )

    unique: dict[tuple[str, str, str, str], Listing] = {}
    duplicates = 0
    for listing in listings:
        key = (
            listing.employer.casefold(),
            listing.title.casefold(),
            normalize_url(listing.url),
            listing.location.casefold(),
        )
        if key in unique:
            duplicates += 1
        else:
            unique[key] = listing

    errors: list[str] = []
    if table_count == 0:
        errors.append("no parseable job table found in HTML")
    elif not unique:
        errors.append("no parseable early-careers rows found in HTML")

    captured = len(raw_row_keys)
    complete = (
        table_count > 0
        and not malformed
        and advertised is not None
        and captured == advertised
        and (page_count is None or page_count == 1)
    )
    if not complete and unique:
        if advertised is None:
            errors.append("advertised opening count was not present; capture is incomplete")
        else:
            page_note = f"; page {page_number} of {page_count}" if page_count else ""
            errors.append(
                f"captured {captured} of advertised {advertised} openings{page_note}"
            )
    if malformed and unique:
        errors.append(f"{malformed} table row(s) were malformed")

    return ParseReport(
        listings=tuple(unique.values()),
        total_records=captured,
        malformed_records=malformed,
        filtered_records=filtered,
        duplicate_records=duplicates,
        errors=tuple(errors),
        advertised_count=advertised,
        page_number=page_number,
        page_count=page_count,
        captured_records=captured,
    )


def parse_simplytk_html(html: str | None, base_url: str = "https://simplytk.com/internship-tracker") -> list[Listing]:
    return list(parse_simplytk_html_report(html, base_url).listings)


# Friendly aliases for callers that use provider-oriented names.
parse_pinpoint_postings = parse_pinpoint_payload
parse_recruitee_offers = parse_recruitee_payload
parse_simplytk_table = parse_simplytk_html
