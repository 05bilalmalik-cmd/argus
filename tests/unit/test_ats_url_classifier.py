from __future__ import annotations

import pytest

from app.scouting import trackr_live
from app.scouting.sources.aggregators import parse_listing_html
from app.scouting.sources.base import _ats_from_url
from app.scouting.trackr import (
    _ats_from_url as trackr_ats_from_url,
    parse_trackr_html,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://job-boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://job-boards.eu.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://lever.co/acme/jobs/abc", "lever"),
        ("https://jobs.lever.co/acme/abc-def", "lever"),
        ("https://hire.lever.co/acme/abc-def", "lever"),
        (
            "https://travelers.wd5.myworkdayjobs.com/en-US/External/job/London/Intern_R123",
            "workday",
        ),
        (
            "https://acme.wd103.myworkdaysite.com/recruiting/acme/External/job/Analyst_R7",
            "workday",
        ),
        (
            "https://jobs.smartrecruiters.com/Wiser/744000143765020-summer-intern",
            "smartrecruiters",
        ),
        ("https://apply.workable.com/acme/j/ABC123/", "workable"),
        ("https://jobs.ashbyhq.com/acme/12345678", "ashby"),
        ("https://careers-acme.icims.com/jobs/1234/intern/job", "icims"),
        (
            "https://acme.taleo.net/careersection/ex/jobdetail.ftl?job=1234",
            "taleo",
        ),
        (
            "https://career5.successfactors.com/career?company=acme&career_job_req_id=123",
            "successfactors",
        ),
        (
            "https://career2.successfactors.eu/career?company=acme&career_job_req_id=456",
            "successfactors",
        ),
        (
            "https://acme.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/123",
            "oracle",
        ),
        ("https://blackrock.tal.net/vx/lang-en-GB/mobile-0/appcentre-1/brand-3/job-123", "talnet"),
        ("https://acme.eightfold.ai/careers/job/123", "eightfold"),
        ("https://acme.teamtailor.com/jobs/123-summer-intern", "teamtailor"),
        ("https://acme.recruitee.com/o/summer-intern", "recruitee"),
        ("https://acme.jobs.personio.de/job/123456", "personio"),
    ],
)
def test_ats_classifier_recognises_provider_hosts(url: str, expected: str) -> None:
    assert _ats_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://notgreenhouse.io/jobs/123",
        "https://greenhouse.io.evil.com/jobs/123",
        "https://careers.example.com/?ref=greenhouse",
        "https://careers.example.com/greenhouse/jobs/123",
        "https://higher.gs.com/campus?type=internships",
        "https://gic.careers/jobs/summer-internship",
    ],
)
def test_ats_classifier_does_not_guess_from_untrusted_substrings(url: str) -> None:
    assert _ats_from_url(url) == "unknown"


def test_greenhouse_shortlink_requires_resolution() -> None:
    classification = _ats_from_url("https://grnh.se/twyh9job1us")

    assert classification == "greenhouse_shortlink"
    assert classification != "greenhouse"


def test_trackr_compatibility_export_is_the_canonical_classifier() -> None:
    assert trackr_ats_from_url is _ats_from_url


def test_ats_classifier_is_stable_across_repeated_calls() -> None:
    url = "https://jobs.ashbyhq.com/acme/12345678"
    first = _ats_from_url(url)

    assert first == "ashby"
    assert [_ats_from_url(url) for _ in range(3)] == [first, first, first]


def test_trackr_anchor_filter_uses_canonical_provider_boundaries() -> None:
    html = """
    <section><a href="https://notgreenhouse.io/jobs/123">Fake Summer Internship</a></section>
    <section><a href="https://apply.workable.com/acme/j/ABC123/">Real Summer Internship</a></section>
    """

    rows = parse_trackr_html(html)

    assert [(row.url, row.ats_type) for row in rows] == [
        ("https://apply.workable.com/acme/j/ABC123/", "workable")
    ]


def test_aggregator_anchor_filter_uses_canonical_provider_boundaries() -> None:
    html = """
    <section><a href="https://notgreenhouse.io/jobs/123">Fake Summer Internship</a></section>
    <section><a href="https://jobs.ashbyhq.com/acme/12345678">Real Summer Internship</a></section>
    """

    rows = parse_listing_html(html, source_label="test")

    assert [(row.url, row.ats_type) for row in rows] == [
        ("https://jobs.ashbyhq.com/acme/12345678", "ashby")
    ]


def test_trackr_live_populates_ats_type_from_scraped_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_scrape_via_browser() -> list[tuple[str, object]]:
        return [
            (
                "summer-internships",
                {
                    "programmes": [
                        {
                            "company": {"name": "Wiser"},
                            "name": "Summer Internship",
                            "url": (
                                "https://jobs.smartrecruiters.com/Wiser/"
                                "744000143765020-summer-intern"
                            ),
                            "openingDate": "2026-08-01T00:00:00Z",
                            "closingDate": "2026-10-01T00:00:00Z",
                        }
                    ]
                },
            )
        ]

    monkeypatch.setattr(trackr_live, "_scrape_via_browser", fake_scrape_via_browser)

    rows = trackr_live.fetch_programmes()

    assert len(rows) == 1
    assert rows[0].ats_type == "smartrecruiters"
