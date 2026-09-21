"""Additional direct employer feeds for the finance-focused tracker.

This module deliberately owns only fixed, public Workday CXS board adapters.  The
board identifiers below were derived from public employer job URLs in
``direct-source-seeds.json`` and then checked with the corresponding public CXS
jobs endpoint.  Collection never uses credentials, cookies, browser state, an
application endpoint, or a user-supplied host.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape
from typing import Any, Mapping
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.tracker.contracts import Listing, SourceResult, utc_now
from app.tracker.source_parsers import (
    ParseReport,
    clean,
    is_early_careers,
    normalize_programme,
    normalize_url,
    parse_deadline,
    parse_timestamp,
    valid_url,
)


# Operational limits are intentionally conservative.  A board can have many
# public roles, but this source must not turn a refresh into an unbounded sweep.
HTTP_TIMEOUT_SECONDS = 5.0
HTTP_TIMEOUT = HTTP_TIMEOUT_SECONDS
MAX_CONCURRENCY = 3
PAGE_SIZE = 20
MAX_WORKDAY_PAGES = 6
MAX_PAGES = MAX_WORKDAY_PAGES
MAX_DETAIL_REQUESTS_PER_BOARD = 32
BOARD_BUDGET_SECONDS = 15.0
COLLECTION_BUDGET_SECONDS = 80.0
VALID_STATUSES = frozenset({"ok", "empty", "partial", "error"})
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 ARGUS-tracker/1.0"
)
SEARCH_TEXTS = ("internship", "placement", "spring", "summer analyst")


# This is a title-level relevance screen, not an eligibility guarantee.  It
# prevents a large enterprise board's software/technology internships from
# inflating a finance tracker.  Board-specific terms cover finance titles whose
# abbreviated names do not contain a general finance word (for example FIG or
# Capital Group's CAP Summer Associate).
_FINANCE_MARKERS = (
    "account analyst",
    "accounting",
    "advisory",
    "alternatives",
    "asset",
    "audit",
    "banking",
    "capital market",
    "capital solution",
    "commercial real estate",
    "compliance",
    "corporate finance",
    "corporate banking",
    "credit",
    "debt capital",
    "equity capital",
    "equity research",
    "financial",
    "finance",
    "financial crime",
    "financial institution",
    "fig",
    "fund",
    "global markets",
    "insurance",
    "investment",
    "leveraged",
    "market",
    "markets",
    "private capital",
    "private equity",
    "private wealth",
    "portfolio",
    "quant",
    "restructuring",
    "risk",
    "sales and trading",
    "strategic advisory",
    "strategic partners",
    "structured trading",
    "tactical opportunities",
    "trade finance",
    "trade support",
    "trading",
    "transaction banking",
    "treasury",
    "valuation",
    "wealth",
)


@dataclass(frozen=True, slots=True)
class WorkdayBoard:
    """One fixed public board, derived from an observed employer role URL."""

    host: str
    tenant: str
    site: str
    employer: str
    seed_url: str
    search_texts: tuple[str, ...] = SEARCH_TEXTS
    relevance_markers: tuple[str, ...] = _FINANCE_MARKERS

    @property
    def jobs_url(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"

    @property
    def name(self) -> str:
        return f"workday:{self.tenant}"

    @property
    def careers_prefix(self) -> str:
        return f"https://{self.host}/en-US/{self.site}"


# Friendly name used by callers that want provider terminology.
WorkdayConfig = WorkdayBoard


def _employer_from_tenant(tenant: str) -> str:
    return re.sub(r"[-_]+", " ", tenant).strip().title()


def derive_workday_board(
    seed_url: str,
    *,
    employer: str | None = None,
    search_texts: tuple[str, ...] = SEARCH_TEXTS,
    relevance_markers: tuple[str, ...] = _FINANCE_MARKERS,
) -> WorkdayBoard:
    """Derive a Workday tenant/site only from a concrete public job URL.

    Both common public paths are accepted: ``/en-US/Site/job/...`` and
    ``/Site/job/...``.  A careers landing page, arbitrary host, or missing role
    route is rejected rather than repaired or guessed.
    """

    if not isinstance(seed_url, str) or not seed_url.strip():
        raise ValueError("seed_url must be a non-empty URL")
    try:
        parsed = urlsplit(seed_url)
        host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError as exc:
        raise ValueError("seed_url is malformed") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("seed_url must be an absolute HTTP(S) URL")
    if not re.fullmatch(
        r"[a-z0-9][a-z0-9-]*\.wd[0-9]+\.(?:myworkdayjobs|myworkdaysite)\.com",
        host,
    ):
        raise ValueError("seed_url must target a Workday public career host")

    parts = [part for part in parsed.path.split("/") if part]
    route_index = next(
        (
            index
            for index, part in enumerate(parts)
            if part.casefold() in {"job", "details"}
        ),
        None,
    )
    if route_index is None or route_index == 0 or route_index + 1 >= len(parts):
        raise ValueError("seed_url must contain a concrete Workday job route")
    site = parts[route_index - 1]
    tenant = host.split(".", 1)[0]
    if not site or not tenant:
        raise ValueError("seed_url did not contain a Workday tenant/site")

    terms = tuple(clean(value) for value in search_texts if clean(value))
    if not terms:
        raise ValueError("search_texts must contain at least one term")
    markers = tuple(clean(value).casefold() for value in relevance_markers if clean(value))
    if not markers:
        raise ValueError("relevance_markers must contain at least one term")
    return WorkdayBoard(
        host=host,
        tenant=tenant,
        site=site,
        employer=clean(employer) or _employer_from_tenant(tenant),
        seed_url=seed_url,
        search_texts=terms,
        relevance_markers=markers,
    )


# These are the only boards registered by this module.  Every seed is an
# observed role URL from direct-source-seeds.json; no tenant or site is guessed.
# The CXS responses for these boards were independently probed on 2026-09-11.
WORKDAY_BOARDS: tuple[WorkdayBoard, ...] = (
    derive_workday_board(
        "https://lbg.wd3.myworkdayjobs.com/en-US/Undergraduate_careers/job/London/Corporate-Banking---Markets-Industrial-Placement_163447",
        employer="Lloyds Banking Group",
        search_texts=("industrial placement", "spring week"),
    ),
    derive_workday_board(
        "https://barclays.wd3.myworkdayjobs.com/en-US/External_Career_Site_Barclays/job/Birmingham-One-Snow-Hill/UK-Corporate-Banking-Summer-Internship-Programme-2027-Birmingham_JR-0000124204",
        employer="Barclays",
        search_texts=("internship", "industrial placement", "spring week"),
    ),
    derive_workday_board(
        "https://mufgub.wd3.myworkdayjobs.com/en-US/MUFG-EarlyCareers/job/XMLNAME-2027-MUFG-UK-Summer-Analyst-Programme--Capital-Markets_10079265-WD",
        employer="MUFG",
        search_texts=("internship", "spring week", "summer analyst"),
    ),
    derive_workday_board(
        "https://citi.wd5.myworkdayjobs.com/en-US/2/job/London--United-Kingdom/Banking--Financing--Summer-Analyst--London---United-Kingdom-2027_26993182",
        employer="Citi",
        search_texts=("internship", "industrial placement", "spring week", "summer analyst"),
    ),
    derive_workday_board(
        "https://hl.wd1.myworkdayjobs.com/en-US/Campus/job/XMLNAME-2027-Summer-Financial-Analyst---Corporate-Finance--M-A-_R3564",
        employer="Houlihan Lokey",
        search_texts=("internship", "summer analyst"),
    ),
    derive_workday_board(
        "https://blackstone.wd1.myworkdayjobs.com/en-US/Blackstone_Campus_Careers/job/London/XMLNAME-2027-Blackstone-Tactical-Opportunities-Summer-Analyst--London-_43179",
        employer="Blackstone",
        search_texts=("internship", "industrial placement", "summer analyst"),
    ),
    derive_workday_board(
        "https://alantra.wd3.myworkdayjobs.com/en-US/Alantra/job/FIG-Summer-Internship--London--UK-_JR489-1",
        employer="Alantra",
        search_texts=("internship", "summer analyst"),
    ),
    derive_workday_board(
        "https://pjtpartners.wd1.myworkdayjobs.com/en-US/Students/job/XMLNAME-2027-Summer-Analyst--PJT-Park-Hill---Private-Capital-Solutions--London_R0003511",
        employer="PJT Partners",
        search_texts=("internship", "summer analyst"),
    ),
    derive_workday_board(
        "https://capgroup.wd1.myworkdayjobs.com/en-US/capitalgroupcareers/job/CAP-Summer-Associate---Europe--2027-_JR7201",
        employer="Capital Group",
        search_texts=("internship", "summer analyst"),
        relevance_markers=_FINANCE_MARKERS + ("summer associate",),
    ),
    derive_workday_board(
        "https://wf.wd1.myworkdayjobs.com/en-US/WellsFargoJobs/job/CITY-OF-LONDON/EMEA-Banking-Summer-Analyst_R-570654",
        employer="Wells Fargo",
        search_texts=("internship", "industrial placement", "summer analyst"),
    ),
    derive_workday_board(
        "https://pimco.wd1.myworkdayjobs.com/en-US/pimco-careers/job/XMLNAME-2027-Summer-Intern---Technology-Analyst--Software-Engineering--EMEA_R106800",
        employer="PIMCO",
        search_texts=("internship", "summer analyst"),
    ),
)

# Alias that makes the fixed registration visible to a parent integrator.
SOURCE_SPECS = WORKDAY_BOARDS


def _http_headers(*, accept: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": "en-GB,en;q=0.9",
    }


def http_post_json(url: str, payload: Mapping[str, object]) -> Any:
    """POST one public Workday search request; never an application request."""

    import httpx

    response = httpx.post(
        url,
        json=dict(payload),
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={**_http_headers(accept="application/json"), "Content-Type": "application/json"},
        follow_redirects=False,
        trust_env=False,
    )
    response.raise_for_status()
    return response.json()


def http_get_json(url: str) -> Any:
    """GET one public Workday job-detail response."""

    import httpx

    response = httpx.get(
        url,
        timeout=HTTP_TIMEOUT_SECONDS,
        headers=_http_headers(accept="application/json,text/plain;q=0.9,*/*;q=0.8"),
        follow_redirects=False,
        trust_env=False,
    )
    response.raise_for_status()
    return response.json()


def _exception_message(exc: BaseException, url: str) -> str:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        reason = getattr(response, "reason_phrase", "") or ""
        suffix = f" {reason}" if reason else ""
        return f"HTTP {status_code}{suffix} from {url}"
    detail = str(exc).strip()
    return f"{type(exc).__name__} from {url}: {detail}" if detail else f"{type(exc).__name__} from {url}"


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        text = clean(value)
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            for text in _strings(item):
                if text not in result:
                    result.append(text)
        return result
    if isinstance(value, Mapping):
        for key in ("name", "text", "value", "location", "city"):
            candidate = clean(value.get(key))
            if candidate:
                return [candidate]
    return []


def _location_text(mapping: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in ("locationsText", "location", "locations", "additionalLocations"):
        for value in _strings(mapping.get(key)):
            if value not in values:
                values.append(value)
    requisition_location = mapping.get("jobRequisitionLocation")
    if isinstance(requisition_location, Mapping):
        for key in ("name", "location", "city", "country"):
            for value in _strings(requisition_location.get(key)):
                if value not in values:
                    values.append(value)
    return ", ".join(values)


def _description_text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = _first(value, "text", "value", "content", "html")
    if value in (None, ""):
        return ""
    text = unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return clean(text)


def _bullet_fields(posting: Mapping[str, Any]) -> list[str]:
    for key in ("bulletFields", "bullet_fields"):
        values = _strings(posting.get(key))
        if values:
            return values
    return []


def _requisition_id(mapping: Mapping[str, Any], external_path: str = "") -> str:
    direct = _first(mapping, "jobReqId", "jobPostingId", "requisitionId", "id")
    if direct not in (None, "") and not isinstance(direct, bool):
        return clean(direct)
    for value in _bullet_fields(mapping):
        if not re.search(r"\b(?:posting|end|date|days?|left|apply)\b", value, re.I):
            return value
    slug = external_path.rstrip("/").rsplit("/", 1)[-1]
    return clean(slug)


def _external_path(value: Any) -> str:
    path = clean(value)
    if not path.startswith("/"):
        return ""
    if not re.match(r"^/(?:job|details)/[^/]+(?:/[^/]*)?$", path, re.I):
        return ""
    return path


def _public_job_url(board: WorkdayBoard, external_path: str) -> str:
    return f"{board.careers_prefix}{external_path}"


def _path_from_public_url(board: WorkdayBoard, url: str) -> str:
    try:
        parts = [part for part in urlsplit(url).path.split("/") if part]
    except ValueError:
        return ""
    site_index = next(
        (index for index, part in enumerate(parts) if part.casefold() == board.site.casefold()),
        None,
    )
    if site_index is None:
        return ""
    route_index = next(
        (
            index
            for index in range(site_index + 1, len(parts))
            if parts[index].casefold() in {"job", "details"}
        ),
        None,
    )
    if route_index is None:
        return ""
    return "/" + "/".join(parts[route_index:])


def _finance_relevant(title: str, board: WorkdayBoard) -> bool:
    lowered = clean(title).casefold()
    if not lowered:
        return False
    return any(
        re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", lowered)
        for marker in board.relevance_markers
    )


def _date_from_source_text(value: Any) -> str | None:
    raw = clean(value)
    if not raw:
        return None
    direct = parse_deadline(raw)
    if direct:
        return direct
    patterns = (
        r"\b\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?\b",
        r"\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+\d{4}\b",
        r"\b[A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+\d{4}\b",
        r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if match:
            parsed = parse_deadline(match.group(0))
            if parsed:
                return parsed
    return None


def _posted_fields(mapping: Mapping[str, Any], fallback: Listing | None = None) -> tuple[str | None, str]:
    explicit_keys = (
        "postedAt",
        "posted_at",
        "publishedAt",
        "published_at",
        "datePosted",
        "publicationDate",
        "firstPublished",
        "firstPublishedAt",
        "postedDate",
        # Workday's public detail contract exposes the posting start as
        # startDate. It is distinct from updatedAt and matches postedOn in
        # observed responses; it is not an update-time fallback.
        "startDate",
    )
    posted_raw: Any = _first(mapping, "postedOn", "posted_text", "postedText")
    posted_at = parse_timestamp(posted_raw) if posted_raw not in (None, "") else None
    for key in explicit_keys:
        candidate = mapping.get(key)
        if candidate in (None, "") or isinstance(candidate, bool):
            continue
        # Prefer an explicit date in postedOn when a provider supplies one;
        # relative postedOn text remains visible while startDate can provide
        # the provider's explicit posting-start date as a separate signal.
        if posted_at is None:
            posted_at = parse_timestamp(candidate)
        if posted_raw in (None, ""):
            posted_raw = candidate
        if posted_at is not None:
            break
    if posted_raw in (None, "") and fallback is not None:
        posted_raw = fallback.posted_text
        if posted_at is None:
            posted_at = fallback.posted_at
    return posted_at, clean(posted_raw)


def _deadline_fields(mapping: Mapping[str, Any], fallback: Listing | None = None) -> tuple[str | None, str]:
    raw = _first(
        mapping,
        "jobPostingEndDateAsText",
        "applicationDeadline",
        "application_deadline",
        "deadline",
        "closingDate",
        "closeDate",
        "endDate",
    )
    if isinstance(raw, (list, tuple)):
        values = _strings(raw)
        raw = next(
            (
                value
                for value in values
                if re.search(r"\b(?:deadline|close|closing|end\s+date|posting\s+end)\b", value, re.I)
                or _date_from_source_text(value)
            ),
            None,
        )
    if raw in (None, "") and fallback is not None:
        raw = fallback.deadline_text
    text = clean(raw)
    return _date_from_source_text(text), text


def _source_id(board: WorkdayBoard, mapping: Mapping[str, Any], external_path: str, fallback: str = "") -> str:
    identifier = _requisition_id(mapping, external_path) or clean(fallback)
    return f"workday:{board.tenant}:{identifier}" if identifier else ""


def _error_result(*, board: WorkdayBoard, error: str, started: float) -> SourceResult:
    return SourceResult(
        name=board.name,
        url=board.jobs_url,
        status="error",
        listings=[],
        error=clean(error),
        checked_at=utc_now(),
        elapsed_seconds=max(0.0, time.monotonic() - started),
    )


def parse_workday_payload_report(raw: Any, board: WorkdayBoard) -> ParseReport:
    """Parse one public CXS search response into finance early-careers rows."""

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return ParseReport(errors=(f"invalid JSON payload: {exc.msg}",))
    if not isinstance(raw, Mapping):
        return ParseReport(errors=("schema change: expected a JSON object",))
    postings = raw.get("jobPostings")
    if not isinstance(postings, list):
        return ParseReport(errors=("schema change: expected a 'jobPostings' list",))
    advertised = raw.get("total")
    if isinstance(advertised, bool) or not isinstance(advertised, int) or advertised < 0:
        return ParseReport(errors=("schema change: expected a non-negative integer 'total'",))

    malformed = 0
    filtered = 0
    duplicates = 0
    listings: list[Listing] = []
    seen: set[str] = set()
    for posting in postings:
        if not isinstance(posting, Mapping):
            malformed += 1
            continue
        title = clean(posting.get("title"))
        external_path = _external_path(posting.get("externalPath") or posting.get("external_path"))
        if not title or not external_path:
            malformed += 1
            continue
        key = normalize_url(_public_job_url(board, external_path))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        if not is_early_careers(title) or not _finance_relevant(title, board):
            filtered += 1
            continue
        posted_at, posted_text = _posted_fields(posting)
        deadline, deadline_text = _deadline_fields(
            {"bulletFields": _bullet_fields(posting)}
        )
        listings.append(
            Listing(
                employer=board.employer,
                title=title,
                url=_public_job_url(board, external_path),
                location=_location_text(posting),
                programme=normalize_programme("", title),
                source_id=_source_id(board, posting, external_path),
                posted_at=posted_at,
                deadline=deadline,
                description="",
                deadline_text=deadline_text,
                posted_text=posted_text,
            )
        )

    return ParseReport(
        listings=tuple(listings),
        total_records=len(postings),
        malformed_records=malformed,
        filtered_records=filtered,
        duplicate_records=duplicates,
        advertised_count=advertised,
        captured_records=len(postings),
    )


def parse_workday_payload(raw: Any, board: WorkdayBoard) -> list[Listing]:
    return list(parse_workday_payload_report(raw, board).listings)


def parse_workday_detail(
    raw: Any,
    board: WorkdayBoard,
    fallback: Listing | None = None,
) -> Listing | None:
    """Parse one public CXS detail response, retaining source metadata."""

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, Mapping) or not isinstance(raw.get("jobPostingInfo"), Mapping):
        return None
    info = raw["jobPostingInfo"]
    title = clean(info.get("title")) or (fallback.title if fallback else "")
    external_path = _external_path(info.get("externalPath"))
    if not external_path and fallback is not None:
        external_path = _path_from_public_url(board, fallback.url)
    if not title or not external_path:
        return None
    if not is_early_careers(title) or not _finance_relevant(title, board):
        return None

    posted_at, posted_text = _posted_fields(info, fallback)
    deadline, deadline_text = _deadline_fields(info, fallback)
    location = _location_text(info) or (fallback.location if fallback else "")
    source_id = _source_id(
        board,
        info,
        external_path,
        fallback.source_id.removeprefix(f"workday:{board.tenant}:") if fallback else "",
    )
    return Listing(
        employer=board.employer,
        title=title,
        url=_public_job_url(board, external_path),
        location=location,
        programme=normalize_programme("", title),
        source_id=source_id,
        posted_at=posted_at,
        deadline=deadline,
        description=_description_text(info.get("jobDescription") or info.get("description")),
        deadline_text=deadline_text,
        posted_text=posted_text,
    )


# Provider-oriented aliases make the pure parser easy to discover in tests and
# future parent integration without changing the fixed SourceResult contract.
parse_workday_job_search = parse_workday_payload
parse_workday_job_detail = parse_workday_detail


def _listing_key(listing: Listing) -> str:
    if listing.source_id:
        return listing.source_id.casefold()
    return normalize_url(listing.url).casefold()


def _merge_unique(listings: list[Listing]) -> list[Listing]:
    unique: dict[str, Listing] = {}
    for listing in listings:
        key = _listing_key(listing)
        if key not in unique:
            unique[key] = listing
    return list(unique.values())


def _run_board(board: WorkdayBoard, overall_deadline: float | None = None) -> SourceResult:
    started = time.monotonic()
    board_deadline = started + BOARD_BUDGET_SECONDS
    if overall_deadline is not None:
        board_deadline = min(board_deadline, overall_deadline)
    errors: list[str] = []
    candidates: dict[str, Listing] = {}
    raw_rows = 0
    filtered_rows = 0
    malformed_rows = 0
    duplicate_rows = 0
    pages = 0

    for search_text in board.search_texts:
        offset = 0
        page_number = 0
        first_total: int | None = None
        seen_pages: set[tuple[str, ...]] = set()
        while True:
            if time.monotonic() >= board_deadline:
                errors.append(f"time budget reached during search {search_text!r}")
                break
            if page_number >= MAX_WORKDAY_PAGES:
                if first_total is not None and offset < first_total:
                    errors.append(
                        f"pagination cap reached for {search_text!r} at {MAX_WORKDAY_PAGES} pages"
                    )
                break
            request = {
                "appliedFacets": {},
                "limit": PAGE_SIZE,
                "offset": offset,
                "searchText": search_text,
            }
            try:
                payload = http_post_json(board.jobs_url, request)
            except Exception as exc:  # noqa: BLE001 - isolate one public board
                errors.append(f"search {search_text!r}: {_exception_message(exc, board.jobs_url)}")
                break
            report = parse_workday_payload_report(payload, board)
            if report.errors:
                errors.extend(f"search {search_text!r}: {error}" for error in report.errors)
                break
            first_total = report.advertised_count if first_total is None else first_total
            if report.advertised_count != first_total:
                errors.append(f"search {search_text!r}: advertised total changed during pagination")
                break
            raw_rows += report.total_records or 0
            filtered_rows += report.filtered_records
            malformed_rows += report.malformed_records
            duplicate_rows += report.duplicate_records
            pages += 1
            page_keys = tuple(sorted(_listing_key(item) for item in report.listings))
            if page_keys and page_keys in seen_pages:
                errors.append(f"search {search_text!r}: repeated page")
                break
            if page_keys:
                seen_pages.add(page_keys)
            for listing in report.listings:
                candidates.setdefault(_listing_key(listing), listing)
            page_number += 1
            if first_total == 0 or offset + PAGE_SIZE >= first_total:
                break
            if not report.total_records:
                errors.append(
                    f"search {search_text!r}: empty page before advertised total {first_total}"
                )
                break
            offset += PAGE_SIZE

    if len(candidates) > MAX_DETAIL_REQUESTS_PER_BOARD:
        errors.append(
            f"detail cap reached at {MAX_DETAIL_REQUESTS_PER_BOARD} of {len(candidates)} candidates"
        )
    candidate_values = list(candidates.values())
    detail_limit = min(len(candidate_values), MAX_DETAIL_REQUESTS_PER_BOARD)
    listings: list[Listing] = []
    for index, fallback in enumerate(candidate_values[:detail_limit]):
        if time.monotonic() >= board_deadline:
            errors.append(f"time budget reached before {len(candidate_values) - index} detail request(s)")
            listings.extend(candidate_values[index:])
            break
        external_path = _path_from_public_url(board, fallback.url)
        detail_url = f"https://{board.host}/wday/cxs/{board.tenant}/{board.site}{external_path}"
        detail_failed = False
        try:
            detail_payload = http_get_json(detail_url)
            detail = parse_workday_detail(detail_payload, board, fallback)
            detail_failed = detail is None
        except Exception as exc:  # noqa: BLE001 - retain list row on detail failure
            errors.append(f"detail {fallback.source_id or fallback.title!r}: {_exception_message(exc, detail_url)}")
            detail = None
            detail_failed = True
        if detail is None:
            # The list response is still a valid public observation.  It lacks
            # detail metadata but must not disappear merely because one detail
            # endpoint changed or timed out.
            listings.append(fallback)
            if detail_failed and (not errors or not errors[-1].startswith("detail ")):
                errors.append(f"detail response was not parseable for {fallback.title!r}")
        else:
            listings.append(detail)
    else:
        if detail_limit < len(candidate_values):
            listings.extend(candidate_values[detail_limit:])

    listings = _merge_unique(listings)
    if malformed_rows:
        errors.append(f"{malformed_rows} malformed search record(s)")
    if pages and raw_rows:
        errors.append(
            f"captured {raw_rows} search rows; retained {len(listings)} finance early-careers listings"
        ) if errors else None
    elif not pages and not errors:
        errors.append("no public search pages were captured")

    if listings:
        status = "partial" if errors else "ok"
    elif errors:
        status = "error"
    else:
        status = "empty"
    return SourceResult(
        name=board.name,
        url=board.jobs_url,
        status=status,
        listings=listings,
        error="; ".join(errors),
        checked_at=utc_now(),
        elapsed_seconds=max(0.0, time.monotonic() - started),
    )


def _run_board_safe(board: WorkdayBoard, overall_deadline: float | None = None) -> SourceResult:
    started = time.monotonic()
    try:
        return _run_board(board, overall_deadline)
    except Exception as exc:  # noqa: BLE001 - no board may sink a refresh
        return _error_result(
            board=board,
            error=f"{type(exc).__name__}: public board adapter failed",
            started=started,
        )


def collect_additional_sources() -> list[SourceResult]:
    """Collect fixed direct finance boards with at most three workers.

    Search and detail requests are public read-only requests.  Results retain
    list-only rows when a detail request fails and report ``partial`` rather
    than turning a broken detail endpoint into an empty board.  The per-board
    budget, page cap, detail cap, request timeout, and three-worker pool bound
    the total refresh; this function does not touch the legacy sweep/runtime or
    the tracker store.
    """

    boards = tuple(WORKDAY_BOARDS)
    if not boards:
        return []
    collection_deadline = time.monotonic() + COLLECTION_BUDGET_SECONDS
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENCY, len(boards))) as pool:
        futures = [pool.submit(_run_board_safe, board, collection_deadline) for board in boards]
        results = [future.result() for future in futures]
    return [result if result.status in VALID_STATUSES else SourceResult(
        name=result.name,
        url=result.url,
        status="error",
        listings=[],
        error=f"adapter returned invalid status {result.status!r}",
        checked_at=result.checked_at,
        elapsed_seconds=result.elapsed_seconds,
    ) for result in results]


__all__ = [
    "HTTP_TIMEOUT_SECONDS",
    "HTTP_TIMEOUT",
    "MAX_CONCURRENCY",
    "PAGE_SIZE",
    "MAX_WORKDAY_PAGES",
    "MAX_PAGES",
    "MAX_DETAIL_REQUESTS_PER_BOARD",
    "BOARD_BUDGET_SECONDS",
    "COLLECTION_BUDGET_SECONDS",
    "SEARCH_TEXTS",
    "WorkdayBoard",
    "WorkdayConfig",
    "WORKDAY_BOARDS",
    "SOURCE_SPECS",
    "derive_workday_board",
    "derive_workday_config",
    "http_post_json",
    "http_get_json",
    "parse_workday_payload_report",
    "parse_workday_payload",
    "parse_workday_job_search",
    "parse_workday_detail",
    "parse_workday_job_detail",
    "collect_additional_sources",
]


# Alias is defined after the function to keep the implementation's canonical
# name explicit in tracebacks and documentation.
derive_workday_config = derive_workday_board
