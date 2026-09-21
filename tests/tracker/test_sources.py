from __future__ import annotations

import threading
import time

from app.tracker import sources
from app.tracker.contracts import Listing, SourceResult


GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 101,
            "title": "Software Engineer Intern - Summer 2027",
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/101",
            "location": {"name": "London, United Kingdom"},
            "updated_at": "2026-09-11T10:00:00Z",
            "employment_type": "Full-time",
        },
        {
            "id": 102,
            "title": "2027 Graduate Programme",
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/102",
            "location": {"name": "London"},
        },
    ]
}


RECRUITEE_PAYLOAD = {
    "offers": [
        {
            "id": 2686960,
            "title": "Silverpeak Industrial Placement 2027",
            "company_name": "Silverpeak",
            "careers_url": "https://silverpeakllp.recruitee.com/o/silverpeak-industrial-placement-2027",
            "careers_apply_url": "https://silverpeakllp.recruitee.com/o/silverpeak-industrial-placement-2027/c/new",
            "location": "London, Greater London, United Kingdom",
            "published_at": "2026-09-10 14:51:57 UTC",
            "created_at": "2026-07-23 09:44:28 UTC",
            "updated_at": "2026-09-10 14:51:57 UTC",
            "status": "published",
            "employment_type": "fulltime_fixed_term",
        }
    ]
}


PINPOINT_PAYLOAD = {
    "data": [
        {
            "id": "378239ee-5b71-4886-af47-236693e29acf",
            "title": "Deal Advisory - Placement Student 2027",
            "url": "https://menzies.pinpointhq.com/en/postings/378239ee-5b71-4886-af47-236693e29acf",
            "employment_type": "full_time",
            "employment_type_text": "Full Time",
            "location": {"city": "London", "name": "London Office", "province": "UK"},
            "job": {
                "division": {"name": "Early Careers"},
                "department": {"name": "Specialist Advisory"},
            },
        },
        {
            "id": "graduate-1",
            "title": "Graduate Programme 2027",
            "url": "https://menzies.pinpointhq.com/en/postings/graduate-1",
            "location": {"city": "London"},
            "job": {"division": {"name": "Early Careers"}},
        },
    ]
}


SIMPLYTK_HTML = """
<html><body>
<p>Page 1 of 2 · 549 openings</p>
<table class="desktop"><thead><tr>
  <th>Company</th><th>Role</th><th>Programme</th><th>Location</th>
  <th>Deadline</th><th>Posted</th><th>Apply</th>
</tr></thead><tbody>
<tr><td><a href="/companies/silverpeak">Silverpeak</a><span>Investment banking</span></td>
<td><span class="truncate">Industrial Placement 2027</span><span>Investment Banking</span></td>
<td title="Industrial placement"><span>Industrial placement</span></td><td><span>London</span></td>
<td>15 Oct</td><td><span>18 hours ago</span></td>
<td><a href="https://silverpeakllp.recruitee.com/o/silverpeak-industrial-placement-2027">Apply</a></td></tr>
<tr><td><a href="/companies/noise">Noise Co</a></td>
<td><span class="truncate">Graduate Scheme 2027</span></td><td title="Graduate"><span>Graduate</span></td>
<td><span>London</span></td><td>1 Jan 2027</td><td>1 day ago</td>
<td><a href="https://example.test/graduate">Apply</a></td></tr>
</tbody></table>
<table class="mobile"><thead><tr>
  <th>Company</th><th>Role</th><th>Programme</th><th>Location</th>
  <th>Deadline</th><th>Posted</th><th>Apply</th>
</tr></thead><tbody>
<tr><td><a href="/companies/silverpeak">Silverpeak</a></td>
<td><span class="truncate">Industrial Placement 2027</span></td><td>Industrial placement</td><td>London</td>
<td>15 Oct</td><td>18 hours ago</td><td><a href="https://silverpeakllp.recruitee.com/o/silverpeak-industrial-placement-2027">Apply</a></td></tr>
</tbody></table>
</body></html>
"""


