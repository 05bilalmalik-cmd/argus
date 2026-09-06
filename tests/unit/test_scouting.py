"""Tests for the scouting module: programme classification, Trackr HTML
parsing, ingestion, and autopilot priority/fallback logic."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.models import Application, ConflictRule, Opportunity
from app.scouting.programmes import (
    ProgrammeType,
    classify_programme,
    should_auto_apply,
)
from app.scouting.service import ScoutService, _trusted_provider_hint
from app.scouting.trackr import ScrapedOpportunity, parse_trackr_html, scrape_folder
from app.scouting import trackr_live


def setup(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox_from_path(settings.secret_key_path)
    return settings, db, crypto


def CryptoBox_from_path(key_path):  # local alias keeps imports at top tidy
    from app.security.crypto import CryptoBox

    return CryptoBox.from_path(key_path)


# ----------------------------------------------------------------- classifier


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Year in Industry Placement - Investment Banking", ProgrammeType.YEAR_IN_INDUSTRY),
        ("Industrial Placement - Risk", ProgrammeType.YEAR_IN_INDUSTRY),
        ("Placement Year - Technology", ProgrammeType.YEAR_IN_INDUSTRY),
        ("2028 Spring Week - Global Markets", ProgrammeType.SPRING_WEEK),
        ("Spring Insight Programme", ProgrammeType.SPRING_WEEK),
        ("Summer Analyst - Investment Banking Division", ProgrammeType.SUMMER),
        ("Summer Internship - Technology", ProgrammeType.SUMMER),
        ("Off-cycle Internship", ProgrammeType.SUMMER),
        ("Graduate Scheme - Software Engineering", ProgrammeType.OTHER),
        ("Graduate Programme - Quant", ProgrammeType.OTHER),
        ("Experienced Hire Role", ProgrammeType.OTHER),
    ],
)
def test_classify_programme(text: str, expected: ProgrammeType) -> None:
    assert classify_programme(text) is expected


def test_auto_apply_gate() -> None:
    assert should_auto_apply("year_in_industry")
    assert should_auto_apply("spring_week")
    assert should_auto_apply("summer")
    assert not should_auto_apply("other")


def test_european_greenhouse_application_url_gets_a_trusted_provider_hint() -> None:
    url = "https://job-boards.eu.greenhouse.io/isam/jobs/4949792101"

    assert _trusted_provider_hint(url) == ("greenhouse", url)


# -------------------------------------------------------------------- parser


TRACKR_HTML = """
<html><body>
<div class="job-card">
  <a href="https://boards.greenhouse.io/goldmansachs/jobs/123">
    Year in Industry Placement - Investment Banking 2026
  </a>
  <span>Goldman Sachs</span><span>London</span>
</div>
<div class="job-card">
  <a href="https://jobs.lever.co/jpmorgan/abc-123">
    2027 Spring Week - Global Markets
  </a>
  <span>J.P. Morgan</span>
</div>
<div class="job-card excluded">
  <a href="https://boards.greenhouse.io/somefirm/jobs/999">Graduate Scheme - Tech</a>
