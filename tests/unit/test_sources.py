"""Offline unit tests for app/scouting/sources (no network).

Covers: Greenhouse/Lever payload parsing, aggregator HTML parsers (JSON-LD +
anchor fallback), early-careers filtering, URL dedup, source labeling,
malformed-input tolerance, watchlist merging, and collect_all source
isolation. One integration smoke test runs only when ARGUS_NET_TESTS is set.
"""
from __future__ import annotations

import json

import pytest

from app.scouting.programmes import ProgrammeType
from app.scouting.sources import aggregators, greenhouse, lever, watchlist
from app.scouting.sources.base import (
    collect_all,
    dedupe,
    is_early_careers,
    normalize_url,
)
from app.scouting.trackr import ScrapedOpportunity

# --- Fixtures ---------------------------------------------------------------

GREENHOUSE_PAYLOAD = json.dumps(
    {
        "meta": {},
        "jobs": [
            {
                "title": "2026 Spring Week - Technology",
                "absolute_url": "https://boards.greenhouse.io/citadel/jobs/1001",
                "location": {"name": "London, UK"},
                "updated_at": "2026-01-05T10:00:00-04:00",
            },
            {
                "title": "Software Engineer, Core Infrastructure",
                "absolute_url": "https://boards.greenhouse.io/citadel/jobs/1002",
                "location": {"name": "New York"},
                "updated_at": "2026-01-06T10:00:00-04:00",
            },
            {"title": "", "absolute_url": "https://boards.greenhouse.io/citadel/jobs/1003"},
            {"title": "Internship", "absolute_url": "not-a-url"},
        ],
    }
)

LEVER_PAYLOAD = json.dumps(
    [
        {
            "text": "Off-Cycle Internship - Quant Research",
            "hostedUrl": "https://jobs.lever.co/squarepoint/aaa",
            "applyUrl": "https://jobs.lever.co/squarepoint/aaa/apply?lever-source=api",
            "categories": {"location": "London", "commitment": "Internship"},
            "createdAt": 1700000000000,
        },
        {
            "text": "Backend Engineer",
            "hostedUrl": "https://jobs.lever.co/squarepoint/bbb",
            "categories": {"location": "Paris", "commitment": "Full-time"},
        },
        {
            # no commitment -> kept here, title filter applies later
            "text": "Summer Internship",
            "hostedUrl": "https://jobs.lever.co/squarepoint/ccc",
            "categories": {"location": None},
        },
        {"text": "Broken", "hostedUrl": "javascript:void(0)"},
    ]
)

JSONLD_HTML = """<html><head>
<script type="application/ld+json">
{"@context":"http://schema.org","@type":"JobPosting","title":"Spring Insight Programme",
 "hiringOrganization":{"name":"Bright Example Bank"},
 "url":"https://www.brightnetwork.co.uk/apply/spring-insight",
 "jobLocation":{"address":{"addressLocality":"London"}},
 "validThrough":"2026-03-01"}
</script></head><body>listing page</body></html>"""

JSONLD_GRAPH_HTML = """<html><head>
<script type="application/ld+json">
{"@graph":[{"@type":["Organization"],"name":"x"},
           {"@type":["JobPosting"],"title":"Industrial Placement 2026",
            "hiringOrganization":{"name":"WikiCo"},"url":"https://www.wikijob.co.uk/p/1"}]}
</script></head><body></body></html>"""

ANCHOR_HTML = """<html><body>
<div class="card">
  <a href="https://boards.greenhouse.io/blackstone/jobs/55">2026 Summer Internship</a>
</div>
<div class="card">
  <div><span>Goldman Sachs</span>
    <a href="/out?u=https%3A%2F%2Fjobs.lever.co%2Fgoldman%2F9">Spring Week</a></div>
</div>
<div class="card">
  <a href="https://job-boards.greenhouse.io/gresearch/jobs/7">Graduate Scheme in Engineering</a>
</div>
<div class="card">
  <a href="/relative/path/not-ats">Some Other Link</a>
</div>
</body></html>"""


def _sources(*rows: ScrapedOpportunity):
    return [("fake", lambda rows=list(rows): list(rows))]


# --- Greenhouse -------------------------------------------------------------