def test_greenhouse_parser_keeps_full_time_internship_and_never_uses_updated_at():
    rows = sources.parse_greenhouse_payload(GREENHOUSE_PAYLOAD, "acme")

    assert len(rows) == 1
    assert rows[0].title == "Software Engineer Intern - Summer 2027"
    assert rows[0].employer == "Acme"
    assert rows[0].programme == "summer"
    assert rows[0].posted_at is None


def test_greenhouse_source_distinguishes_valid_empty_from_schema_error(monkeypatch):
    monkeypatch.setattr(sources, "http_get_json", lambda url: {"jobs": []})
    empty = sources.greenhouse_source("acme")
    assert empty.status == "empty"
    assert empty.listings == []

    monkeypatch.setattr(sources, "http_get_json", lambda url: {"unexpected": []})
    broken = sources.greenhouse_source("acme")
    assert broken.status == "error"
    assert "jobs" in broken.error


def test_recruitee_source_uses_publication_timestamp_and_apply_url(monkeypatch):
    monkeypatch.setattr(sources, "http_get_json", lambda url: RECRUITEE_PAYLOAD)

    result = sources.recruitee_silverpeak_source()

    assert result.status == "ok"
    assert len(result.listings) == 1
    listing = result.listings[0]
    assert listing.employer == "Silverpeak"
    assert listing.url.endswith("/c/new")
    assert listing.programme == "year_in_industry"
    assert listing.posted_at == "2026-09-10T14:51:57+00:00"


def test_pinpoint_source_accepts_menzies_placement_student_even_when_full_time(monkeypatch):
    monkeypatch.setattr(sources, "http_get_json", lambda url: PINPOINT_PAYLOAD)

    result = sources.pinpoint_menzies_source()

    assert result.status == "ok"
    assert [listing.title for listing in result.listings] == [
        "Deal Advisory - Placement Student 2027"
    ]
    assert result.listings[0].programme == "year_in_industry"
    assert result.listings[0].location == "London"


def test_simplytk_deduplicates_desktop_mobile_and_reports_incomplete_page(monkeypatch):
    monkeypatch.setattr(sources, "http_get_text", lambda url: SIMPLYTK_HTML)

    result = sources.simplytk_source()

    assert result.status == "partial"
    assert len(result.listings) == 1
    listing = result.listings[0]
    assert listing.employer == "Silverpeak"
    assert listing.title == "Industrial Placement 2027"
    assert listing.programme == "year_in_industry"
    assert listing.location == "London"
    assert listing.deadline is None  # no year was displayed; do not guess one
    assert listing.url.endswith("/silverpeak-industrial-placement-2027")
    assert "549" in result.error


def test_simplytk_html_without_parseable_rows_is_error(monkeypatch):
    monkeypatch.setattr(sources, "http_get_text", lambda url: "<html><body>blocked</body></html>")

    result = sources.simplytk_source()

    assert result.status == "error"
    assert result.listings == []
    assert "no parseable" in result.error.lower()


def test_collect_sources_isolates_errors_and_never_converts_them_to_empty(monkeypatch):
    def broken():
        raise RuntimeError("fixture HTTP failure")

    def good():
        return SourceResult(
            name="good",
            url="https://example.test/jobs",
            status="ok",
            listings=[Listing("Example", "Summer Internship", "https://example.test/1")],
        )

    monkeypatch.setattr(
        sources,
        "SOURCE_SPECS",
        (
            sources.SourceSpec("broken", "https://example.test/broken", broken),
            sources.SourceSpec("good", "https://example.test/good", good),
        ),
    )

    results = sources.collect_sources()

    assert [result.status for result in results] == ["error", "ok"]
    assert "fixture HTTP failure" in results[0].error
    assert results[0].listings == []


def test_collect_sources_never_runs_more_than_four_sources_at_once(monkeypatch):
    active = 0
    peak = 0
    lock = threading.Lock()

    def observed():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return SourceResult("observed", "https://example.test", "empty")

    specs = tuple(
        sources.SourceSpec(str(index), f"https://example.test/{index}", observed)
        for index in range(9)
    )
    monkeypatch.setattr(sources, "SOURCE_SPECS", specs)

    results = sources.collect_sources()

    assert len(results) == 9
    assert peak <= 4