</div>
<a href="https://example.com/not-an-ats">Summer Internship</a>
</body></html>
"""


def test_parse_trackr_html_finds_ats_links() -> None:
    results = parse_trackr_html(TRACKR_HTML)
    urls = {r.url for r in results}
    assert "https://boards.greenhouse.io/goldmansachs/jobs/123" in urls
    assert "https://jobs.lever.co/jpmorgan/abc-123" in urls
    greenhouse = next(r for r in results if "greenhouse" in r.url)
    lever = next(r for r in results if "lever" in r.url)
    assert greenhouse.programme_type is ProgrammeType.YEAR_IN_INDUSTRY
    assert greenhouse.employer == "Goldman Sachs"
    assert greenhouse.ats_type == "greenhouse"
    assert lever.programme_type is ProgrammeType.SPRING_WEEK
    assert not any("example.com" in u for u in urls)


def test_parse_trackr_html_ldjson() -> None:
    html = """
    <html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting",
     "title":"Summer Analyst - Markets","url":"https://job-boards.greenhouse.io/ubs/jobs/77",
     "hiringOrganization":{"name":"UBS"},"validThrough":"2026-11-30"}
    </script></head><body></body></html>
    """
    results = parse_trackr_html(html)
    assert len(results) == 1
    assert results[0].employer == "UBS"
    assert results[0].deadline == date(2026, 11, 30)
    assert results[0].ats_type == "greenhouse"


def test_trackr_parser_keeps_distinct_roles_that_share_one_source_url() -> None:
    html = """
    <script type="application/ld+json">
    {"@graph":[
      {"@type":"JobPosting","title":"Summer Analyst","url":"https://jobs.lever.co/acme/shared","hiringOrganization":{"name":"Acme"}},
      {"@type":"JobPosting","title":"Spring Insight","url":"https://jobs.lever.co/acme/shared","hiringOrganization":{"name":"Acme"}}
    ]}
    </script>
    """

    results = parse_trackr_html(html)

    assert {item.role_title for item in results} == {
        "Summer Analyst",
        "Spring Insight",
    }


def test_scraped_opportunity_preserves_legacy_positional_constructor() -> None:
    deadline = date(2027, 1, 31)

    row = ScrapedOpportunity(
        "Acme",
        "Summer Analyst",
        "https://jobs.lever.co/acme/req",
        "London",
        deadline,
        "lever",
        ProgrammeType.SUMMER,
        "Private Credit",
        "legacy-positional",
    )

    assert row.source_url == "https://jobs.lever.co/acme/req"
    assert row.location == "London"
    assert row.deadline == deadline
    assert row.ats_type == "lever"
    assert row.programme_type is ProgrammeType.SUMMER
    assert row.division == "Private Credit"
    assert row.source == "legacy-positional"
    assert row.application_url is None


def test_trackr_folder_and_ingestion_keep_shared_url_roles(tmp_path: Path) -> None:
    trackr_dir = tmp_path / "trackr"
    trackr_dir.mkdir()
    shared_url = "https://jobs.lever.co/acme/shared"
    summer_html = """
    <script type="application/ld+json">
    {"@type":"JobPosting","title":"Summer Analyst","url":"SHARED",
     "hiringOrganization":{"name":"Acme"}}
    </script>
    """.replace("SHARED", shared_url)
    spring_html = """
    <script type="application/ld+json">
    {"@type":"JobPosting","title":"Spring Insight","url":"SHARED",
     "hiringOrganization":{"name":"Acme"}}
    </script>
    """.replace("SHARED", shared_url)
    (trackr_dir / "summer.html").write_text(
        summer_html,
        encoding="utf-8",
    )
    (trackr_dir / "spring.html").write_text(
        spring_html,
        encoding="utf-8",
    )
    rows = scrape_folder(trackr_dir)
    settings, db, crypto = setup(tmp_path / "data")

    with db.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            rows, default_cycle="2027"
        )
        roles = {item.role_title for item in session.query(Opportunity).all()}

    assert len(rows) == 2
    assert report["imported"] == 2
    assert roles == {"Summer Analyst", "Spring Insight"}


# ------------------------------------------------------------------ ingestion


def _scraped(url: str, employer: str = "Goldman Sachs", role: str = "Year in Industry"):
    return ScrapedOpportunity(
        employer=employer,
        role_title=role,
        url=url,
        source="test",
    )


def _prepared_candidate(session, *, employer: str = "Prepared Bank"):
    opportunity = Opportunity(
        employer=employer,
        role_title="Summer Analyst",
        programme_group=ProgrammeType.SUMMER.value,
        cycle="2027",
        url=f"https://boards.greenhouse.io/{employer.casefold().replace(' ', '-')}/1",
        source="test",
        cv_required=False,
    )
    session.add(opportunity)
    session.flush()
    application = Application(
        opportunity_id=opportunity.id,
        state=ApplicationState.PACKAGE_PREPARED.value,
        priority=50,
    )
    session.add(application)
    session.flush()
    return application, opportunity


def _mark_ingested_targets_verified(session) -> None:
    """Scheduling tests operate on resolved fixtures, never raw source URLs."""

    for opportunity in session.query(Opportunity).all():
        opportunity.application_url = opportunity.url
        opportunity.target_status = TargetKind.APPLICATION_ENTRY.value
        opportunity.resolved_ats_type = "fixture"
    session.flush()


def test_ingest_dedupes_and_creates_applications(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        stats = scout.ingest(
            [
                _scraped("https://boards.greenhouse.io/gs/jobs/1"),
                _scraped("https://boards.greenhouse.io/gs/jobs/1"),  # duplicate URL
                _scraped(
                    "https://jobs.lever.co/ms/2", "Morgan Stanley", "Spring Week 2027"
                ),
                _scraped("https://x.example.com/grad", "SomeCo", "Graduate Scheme"),
            ]
        )
        assert stats["imported"] == 2
        assert stats["duplicates"] == 1
        assert stats["excluded"] == 1
        assert stats["applications"] == 2

        opportunities = session.query(Opportunity).all()
        groups = {o.programme_group for o in opportunities}
        assert groups == {"year_in_industry", "spring_week"}


@pytest.mark.parametrize(
    ("explicit_type", "role_title", "expected_group"),
    [
        (ProgrammeType.YEAR_IN_INDUSTRY, "Summer Analyst", "year_in_industry"),
        (ProgrammeType.OTHER, "Summer Analyst", "summer"),
        ("not-a-programme", "Spring Week", "spring_week"),
    ],
)
def test_ingest_uses_only_usable_explicit_programme_categories(
    tmp_path: Path,
    explicit_type: ProgrammeType | str,
    role_title: str,
    expected_group: str,
) -> None:
    """Usable source evidence wins; OTHER and invalid values defer to titles."""
    settings, db, crypto = setup(tmp_path)
    row = ScrapedOpportunity(
        employer="Category Bank",
        role_title=role_title,
        url="https://jobs.lever.co/category-bank/role",
        programme_type=explicit_type,
        source="test",
    )

    with db.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest([row], default_cycle="2027")
        opportunity = session.query(Opportunity).one()

    assert report["imported"] == 1
    assert opportunity.programme_group == expected_group


def test_ingest_refuses_out_of_scope_title_even_when_category_is_supported(
    tmp_path: Path,
) -> None:
    """A future source cannot hide a graduate scheme behind an internship label."""
    settings, db, crypto = setup(tmp_path)
    row = ScrapedOpportunity(
        employer="Priority Bank",
        role_title="Graduate Scheme",
        url="https://jobs.lever.co/priority-bank/role",
        programme_type=ProgrammeType.SPRING_WEEK,
        source="test",
    )

    with db.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest([row], default_cycle="2027")
        opportunity_count = session.query(Opportunity).count()

    assert report["imported"] == 0
    assert report["excluded"] == 1
    assert report["out_of_scope_programme"] == 1
    assert opportunity_count == 0


def test_ingest_counts_each_exclusion_and_failure_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropped rows expose distinct machine-readable reasons and aggregate exclusions."""
    settings, db, crypto = setup(tmp_path)

    def fail_add(self, opportunity, *, actor="user"):  # noqa: ANN001
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr("app.scouting.service.OpportunityService.add", fail_add)
    rows = [
        _scraped("https://jobs.lever.co/count-bank/unknown", role="Graduate Scheme"),
        _scraped("", role="Summer Analyst"),
        _scraped("ftp://jobs.lever.co/count-bank/invalid", role="Summer Analyst"),
        _scraped("https://jobs.lever.co/count-bank/failure", role="Summer Analyst"),
    ]

    with db.session_scope() as session:
        stats = ScoutService(session, settings, crypto).ingest(rows, default_cycle="2027")

    assert stats["excluded"] == 3
    assert stats["out_of_scope_programme"] == 1
    assert stats["unknown_programme"] == 0
    assert stats["missing_url"] == 1
    assert stats["invalid_url"] == 1
    assert stats["ingestion_failure"] == 1
    assert all(type(value) is int for value in stats.values())


