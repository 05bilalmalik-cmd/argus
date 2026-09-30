"""Live Trackr scraper.

The public API (api.the-trackr.com/programmes) applies client fingerprinting:
real browsers get a 200/day rate budget, scripted HTTP clients (httpx etc.)
get 10/day and silently return [] once exhausted. So scraping runs through
Playwright's real Chromium against the live site - same quota as a human
browser tab - and falls back to saved HTML drops when unavailable.

Endpoint contract (verified live):
  GET https://api.the-trackr.com/programmes
      ?region=UK&industry=Finance&season=2027&type=<slug>
  slugs: industrial-placements | spring-weeks | summer-internships |
         off-cycle-internships
  -> {"programmes":[...]} or [...] depending on endpoint mood.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlsplit

from app.scouting.application_window import (
    ApplicationWindowStatus,
    derive_application_window,
    tracker_owned_host,
)
from app.scouting.divisions import infer_division
from app.scouting.trackr_identity import (
    TrackrIdentityConflictError,
    material_identity_signature,
    normalize_trackr_id,
)
from app.scouting.sources.base import _ats_from_url

logger = logging.getLogger(__name__)

TRACKER_URL = "https://app.the-trackr.com/uk-finance/{slug}"
SLUG_TO_TYPE = {
    "industrial-placements": "year_in_industry",
    "spring-weeks": "spring_week",
    "summer-internships": "summer",
    "off-cycle-internships": "summer",
}


class TrackrPayloadIdentityError(TrackrIdentityConflictError):
    """A live response repeated one API ID with conflicting evidence."""


@dataclass(slots=True)
class TrackrRow:
    employer: str
    role_title: str
    tracker_url: str
    employer_application_url: str | None
    opening_date: date | None
    closing_date: date | None
    programme_type: str
    location: str = ""
    source: str = "trackr_live"
    ats_type: str = "unknown"
    explicit_status: object = None
    rolling: bool | None = None
    season: str = ""
    source_record_id: str = ""
    invalid_application_url: bool = False
    # Already-fetched payload evidence, mapped defensively (absent -> neutral).
    # division is a canonical CV-matching slug (never raw provider text);
    # eligibility_note is provenance-labelled free text for review, never
    # parsed into graduation-year bounds.
    division: str = ""
    eligibility_note: str = ""
    cv_required: bool | None = None
    cover_letter_required: bool | None = None
    written_answers_required: bool | None = None

    def __post_init__(self) -> None:
        self.source_record_id = normalize_trackr_id(self.source_record_id)

    @property
    def source_url(self) -> str:
        return self.tracker_url

    @property
    def url(self) -> str:
        """Compatibility alias: the Trackr listing page is provenance only."""

        return self.source_url

    @property
    def application_url(self) -> str | None:
        return self.employer_application_url

    @property
    def deadline(self) -> date | None:
        return self.closing_date

    @property
    def application_window_status(self) -> ApplicationWindowStatus:
        return derive_application_window(
            explicit_status=self.explicit_status,
            opening_date=self.opening_date,
            closing_date=self.closing_date,
            application_url=self.application_url,
            link_signal_known=True,
        )


def _date_of(raw: object) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _clean_url(url: object) -> str:
    """Return a bounded public HTTPS employer target or reject it."""

    if not isinstance(url, str) or len(url) > 2048:
        return ""
    from app.domain.targets import strip_tracking_parameters

    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold().rstrip(".")
        if parts.scheme.casefold() != "https" or not host:
            return ""
        if parts.username is not None or parts.password is not None:
            return ""
        if tracker_owned_host(url):
            return ""
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            (".localhost", ".local", ".internal", ".localdomain")
        ):
            return ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                return ""
        else:
            if not address.is_global:
                return ""
        return strip_tracking_parameters(url, preserve_fragment=True)
    except ValueError:
        return ""


def _location_of(raw: object) -> str:
    if not isinstance(raw, list):
        return ""
    values: list[str] = []
    for item in raw:
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict):
            value = str(item.get("name") or item.get("city") or "").strip()
        else:
            value = ""
        if value and value not in values:
            values.append(value)
    return ", ".join(values)


def _division_slug(raw: object, role_title: str, employer: str) -> str:
    """Return a canonical division slug from provider division text.

    The slug vocabulary is shared with title inference, and the role title
    plus employer are always included, so the result is a superset of the
    title-only fallback evidence — never worse. Empty/absent provider
    divisions return "" so the caller falls back exactly as before.
    """

    parts: list[str] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str) and entry.strip():
                parts.append(entry.replace("|", " "))
    if not parts:
        return ""
    return infer_division(f"{role_title} {' '.join(parts)}", employer)


def _yes_no_required(raw: object) -> bool | None:
    """Map provider Yes/No/Optional requirement flags to booleans."""

    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    normalised = re.sub(r"[^a-z]+", "", str(raw).casefold())
    if normalised == "yes":
        return True
    if normalised in {"no", "optional"}:
        return False
    return None


def _bool_or_none(raw: object) -> bool | None:
    return raw if isinstance(raw, bool) else None


def _eligibility_note(raw: object) -> str:
    """Return provenance-labelled eligibility text, or "" when absent."""

    if not isinstance(raw, str) or not raw.strip():
        return ""
    return f"Trackr eligibility: {raw.strip()[:2000]}"


async def _scrape_via_browser() -> list[dict]:
    """Open the live tracker pages in headless Chromium and capture the API
    responses the page itself makes."""
    from playwright.async_api import async_playwright

    payloads: list[tuple[str, object]] = []

    async def main() -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()

                async def on_response(response):  # noqa: ANN001
                    url = response.url
                    if "api.the-trackr.com/programmes" not in url or "visits" in url:
                        return
                    match = re.search(r"type=([a-z-]+)", url)
                    slug = match.group(1) if match else ""
                    try:
                        payload = await response.json()
                    except Exception:  # noqa: BLE001
                        return
                    payloads.append((slug, payload))

                page.on("response", on_response)
                for slug in SLUG_TO_TYPE:
                    try:
                        await page.goto(
                            TRACKER_URL.format(slug=slug),
                            wait_until="networkidle",
                            timeout=45_000,
                        )
                        await page.wait_for_timeout(1_500)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("trackr page %s failed: %s", slug, exc)
            finally:
                await browser.close()

    await main()
    return payloads


def fetch_programmes(season_hint: str = "") -> list[TrackrRow]:
    """Scrape all four trackers without inventing an employer target."""
    try:
        payloads = asyncio.run(_scrape_via_browser())
    except Exception as exc:  # noqa: BLE001
        logger.error("trackr browser scrape failed entirely: %s", exc)
        return []

    rows: list[TrackrRow] = []
    identified: dict[str, tuple[object, ...]] = {}
    idless_seen: set[tuple[str, str, str, str, str]] = set()
    for slug, payload in payloads:
        programme_type = SLUG_TO_TYPE.get(slug, "other")
        items = (
            payload.get("programmes", [])
            if isinstance(payload, dict)
            else payload
        )
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            company = (item.get("company") or {}).get("name") or "Unknown"
            name = str(item.get("name") or "").strip()
            raw_application_url = item.get("url")
            application_url = _clean_url(raw_application_url) or None
            rolling = item.get("rolling")
            rolling = rolling if isinstance(rolling, bool) else None
            row = TrackrRow(
                employer=company,
                role_title=name,
                tracker_url=TRACKER_URL.format(slug=slug),
                employer_application_url=application_url,
                opening_date=_date_of(item.get("openingDate")),
                closing_date=_date_of(item.get("closingDate")),
                programme_type=programme_type,
                location=_location_of(item.get("locations")),
                source=f"trackr_live:{slug}",
                ats_type=_ats_from_url(application_url or ""),
                explicit_status=item.get("status"),
                rolling=rolling,
                season=str(item.get("season") or season_hint or ""),
                source_record_id=normalize_trackr_id(item.get("id")),
                invalid_application_url=(
                    raw_application_url not in (None, "")
                    and application_url is None
                ),
                division=_division_slug(
                    item.get("divisions"), name, str(company)
                ),
                eligibility_note=_eligibility_note(item.get("eligibility")),
                cv_required=_bool_or_none(item.get("cv")),
                cover_letter_required=_yes_no_required(item.get("coverLetter")),
                written_answers_required=_yes_no_required(
                    item.get("writtenAnswers")
                ),
            )
            if row.source_record_id:
                signature = material_identity_signature(row)
                previous = identified.get(row.source_record_id)
                if previous is not None:
                    if previous != signature:
                        raise TrackrPayloadIdentityError(
                            "conflicting live Trackr rows share programme ID "
                            f"{row.source_record_id!r}"
                        )
                    continue
                identified[row.source_record_id] = signature
            else:
                fallback_key = (
                    slug,
                    str(company).strip().casefold(),
                    name.casefold(),
                    row.location.casefold(),
                    row.tracker_url,
                )
                if fallback_key in idless_seen:
                    continue
                idless_seen.add(fallback_key)
            rows.append(row)
    logger.info("trackr live scrape: %d programmes", len(rows))
    return rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = fetch_programmes()
    print(f"total: {len(result)}")
    with_url = [r for r in result if r.application_url]
    print(f"with application URL: {len(with_url)}")
    by_type: dict[str, int] = {}
    for r in result:
        by_type[r.programme_type] = by_type.get(r.programme_type, 0) + 1
    print("by type:", by_type)
    for r in with_url[:8]:
        print(
            f"  [{r.programme_type}] {r.employer} | "
            f"{r.role_title[:44]} | {str(r.application_url)[:60]}"
        )
