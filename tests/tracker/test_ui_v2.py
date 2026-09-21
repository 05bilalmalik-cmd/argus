"""Browser acceptance for the tracker v2 surface.

All responses in this module are clearly labelled local fixtures.  The page is loaded
through Playwright route interception; no tracker server, source, SMTP service, or user
browser is started or contacted.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, Page, sync_playwright  # noqa: E402


ROOT = Path(__file__).parents[2]
INDEX = ROOT / "app" / "tracker" / "static" / "index.html"
STATIC = INDEX.parent


# MOCK FIXTURE: fictional roles only; no live employer or candidate data.
FIXTURE_JOBS = [
    {
        "id": "fixture-open",
        "employer": "Northstar Capital (fixture)",
        "title": "Summer Finance Analyst Intern",
        "url": "https://jobs.example.test/northstar/7",
        "location": "London, UK · Hybrid",
        "programme": "summer",
        "description": "Fixture description for a finance role.",
        "deadline": "2026-11-30",
        "deadline_text": "Applications close 30 November 2026",
        "deadline_basis": "Employer careers posting",
        "posted_at": "2026-08-20T09:00:00+00:00",
        "posted_text": "Posted for the 2027 intake",
        "first_seen": "2026-09-11T08:00:00+00:00",
        "last_seen": "2026-09-11T09:00:00+00:00",
        "verified_at": "2026-09-11T09:05:00+00:00",
        "verification_error": "",
        "availability": "open",
        "match_status": "potential",
        "match_reasons": ["Finance degree aligns with the role family"],
        "match_unknowns": ["Right-to-work policy is not stated"],
        "evidence": [
            {
                "source": "Northstar careers fixture",
                "source_url": "https://jobs.example.test/northstar/7",
                "quote": "Applications close 30 November 2026",
                "observed_at": "2026-09-11T09:05:00+00:00",
            }
        ],
        "sources": ["northstar-fixture"],
        "saved": True,
        "stage": "not_applied",
        "notes": "Fixture note",
        "due_date": None,
        "new": True,
        "stale": False,
        "expired": False,
        "deadline_soon": False,
    },
    {
        "id": "fixture-review",
        "employer": "<Fixture> Research",
        "title": "Review <role> / evidence required",
        "url": "https://jobs.example.test/research/9",
        "location": "Manchester, UK",
        "programme": "year_in_industry",
        "description": "<img src=x onerror=window.__fixtureXss=1>",
        "deadline": None,
        "deadline_text": "Deadline not stated by source",
        "deadline_basis": "No employer deadline evidence",
        "posted_at": None,
        "posted_text": "Posted date not stated by source",
        "first_seen": "2026-09-10T08:00:00+00:00",
        "last_seen": "2026-09-11T09:00:00+00:00",
        "verified_at": None,
        "verification_error": "Role-specific availability could not be verified",
        "availability": "unknown",
        "match_status": "review",
        "match_reasons": ["Programme preference matches"],
        "match_unknowns": ["Grade requirements and work rights are unknown"],
        "evidence": [
            {
                "source": "Research source fixture",
                "source_url": "javascript:alert(1)",
                "quote": "<b>Untrusted source quote</b>",
                "observed_at": "2026-09-11T09:00:00+00:00",
            }
        ],
        "sources": ["research-fixture"],
        "saved": False,
        "stage": "not_applied",
        "notes": "",
        "due_date": None,
        "new": False,
        "stale": False,
        "expired": False,
        "deadline_soon": False,
    },
]

# MOCK FIXTURE: deliberate incomplete health response to prove null/missing is not OK.
FIXTURE_STATUS = {
    "summary": {
        "total": 2,
        "new_24h": 1,
        "saved": 1,
        "source_attention": None,
    },
    "refresh": {
        "running": False,
        "automatic": False,
        "interval_seconds": None,
        "next_refresh_at": None,
        "last_finished_at": None,
        "last_error": "",
    },
    "sources": [],
    "pipeline": {
        "version": 1,
        "started_at": None,
        "finished_at": None,
        "stages": {
            "discovery": {
                "status": "never_checked",
                "last_started_at": None,
                "last_success_at": None,
                "last_finished_at": None,
                "last_error": None,
                "result": None,
            },
            "verification": {"status": "never_checked", "result": None},
            "delivery": {"status": "blocked", "last_error": "Mail setup required", "result": None},
        },
    },
    "intelligence": {
        "status": "never_checked",
        "potential": 1,
        "review": 1,
        "verified_open": 1,
        "unknown": 1,
    },
    "alerts": {
        "config_status": "unconfigured",
        "configured": False,
        "enabled": False,
        "pending": 1,
        "last_error": "SMTP is not configured",
    },
}

# MOCK FIXTURE: extra fields are intentionally present to verify raw profile merging.
FIXTURE_PROFILE = {
    "degree": "Finance",
    "graduation_years": {"summer": 2028, "year_in_industry": 2029, "spring_week": 2029},
    "desired_roles": ["summer", "year_in_industry", "spring_week"],
    "desired_locations": ["UK"],
    "grades": {"status": "unknown"},
    "work_rights": None,
    "fixture_extra": "preserve-this-field",
}

# MOCK FIXTURE: sent is not treated as delivered when the provider did not confirm it.
FIXTURE_ALERTS = {
    "events": [
        {
            "id": "fixture-event-1",
            "event_key": "new-match",
            "status": "sent",
            "kind": "match",
            "created_at": "2026-09-11T09:10:00+00:00",
            "last_error": None,
            "subject": "<strong>Fixture alert subject</strong>",
            "delivered_at": None,
            "message_id": "fixture-message-1",
        }
    ],
    "summary": {
        "config_status": "unconfigured",
        "configured": False,
        "enabled": False,
        "pending": 1,
        "last_error": "SMTP is not configured",
    },
}


class FixtureApi:
    """Route-only local API fixture; it never forwards a request to the network."""

    def __init__(self) -> None:
        self.jobs = json.loads(json.dumps(FIXTURE_JOBS))
        self.detail_records = {job["id"]: json.loads(json.dumps(job)) for job in FIXTURE_JOBS}
        self.profile = json.loads(json.dumps(FIXTURE_PROFILE))
        self.alerts = json.loads(json.dumps(FIXTURE_ALERTS))
        self.requests: list[dict[str, object]] = []
        self.profile_puts: list[dict[str, object]] = []

    def _record(self, request, path: str, query: dict[str, list[str]]) -> None:
        body = None
        if request.post_data:
            try:
                body = json.loads(request.post_data)
            except json.JSONDecodeError:
                body = request.post_data
        self.requests.append(
            {
                "method": request.method,
                "path": path,
                "query": query,
                "body": body,
                "headers": dict(request.headers),
            }
        )

    def handle(self, route) -> None:
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)

        if path == "/":
            route.fulfill(status=200, content_type="text/html", body=INDEX.read_text(encoding="utf-8"))
            return
        if path.startswith("/static/"):
            asset = STATIC / Path(path).name
            if asset in {STATIC / "index.html", STATIC / "tracker.js", STATIC / "tracker.css"}:
                content_type = "text/javascript" if asset.suffix == ".js" else "text/css"
                route.fulfill(status=200, content_type=content_type, body=asset.read_text(encoding="utf-8"))
            else:
                route.fulfill(status=404, body="not a fixture asset")
            return
        if not path.startswith("/api/"):
            route.fulfill(status=404, body="fixture route not found")
            return

        self._record(request, path, query)
        if request.method == "GET" and path == "/api/status":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(FIXTURE_STATUS))
            return
        if request.method == "GET" and path == "/api/profile":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"profile": self.profile}),
            )
            return
        if request.method == "GET" and path == "/api/alerts":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(self.alerts))
            return
        if request.method == "GET" and path == "/api/jobs":
            rows = self.jobs
            q = query.get("q", [""])[0].casefold().strip()
            if q:
                rows = [
                    job
                    for job in rows
                    if q in " ".join(
                        str(job.get(key, "")) for key in ("employer", "title", "location")
                    ).casefold()
                ]
            wanted = query.get("match_status", [""])[0]
            if wanted:
                allowed = set(wanted.split(","))
                rows = [job for job in rows if job["match_status"] in allowed]
            availability = query.get("availability", [""])[0]
            if availability:
                rows = [job for job in rows if job["availability"] == availability]
            if query.get("saved_only", ["false"])[0] == "true":
                rows = [job for job in rows if job["saved"]]
            payload = {
                "jobs": rows,
                "total": len(rows),
                "offset": int(query.get("offset", ["0"])[0]),
                "limit": int(query.get("limit", ["100"])[0]),
                "has_more": False,
            }
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        if request.method == "GET" and path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            job = next((row for row in self.jobs if row["id"] == job_id), self.detail_records.get(job_id))
            if job is None:
                route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "missing"}))
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(job))
            return
        if request.method == "PATCH" and path.startswith("/api/jobs/"):
            body = json.loads(request.post_data or "{}")
            job_id = path.rsplit("/", 1)[-1]
            job = next(row for row in self.jobs if row["id"] == job_id)
            job.update(body)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(job))
            return
        if request.method == "PUT" and path == "/api/profile":
            body = json.loads(request.post_data or "{}")
            self.profile_puts.append(body)
            self.profile = body
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"profile": self.profile}),
            )
            return
        if request.method == "POST" and path == "/api/refresh":
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps({"accepted": True}),
            )
            return
        route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "unsupported fixture"}))


@pytest.fixture(scope="module")
def browser() -> Browser:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def fixture_page(browser: Browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    fixture = FixtureApi()
    page.route("**/*", fixture.handle)
    yield page, fixture
    context.close()


def open_fixture(page: Page) -> None:
    page.goto("http://argus-fixture.test/")
    page.locator("#job-rows tr").first.wait_for(state="visible")


def wait_for_request(fixture: FixtureApi, path: str, predicate=None) -> dict[str, object]:
    for _ in range(40):
        candidates = [item for item in fixture.requests if item["path"] == path]
        if candidates and (predicate is None or predicate(candidates[-1])):
            return candidates[-1]
        # This is a browser polling wait, not a service or network retry.
        import time

        time.sleep(0.025)
    raise AssertionError(f"fixture request not observed: {path}")


def test_evidence_rows_detail_provenance_and_never_checked_health(fixture_page) -> None:
    page, fixture = fixture_page
    console_errors: list[str] = []
    page.on("pageerror", lambda error: console_errors.append(str(error)))
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    open_fixture(page)

    assert page.locator("#job-rows").inner_text().find("Applications close 30 November 2026") >= 0
    assert page.locator("#job-rows").inner_text().find("Posted for the 2027 intake") >= 0
    assert page.locator("#job-rows").inner_text().find("First detected") >= 0
    assert page.locator("#pipeline-panel").get_by_text("NEVER CHECKED").count() >= 1

    page.get_by_role("button", name=re.compile("Source health")).click()
    assert "never checked" in page.locator("#source-rows").inner_text().casefold()
    assert "no source check has completed" in page.locator("#source-empty").inner_text().casefold()
    assert page.locator("#nav-health").inner_text() != "OK"

    page.get_by_role("button", name=re.compile("All openings")).click()
    page.locator(".role-title").first.click()
    assert page.locator("#details").get_attribute("open") is not None
    assert page.locator("#detail-provenance").is_visible()
    assert "Right-to-work policy" in page.locator("#detail-unknowns").inner_text()
    assert page.locator("#detail-evidence a").first.get_attribute("href") == "https://jobs.example.test/northstar/7"
    assert "Source posted" in page.locator("#detail-facts").inner_text()
    assert "First detected by ARGUS" in page.locator("#detail-facts").inner_text()
    page.get_by_role("button", name="Close details").click()

    assert not console_errors, console_errors
    assert any(item["path"] == "/api/status" for item in fixture.requests)


def test_fit_availability_priority_queries_keep_legacy_filters(fixture_page) -> None:
    page, fixture = fixture_page
    open_fixture(page)

    page.locator("#search").fill("Northstar")
    page.locator("#programme").select_option("summer")
    page.locator("#stage").select_option("not_applied")
    page.locator("#match-status").select_option("potential,review")
    page.locator("#availability").select_option("open")
    page.locator("#sort").select_option("priority")
    page.locator("#deadline-soon").check()

    request = wait_for_request(
        fixture,
        "/api/jobs",
        lambda item: item["query"].get("sort") == ["priority"],
    )
    query = request["query"]
    assert query["match_status"] == ["potential,review"]
    assert query["availability"] == ["open"]
    assert query["programmes"] == ["summer"]
    assert query["stage"] == ["not_applied"]
    assert query["uk_only"] == ["true"]
    assert query["deadline_soon"] == ["true"]
    assert "saved_only" in query


def test_profile_put_preserves_extra_fields_without_source_refresh(fixture_page) -> None:
    page, fixture = fixture_page
    open_fixture(page)
    page.get_by_role("button", name=re.compile("Automation")).click()
    page.locator("#profile-degree").fill("Accounting and Finance")
    page.locator("#profile-summer-year").fill("2030")
    page.locator("#profile-industry-year").fill("2031")
    page.locator("#profile-spring-year").fill("2031")
    page.get_by_role("button", name="Save profile").click()

    for _ in range(40):
        if fixture.profile_puts:
            break
        import time

        time.sleep(0.025)
    for _ in range(40):
        if "cached matching" in page.locator("#profile-status").inner_text().casefold():
            break
        import time

        time.sleep(0.025)
    assert fixture.profile_puts
    saved = fixture.profile_puts[-1]
    assert saved["degree"] == "Accounting and Finance"
    assert saved["graduation_years"] == {"summer": 2030, "year_in_industry": 2031, "spring_week": 2031}
    assert saved["grades"] == {"status": "unknown"}
    assert saved["work_rights"] is None
    assert saved["fixture_extra"] == "preserve-this-field"
    assert not any(item["method"] == "POST" and item["path"] == "/api/refresh" for item in fixture.requests)
    assert "cached matching" in page.locator("#profile-status").inner_text().casefold()
    put = fixture.requests[-1]
    assert put["headers"].get("x-argus-tracker") == "1"


def test_alert_history_is_uncertain_and_uses_secure_setup_link(fixture_page) -> None:
    page, fixture = fixture_page
    open_fixture(page)
    page.get_by_role("button", name=re.compile("Alert")).click()
    page.locator("#alert-rows tr").first.wait_for(state="visible")

    text = page.locator("#alerts-panel").inner_text()
    assert "<strong>Fixture alert subject</strong>" in text
    assert page.locator("#alert-rows strong").count() == 0
    assert "delivery not confirmed" in text.casefold()
    assert page.locator("#email-settings-link").get_attribute("href") == "/settings/email"
    assert fixture.alerts["events"][0]["status"] == "sent"
    assert "delivered" not in page.locator("#alert-rows").inner_text().casefold()



def test_notes_progress_and_save_controls_use_tracker_mutation(fixture_page) -> None:
    page, fixture = fixture_page
    open_fixture(page)

    page.locator("#job-rows tr").first.locator(".save").click()
    save_request = wait_for_request(
        fixture,
        "/api/jobs/fixture-open",
        lambda item: item["method"] == "PATCH" and item["body"].get("saved") is False,
    )
    assert save_request["headers"].get("x-argus-tracker") == "1"

    page.locator(".role-title").first.click()
    page.locator("#detail-stage").select_option("assessment")
    page.locator("#detail-due").fill("2026-10-01")
    page.locator("#detail-notes").fill("Fixture notes only")
    page.get_by_role("button", name="Save notes").click()
    note_request = wait_for_request(
        fixture,
        "/api/jobs/fixture-open",
        lambda item: item["method"] == "PATCH" and item["body"].get("stage") == "assessment",
    )
    assert note_request["body"] == {"stage": "assessment", "due_date": "2026-10-01", "notes": "Fixture notes only"}
    for _ in range(40):
        if not page.locator("#details").is_visible():
            break
        import time

        time.sleep(0.025)
    assert not page.locator("#details").is_visible()


def test_hash_details_and_mobile_surface_have_no_horizontal_page_overflow(fixture_page) -> None:
    page, fixture = fixture_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("http://argus-fixture.test/#job=fixture-review")
    page.locator("#details").wait_for(state="visible")
    assert page.locator("#detail-title").inner_text() == "Review <role> / evidence required"
    assert page.locator("#detail-description").inner_text() == "<img src=x onerror=window.__fixtureXss=1>"
    assert page.locator("#detail-evidence a").count() == 0
    assert page.locator("#detail-evidence").inner_text().find("<b>Untrusted source quote</b>") >= 0
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth") is True
    assert page.evaluate("window.__fixtureXss === undefined") is True
    assert not [item for item in fixture.requests if item["path"] == "/api/jobs/fixture-review" and item["method"] != "GET"]