def test_ingest_rolls_back_real_persistence_failure_and_continues(tmp_path: Path) -> None:
    """A bad row's failed SQLAlchemy flush cannot poison a later valid row."""
    settings, db, crypto = setup(tmp_path)
    bad_row = ScrapedOpportunity(
        employer="Broken Persistence Bank",
        role_title="Summer Analyst",
        url="https://jobs.lever.co/broken-persistence-bank/role",
        deadline="not-a-date",  # type: ignore[arg-type]
        source="test",
    )
    valid_row = _scraped(
        "https://jobs.lever.co/later-valid-bank/role",
        employer="Later Valid Bank",
        role="Spring Week",
    )

    with db.session_scope() as session:
        stats = ScoutService(session, settings, crypto).ingest(
            [bad_row, valid_row], default_cycle="2027"
        )
        opportunities = session.query(Opportunity).order_by(Opportunity.employer).all()

    assert stats["seen"] == 2
    assert stats["ingestion_failure"] == 1
    assert stats["imported"] == 1
    assert stats["applications"] == 1
    assert [opportunity.employer for opportunity in opportunities] == ["Later Valid Bank"]


def test_ingest_uses_title_classifier_when_input_has_no_programme_type(
    tmp_path: Path,
) -> None:
    """Sources without a category attribute fall back to the unchanged classifier."""
    settings, db, crypto = setup(tmp_path)
    row = SimpleNamespace(
        employer="Attribute-Free Bank",
        role_title="Spring Week",
        source_url="https://jobs.lever.co/attribute-free-bank/role",
        url="https://jobs.lever.co/attribute-free-bank/role",
        application_url=None,
        location="",
        deadline=None,
        ats_type="lever",
        division="",
        source="test",
    )

    assert not hasattr(row, "programme_type")

    with db.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest([row], default_cycle="2027")
        opportunity = session.query(Opportunity).one()

    assert report["imported"] == 1
    assert opportunity.programme_group == "spring_week"