class TestGreenhouse:
    def test_parse_payload_extracts_rows(self):
        rows = greenhouse.parse_payload(GREENHOUSE_PAYLOAD, "citadel")
        assert len(rows) == 2  # empty-title and non-http rows dropped
        spring = rows[0]
        assert spring.employer == "citadel"
        assert spring.role_title == "2026 Spring Week - Technology"
        assert spring.location == "London, UK"
        assert spring.url == "https://boards.greenhouse.io/citadel/jobs/1001"
        assert spring.source_url == spring.url
        assert spring.application_url == spring.url
        assert spring.source == "greenhouse:citadel"

    @pytest.mark.parametrize("bad", ["", "   ", '{"truncated', "[not json", "null", "42", None])
    def test_malformed_payloads_return_empty(self, bad):
        assert greenhouse.parse_payload(bad, "citadel") == []

    def test_jobs_not_a_list(self):
        assert greenhouse.parse_payload({"jobs": {"nope": 1}}, "citadel") == []
        assert greenhouse.parse_payload({"jobs": [None, 3, "x"]}, "citadel") == []

    def test_fetch_org_labels_and_filters(self, monkeypatch):
        monkeypatch.setattr(greenhouse, "http_get_json", lambda url: json.loads(GREENHOUSE_PAYLOAD))
        rows = greenhouse.fetch_org("citadel")
        # Software Engineer row is filtered out by the early-careers gate.
        assert all(r.source == "greenhouse:citadel" for r in rows)
        assert {r.role_title for r in rows} == {"2026 Spring Week - Technology"}

    def test_fetch_org_builds_expected_url(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(greenhouse, "http_get_json", lambda url: seen.setdefault("url", url) or {"jobs": []})
        greenhouse.fetch_org("drw")
        assert seen["url"] == (
            "https://boards-api.greenhouse.io/v1/boards/drw/jobs"
        )


# --- Lever ------------------------------------------------------------------


class TestLever:
    def test_parse_postings_filters_commitment(self):
        rows = lever.parse_postings(LEVER_PAYLOAD, "squarepoint")
        titles = [r.role_title for r in rows]
        assert titles == ["Off-Cycle Internship - Quant Research", "Summer Internship"]
        assert rows[0].location == "London"
        assert rows[0].source_url == "https://jobs.lever.co/squarepoint/aaa"
        assert rows[0].application_url == (
            "https://jobs.lever.co/squarepoint/aaa/apply?lever-source=api"
        )
        assert rows[0].ats_type == "lever"
        assert rows[0].source == "lever:squarepoint"
        assert rows[0].programme_type is ProgrammeType.SUMMER

    @pytest.mark.parametrize("bad", ["", "{[", "[]", "{}", "null", None])
    def test_malformed_postings_return_empty(self, bad):
        assert lever.parse_postings(bad, "squarepoint") == []

    def test_fetch_company_labels_and_filters(self, monkeypatch):
        payload = [
            {"text": "Spring Week", "hostedUrl": "https://jobs.lever.co/acme/1",
             "categories": {"commitment": "Internship"}},
            {"text": "Quant Analyst", "hostedUrl": "https://jobs.lever.co/acme/2",
             "categories": {"commitment": "Full-time"}},
        ]
        monkeypatch.setattr(lever, "http_get_json", lambda url: payload)
        rows = lever.fetch_company("acme")
        assert [r.source for r in rows] == ["lever:acme"]
        assert [r.role_title for r in rows] == ["Spring Week"]


# --- Aggregators ------------------------------------------------------------


class TestAggregators:
    def test_json_ld_primary_path(self):
        rows = aggregators.parse_brightnetwork_html(JSONLD_HTML)
        assert len(rows) == 1
        row = rows[0]
        assert row.source == "brightnetwork"
        assert row.source_url == "https://www.brightnetwork.co.uk/apply/spring-insight"
        assert row.application_url is None
        assert row.employer == "Bright Example Bank"
        assert row.role_title == "Spring Insight Programme"
        assert row.location == "London"
        assert row.deadline is not None and row.deadline.year == 2026
        assert row.programme_type is ProgrammeType.SPRING_WEEK

    def test_json_ld_graph_and_list_type(self):
        rows = aggregators.parse_wikijob_html(JSONLD_GRAPH_HTML)
        assert len(rows) == 1
        assert rows[0].role_title == "Industrial Placement 2026"
        assert rows[0].programme_type is ProgrammeType.YEAR_IN_INDUSTRY
        assert rows[0].source == "wikijob"

    def test_anchor_fallback(self):
        rows = aggregators.parse_brightnetwork_html(ANCHOR_HTML)
        urls = {r.url for r in rows}
        assert "https://boards.greenhouse.io/blackstone/jobs/55" in urls
        # relative redirector resolved against base_url; ATS host detected inside
        assert any(r.ats_type == "lever" for r in rows)
        # graduate scheme anchor dropped; non-ATS anchor ignored
        assert all("Graduate Scheme" not in r.role_title for r in rows)
        assert len(rows) == 2
        employers = {r.employer for r in rows}
        assert "Blackstone" in employers or "Unknown" in employers

    def test_anchor_fallback_keeps_shared_url_roles_and_collapses_exact_duplicates(self):
        shared_url = "https://jobs.lever.co/acme/shared"
        html = f"""
        <main>
          <article><a href="{shared_url}">Summer Internship</a></article>
          <article><a href="{shared_url}">Spring Insight</a></article>
          <article><a href="{shared_url}">Summer Internship</a></article>
        </main>
        """

        rows = aggregators.parse_listing_html(html, source_label="brightnetwork")

        assert [row.role_title for row in rows] == [
            "Summer Internship",
            "Spring Insight",
        ]

    def test_ratemyplacement_label(self):
        rows = aggregators.parse_ratemyplacement_html(JSONLD_HTML)
        assert rows and rows[0].source == "ratemyplacement"

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_empty_html_returns_nothing(self, bad):
        assert aggregators.parse_brightnetwork_html(bad) == []
        assert aggregators.parse_wikijob_html(bad) == []

    def test_broken_jsonld_falls_back_to_anchors(self):
        html = ('<script type="application/ld+json">{"@type":"JobPosting"</script>'
                '<a href="https://jobs.lever.co/acme/9">Winter Internship</a>')
        rows = aggregators.parse_listing_html(html, source_label="wikijob")
        assert len(rows) == 1 and rows[0].source == "wikijob"

    def test_page_urls_are_module_constants(self):
        assert aggregators.BRIGHTNETWORK_URLS
        assert aggregators.RATEMYPLACEMENT_URLS
        assert aggregators.WIKIJOB_URLS
        assert all(u.startswith("https://www.ratemyplacement.co.uk")
                   for u in aggregators.RATEMYPLACEMENT_URLS)

    def test_fetch_pages_survives_dead_page(self, monkeypatch):
        def boom(url):
            raise RuntimeError("network down")

        monkeypatch.setattr(aggregators, "http_get", boom)
        assert aggregators.fetch_brightnetwork() == []


# --- Early-careers filter / dedup / normalization ----------------------------


class TestFilterAndDedup:
    @pytest.mark.parametrize("title", [
        "Spring Week 2026", "Technology Internship", "Summer Analyst",
        "Industrial Placement", "Off-Cycle Internship", "First Year Insight",
        "Placement Year", "Year in Industry", "2026 Intern - Quant",
    ])
    def test_keeps_early_careers(self, title):
        assert is_early_careers(title)

    @pytest.mark.parametrize("title", [
        "Graduate Scheme", "Software Engineer (Full-time)", "Experienced Hire - Risk",
        "Senior Developer", "PhD Position", "Apprenticeship Programme", "",
        "Quantitative Analyst",
    ])
    def test_drops_non_early_careers(self, title):
        assert not is_early_careers(title)

    def test_normalize_url(self):
        assert normalize_url("HTTPS://Boards.Example.com/x/") == "https://boards.example.com/x"
        assert normalize_url("https://a.com/p#frag") == "https://a.com/p"
        assert normalize_url("relative/path") == "relative/path"
        assert normalize_url(None) == ""

    def test_dedupe_keeps_distinct_roles_that_share_a_source_url(self):
        a = ScrapedOpportunity(employer="X", role_title="Internship A", url="https://e.com/job/1/")
        b = ScrapedOpportunity(employer="Y", role_title="Internship B", url="https://E.com/job/1")
        c = ScrapedOpportunity(employer="Z", role_title="Internship C", url="https://e.com/job/2")
        merged = dedupe([a, b, c])
        assert [m.employer for m in merged] == ["X", "Y", "Z"]

    def test_dedupe_uses_exact_identity_when_all_fingerprints_collide(
        self,
        monkeypatch,
    ):
        class _ForcedDigest:
            def hexdigest(self):
                return "forced-source-collision"

        monkeypatch.setattr(
            "app.domain.targets.hashlib.sha256",
            lambda _material: _ForcedDigest(),
        )
        first = ScrapedOpportunity(
            employer="Collision Capital",
            role_title="Summer Internship",
            url="https://careers.example.test/shared/",
            location="London",
            division="Credit",
        )
        distinct = ScrapedOpportunity(
            employer="Collision Capital",
            role_title="Spring Insight",
            url="https://careers.example.test/shared",
            location="London",
            division="Credit",
        )
        exact_duplicate = ScrapedOpportunity(
            employer=" collision capital ",
            role_title="summer internship",
            url="https://careers.example.test/shared",
            location=" london ",
            division="credit",
        )

        merged = dedupe([first, distinct, exact_duplicate])

        assert [row.role_title for row in merged] == [
            "Summer Internship",
            "Spring Insight",
        ]


# --- collect_all orchestration ----------------------------------------------


class TestCollectAll:
    def test_isolates_failing_sources_and_merges(self):
        def broken():
            raise RuntimeError("boom")

        good = ScrapedOpportunity(
            employer="Citadel", role_title="Spring Week",
            url="https://boards.greenhouse.io/citadel/jobs/1", source="greenhouse:citadel",
        )
        dup = ScrapedOpportunity(
            employer="Other", role_title="Summer Internship",
            url="https://boards.greenhouse.io/citadel/jobs/1/", source="ratemyplacement",
        )
        noise = ScrapedOpportunity(
            employer="BigCo", role_title="Managing Director",
            url="https://bigco.example/jobs/99", source="lever:bigco",
        )
        merged = collect_all(sources=[("broken", broken), ("good", _sources(good)[0][1]),
                                      ("dup", _sources(dup)[0][1]), ("noise", _sources(noise)[0][1])])
        assert [m.role_title for m in merged] == ["Spring Week", "Summer Internship"]
        assert [m.source for m in merged] == ["greenhouse:citadel", "ratemyplacement"]

    def test_none_return_from_source_tolerated(self):
        merged = collect_all(sources=[("nothing", lambda: None)])
        assert merged == []


# --- Watchlist ---------------------------------------------------------------


class TestWatchlist:
    def test_builtin_shape_and_size(self):
        assert len(watchlist.WATCHLIST) >= 5
        for firm, spec in watchlist.WATCHLIST.items():
            assert set(spec) == {"greenhouse", "lever"}, firm
            assert all(isinstance(s, str) and s for s in spec["greenhouse"] + spec["lever"]), firm

    def test_goldman_absent_by_design(self):
        # Not on public Greenhouse — must not be guessed into the watchlist.
        gs = [f for f in watchlist.WATCHLIST if "goldman" in f.casefold()]
        assert not gs or not any(watchlist.WATCHLIST[f]["greenhouse"] or watchlist.WATCHLIST[f]["lever"]
                                 for f in gs)

    def test_env_override_merges_extra_firms(self, tmp_path):
        override = tmp_path / "extra.json"
        override.write_text(json.dumps({
            "Extra Capital": {"greenhouse": ["extracapital"], "lever": []},
            "Citadel": {"lever": ["citadel-lever-mirror"]},
        }), encoding="utf-8")
        merged = watchlist.load_watchlist({"ARGUS_WATCHLIST_JSON": str(override)})
        assert merged["Extra Capital"]["greenhouse"] == ["extracapital"]
        assert "citadel-lever-mirror" in merged["Citadel"]["lever"]
        # builtin untouched (deep copy semantics)
        assert "Citadel" not in watchlist.WATCHLIST  # citadel not builtin (own ATS)

    def test_env_var_fallback(self, tmp_path, monkeypatch):
        override = tmp_path / "env.json"
        override.write_text('{"Env Firm": {"greenhouse": ["envfirm"], "lever": []}}', encoding="utf-8")
        monkeypatch.setenv("ARGUS_WATCHLIST_JSON", str(override))
        assert "Env Firm" in watchlist.load_watchlist()

    def test_bad_override_file_tolerated(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        merged = watchlist.load_watchlist({"ARGUS_WATCHLIST_JSON": str(bad)})
        assert merged == watchlist.WATCHLIST

    def test_slug_helpers(self):
        orgs = sorted(
            {
                slug
                for spec in watchlist.WATCHLIST.values()
                for slug in spec["greenhouse"]
            }
        )
        assert "point72" in orgs and orgs == sorted(set(orgs))


# --- Integration smoke (opt-in via env var; NO run in CI/offline) -----------


@pytest.mark.skipif(
    not __import__("os").environ.get("ARGUS_NET_TESTS"),
    reason="set ARGUS_NET_TESTS=1 to run live-network smoke tests",
)
class TestLiveSmoke:
    def test_greenhouse_live_fetch(self):
        rows = greenhouse.fetch_org("citadel")
        assert isinstance(rows, list) and rows, "expected live postings from citadel board"
        first = rows[0]
        assert first.url.startswith("http") and first.source == "greenhouse:citadel"