def test_live_trackr_rows_retain_slug_provenance_when_ingested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live rows retain the authoritative tracker slug in the stored source."""
    async def fake_scrape_via_browser() -> list[tuple[str, object]]:
        return [
            (
                "spring-weeks",
                {
                    "programmes": [
                        {
                            "company": {"name": "Provenance Bank"},
                            "name": "Unclassifiable insight listing",
                            "url": "https://jobs.lever.co/provenance-bank/role",
                        }
                    ]
                },
            )
        ]

    monkeypatch.setattr(trackr_live, "_scrape_via_browser", fake_scrape_via_browser)
    rows = trackr_live.fetch_programmes()
    settings, db, crypto = setup(tmp_path)

    assert len(rows) == 1
    assert rows[0].source == "trackr_live:spring-weeks"

    with db.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(rows, default_cycle="2027")
        opportunity = session.query(Opportunity).one()

    assert report["imported"] == 1
    assert opportunity.source == "trackr_live:spring-weeks"
    assert opportunity.programme_group == "spring_week"


def test_source_label_never_self_attests_a_structured_application_target(
    tmp_path: Path,
) -> None:
    settings, db, crypto = setup(tmp_path)
    row = ScrapedOpportunity(
        employer="Acme",
        role_title="Summer Analyst",
        source_url="https://trackr.example.test/acme-summer",
        application_url="https://jobs.lever.co/acme/req/apply",
        ats_type="lever",
        source="lever:mutable-label",
    )

    with db.session_scope() as session:
        ScoutService(session, settings, crypto).ingest([row], default_cycle="2027")
        opportunity = session.query(Opportunity).one()
        evidence = json.loads(opportunity.resolution_evidence_json)

        assert opportunity.application_url is None
        assert opportunity.automation_url is None
        assert opportunity.resolved_ats_type == "lever"
        assert evidence["identity_verified"] is False


def test_untrusted_destination_host_gets_no_provider_hint_from_source_label(
    tmp_path: Path,
) -> None:
    settings, db, crypto = setup(tmp_path)
    row = ScrapedOpportunity(
        employer="Acme",
        role_title="Summer Analyst",
        source_url="https://trackr.example.test/acme-summer",
        application_url="https://attacker.example.test/acme/req/apply",
        ats_type="lever",
        source="lever:mutable-label",
    )

    with db.session_scope() as session:
        ScoutService(session, settings, crypto).ingest([row], default_cycle="2027")
        opportunity = session.query(Opportunity).one()

        assert opportunity.application_url is None
        assert opportunity.target_status == TargetKind.UNRESOLVED.value
        assert opportunity.resolved_ats_type == ""


def test_malformed_structured_candidate_is_contained_to_its_ingestion_row(
    tmp_path: Path,
) -> None:
    settings, db, crypto = setup(tmp_path)
    malformed = ScrapedOpportunity(
        employer="Bad Feed",
        role_title="Summer Analyst",
        source_url="https://trackr.example.test/bad",
        application_url="not-a-navigation-url",
        ats_type="lever",
        source="lever:bad",
    )
    valid = ScrapedOpportunity(
        employer="Good Feed",
        role_title="Spring Insight",
        source_url="https://trackr.example.test/good",
        source="aggregator",
    )

    with db.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [malformed, valid], default_cycle="2027"
        )
        records = session.query(Opportunity).all()

        assert report["seen"] == 2
        assert report["imported"] == 2
        assert report["applications"] == 2
        assert {record.employer for record in records} == {"Bad Feed", "Good Feed"}
        assert all(record.application_url is None for record in records)


def test_autopilot_yii_blocks_summer_fallback(tmp_path: Path) -> None:
    """If a firm has an open Yii, its Summer must NOT be auto-applied."""
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        scout.ingest(
            [
                _scraped("https://a.example.com/yii", "Alpha Bank", "Year in Industry 2027"),
                _scraped("https://a.example.com/summer", "Alpha Bank", "Summer Analyst"),
                _scraped("https://b.example.com/summer", "Beta Bank", "Summer Analyst"),
            ]
        )
        _mark_ingested_targets_verified(session)
        candidates = scout.autopilot_candidates()
        employers = {c[1].employer: c[1].programme_group for c in candidates}
        # Alpha Yii present; Alpha Summer suppressed; Beta Summer (no Yii) included
        assert employers.get("Alpha Bank") == "year_in_industry"
        assert employers.get("Beta Bank") == "summer"
        assert sum(1 for e in employers.values() if e == "summer") == 1


def test_priority_ordering(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        scout.ingest(
            [
                _scraped("https://c.example.com/summer", "Gamma", "Summer Analyst"),
                _scraped("https://d.example.com/yii", "Delta", "Year in Industry"),
                _scraped("https://e.example.com/spring", "Epsilon", "Spring Week"),
            ]
        )
        _mark_ingested_targets_verified(session)
        ordered = [pair[1].programme_group for pair in scout.autopilot_candidates()]
        assert ordered.index("year_in_industry") < ordered.index("spring_week")
        assert ordered.index("spring_week") < ordered.index("summer")


def test_closer_deadline_sorts_ahead_of_later_deadline(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        scout.ingest(
            [
                ScrapedOpportunity(
                    employer="Deadline Bank",
                    role_title="Summer Analyst",
                    url="https://example.test/later",
                    source="test",
                    deadline=date.today() + timedelta(days=30),
                ),
                ScrapedOpportunity(
                    employer="Deadline Bank",
                    role_title="Summer Analyst",
                    url="https://example.test/soon",
                    source="test",
                    deadline=date.today() + timedelta(days=2),
                ),
            ]
        )
        _mark_ingested_targets_verified(session)
        ordered = [pair[1].url for pair in scout.autopilot_candidates()]

        assert ordered[:2] == ["https://example.test/soon", "https://example.test/later"]


def test_expired_or_blocked_yii_does_not_suppress_open_summer(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        scout.ingest(
            [
                _scraped(
                    "https://example.test/expired-yii",
                    "Fallback Bank",
                    "Year in Industry",
                ),
                _scraped(
                    "https://example.test/open-summer",
                    "Fallback Bank",
                    "Summer Analyst",
                ),
            ]
        )
        _mark_ingested_targets_verified(session)
        yii = session.query(Opportunity).filter_by(url="https://example.test/expired-yii").one()
        yii.deadline = date.today() - timedelta(days=1)
        yii.application.state = ApplicationState.BLOCKED.value
        candidates = scout.autopilot_candidates()

        assert [opportunity.url for _, opportunity in candidates] == [
            "https://example.test/open-summer"
        ]


def test_review_only_autopilot_never_calls_submit(tmp_path: Path, monkeypatch) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
        }
    )
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox_from_path(settings.secret_key_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        application, opportunity = _prepared_candidate(session)
        monkeypatch.setattr(scout, "autopilot_candidates", lambda: [(application, opportunity)])
        modes = []

        def runner_factory(application_id, mode, headed):
            modes.append(mode.value)
            return {"state": "PACKAGE_PREPARED", "risk_level": 0, "adapter": "greenhouse"}

        result = scout.run_autopilot(runner_factory)

        assert modes == ["review"]
        assert result["submitted"] == 0
        assert result["details"][0]["result"] == "review_only"


def test_armed_autopilot_waits_for_exact_action_time_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "ARMED",
        }
    )
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox_from_path(settings.secret_key_path)
    with db.session_scope() as session:
        session.add(
            ConflictRule(
                employer_pattern="Confirm Bank",
                cycle="2027",
                max_applications=1,
            )
        )
        session.flush()
        scout = ScoutService(session, settings, crypto)
        application, opportunity = _prepared_candidate(session, employer="Confirm Bank")
        monkeypatch.setattr(scout, "autopilot_candidates", lambda: [(application, opportunity)])
        modes: list[str] = []

        def runner_factory(application_id, mode, headed):
            modes.append(mode.value)
            return {
                "state": "PACKAGE_PREPARED",
                "risk_level": 0,
                "adapter": "greenhouse",
            }

        result = scout.run_autopilot(
            runner_factory,
            confirmed_application_ids={"different-application"},
        )

        assert modes == ["review"]
        assert result["submitted"] == 0
        assert result["details"][0]["result"] == "awaiting_action_time_confirmation"
        assert result["details"][0]["confirmation_required"] is True


def test_empty_conflict_rules_block_armed_autopilot_submission(tmp_path: Path, monkeypatch) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "ARMED",
        }
    )
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox_from_path(settings.secret_key_path)
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        application, opportunity = _prepared_candidate(session, employer="No Rules Bank")
        monkeypatch.setattr(scout, "autopilot_candidates", lambda: [(application, opportunity)])
        modes = []

        def runner_factory(application_id, mode, headed):
            modes.append(mode.value)
            return {"state": "PACKAGE_PREPARED", "risk_level": 0, "adapter": "greenhouse"}

        result = scout.run_autopilot(
            runner_factory,
            confirmed_application_ids={application.id},
        )

        assert modes == ["review"]
        assert result["submitted"] == 0
        assert result["blocked"] == 1
        assert result["details"][0]["result"] == "blocked:conflict_rules_not_configured"


def test_unrelated_conflict_rule_does_not_arm_other_employer(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "ARMED",
        }
    )
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox_from_path(settings.secret_key_path)
    with db.session_scope() as session:
        session.add(
            ConflictRule(
                employer_pattern="Other Bank",
                cycle="2027",
                max_applications=2,
            )
        )
        session.flush()
        scout = ScoutService(session, settings, crypto)
        application, opportunity = _prepared_candidate(session, employer="Uncovered Bank")
        monkeypatch.setattr(scout, "autopilot_candidates", lambda: [(application, opportunity)])
        modes = []

        def runner_factory(application_id, mode, headed):
            modes.append(mode.value)
            return {"state": "PACKAGE_PREPARED", "risk_level": 0, "adapter": "greenhouse"}

        result = scout.run_autopilot(
            runner_factory,
            confirmed_application_ids={application.id},
        )

        assert modes == ["review"]
        assert result["submitted"] == 0
        assert result["details"][0]["result"] == "blocked:conflict_rules_not_configured"


def test_autopilot_counts_only_persisted_terminal_submission(tmp_path: Path, monkeypatch) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "ARMED",
        }
    )
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox_from_path(settings.secret_key_path)
    with db.session_scope() as session:
        session.add(
            ConflictRule(
                employer_pattern="Persisted Bank",
                cycle="2027",
                max_applications=2,
            )
        )
        session.flush()
        scout = ScoutService(session, settings, crypto)
        application, opportunity = _prepared_candidate(session, employer="Persisted Bank")
        monkeypatch.setattr(scout, "autopilot_candidates", lambda: [(application, opportunity)])

        def runner_factory(application_id, mode, headed):
            if mode.value == "submit":
                return {
                    "state": "CONFIRMATION_VERIFIED",
                    "risk_level": 0,
                    "adapter": "greenhouse",
                    "receipt": {"reference": "FORGED-RETURN-ONLY"},
                }
            return {"state": "PACKAGE_PREPARED", "risk_level": 0, "adapter": "greenhouse"}

        result = scout.run_autopilot(
            runner_factory,
            confirmed_application_ids={application.id},
        )

        assert result["submitted"] == 0
        assert result["details"][0]["result"] == "submit_unverified"


def test_autopilot_counts_a_persisted_terminal_receipt(tmp_path: Path, monkeypatch) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "ARMED",
        }
    )
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox_from_path(settings.secret_key_path)
    with db.session_scope() as session:
        session.add(
            ConflictRule(
                employer_pattern="Persisted Bank",
                cycle="2027",
                max_applications=2,
            )
        )
        session.flush()
        scout = ScoutService(session, settings, crypto)
        application, opportunity = _prepared_candidate(session, employer="Persisted Bank")
        monkeypatch.setattr(scout, "autopilot_candidates", lambda: [(application, opportunity)])

        def runner_factory(application_id, mode, headed):
            if mode.value == "submit":
                application.state = ApplicationState.CONFIRMATION_VERIFIED.value
                application.submission_reference = "PERSISTED-REF"
                session.commit()
                return {
                    "state": "NEEDS_USER",
                    "risk_level": 0,
                    "adapter": "greenhouse",
                    "receipt": {"reference": "RETURN-VALUE-IGNORED"},
                }
            return {"state": "PACKAGE_PREPARED", "risk_level": 0, "adapter": "greenhouse"}

        result = scout.run_autopilot(
            runner_factory,
            confirmed_application_ids={application.id},
        )

        assert result["submitted"] == 1
        assert result["details"][0]["receipt"] == "PERSISTED-REF"


def test_autopilot_verifies_receipt_committed_by_runner_session(
    tmp_path: Path, monkeypatch
) -> None:
    """The scheduled runner writes in a separate DB session in production."""

    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "ARMED",
        }
    )
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    crypto = CryptoBox_from_path(settings.secret_key_path)
    with db.session_scope() as session:
        session.add(
            ConflictRule(
                employer_pattern="Session Bank",
                cycle="2027",
                max_applications=2,
            )
        )
        application, opportunity = _prepared_candidate(session, employer="Session Bank")
        session.commit()
        scout = ScoutService(session, settings, crypto)
        monkeypatch.setattr(scout, "autopilot_candidates", lambda: [(application, opportunity)])

        def runner_factory(application_id, mode, headed):
            if mode.value == "submit":
                with db.session_scope() as runner_session:
                    persisted = runner_session.get(Application, application_id)
                    persisted.state = ApplicationState.CONFIRMATION_VERIFIED.value
                    persisted.submission_reference = "SEPARATE-SESSION-REF"
                return {
                    "state": "CONFIRMATION_VERIFIED",
                    "risk_level": 0,
                    "adapter": "greenhouse",
                    "receipt": {"reference": "SEPARATE-SESSION-REF"},
                }
            return {"state": "PACKAGE_PREPARED", "risk_level": 0, "adapter": "greenhouse"}

        result = scout.run_autopilot(
            runner_factory,
            confirmed_application_ids={application.id},
        )

        assert result["submitted"] == 1
        assert result["details"][0]["receipt"] == "SEPARATE-SESSION-REF"


def test_ingest_is_idempotent_across_runs(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)

    def run_once() -> dict[str, int]:
        with db.session_scope() as session:
            scout = ScoutService(session, settings, crypto)
            return scout.ingest([_scraped("https://boards.greenhouse.io/gs/jobs/42")])

    first = run_once()
    second = run_once()
    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["duplicates"] == 1
