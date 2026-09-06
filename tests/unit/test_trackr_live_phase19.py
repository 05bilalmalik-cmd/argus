from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.domain.targets import TargetKind
from app.models import Application, Opportunity
from app.scouting import trackr_live
from app.scouting.service import ScoutService
from app.scouting.trackr import ScrapedOpportunity


def _setup(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    from app.security.crypto import CryptoBox

    return settings, db, CryptoBox.from_path(settings.secret_key_path)


def _fetch_rows(
    monkeypatch: pytest.MonkeyPatch,
    *items: dict[str, object],
    slug: str = "industrial-placements",
):
    async def fake_scrape_via_browser() -> list[tuple[str, object]]:
        return [(slug, {"groups": [], "programmes": list(items)})]

    monkeypatch.setattr(trackr_live, "_scrape_via_browser", fake_scrape_via_browser)
    return trackr_live.fetch_programmes()


def _payload_row(
    *,
    employer: str = "Evidence Capital",
    role: str = "Industrial Placement 2027",
    url: object = None,
    source_record_id: object = "programme-123",
) -> dict[str, object]:
    return {
        "id": source_record_id,
        "companyId": "evidence-capital",
        "company": {
            "id": "evidence-capital",
            "name": employer,
            "careersSite": "https://careers.example.com/early-careers",
        },
        "name": role,
        "url": url,
        "openingDate": "2026-08-24T00:00:00.000Z",
        "closingDate": "2026-10-16T00:00:00.000Z",
        "locations": ["London"],
        "type": "industrial-placements",
        "season": "2027",
    }


def test_distinct_source_ids_survive_same_name_collisions_within_one_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _payload_row(source_record_id="programme-a")
    second = _payload_row(source_record_id="programme-b")

    rows = _fetch_rows(monkeypatch, first, second)

    assert [row.source_record_id for row in rows] == ["programme-a", "programme-b"]


def test_distinct_source_ids_survive_same_name_collisions_across_slugs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_scrape_via_browser() -> list[tuple[str, object]]:
        return [
            ("industrial-placements", {"programmes": [_payload_row(source_record_id="yii-a")]}),
            ("summer-internships", {"programmes": [_payload_row(source_record_id="summer-b")]}),
        ]

    monkeypatch.setattr(trackr_live, "_scrape_via_browser", fake_scrape_via_browser)

    rows = trackr_live.fetch_programmes()

    assert [row.source_record_id for row in rows] == ["yii-a", "summer-b"]
    assert [row.source for row in rows] == [
        "trackr_live:industrial-placements",
        "trackr_live:summer-internships",
    ]


def test_identical_duplicate_source_id_collapses_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload_row(source_record_id=" duplicate-id ")

    rows = _fetch_rows(monkeypatch, payload, dict(payload))

    assert len(rows) == 1
    assert rows[0].source_record_id == "duplicate-id"


def test_conflicting_duplicate_source_id_fails_the_whole_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _payload_row(source_record_id="duplicate-id")
    second = _payload_row(
        source_record_id="duplicate-id",
        role="Different material role",
    )

    with pytest.raises(trackr_live.TrackrPayloadIdentityError, match="duplicate-id"):
        _fetch_rows(monkeypatch, first, second)


@pytest.mark.parametrize("source_record_id", [None, "", "   ", 123, "x" * 129])
def test_invalid_source_id_is_explicitly_idless(
    monkeypatch: pytest.MonkeyPatch,
    source_record_id: object,
) -> None:
    row = _fetch_rows(
        monkeypatch,
        _payload_row(source_record_id=source_record_id),
    )[0]

    assert row.source_record_id == ""


def test_payload_employer_link_is_captured_separately_from_tracker_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _fetch_rows(
        monkeypatch,
        _payload_row(
            url=(
                "https://jobs.smartrecruiters.com/Evidence/123-role"
                "?utm_source=Trackr&trid=Trackr&job=123"
            )
        ),
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.source_url == (
        "https://app.the-trackr.com/uk-finance/industrial-placements"
    )
    assert row.url == row.source_url
    assert row.application_url == (
        "https://jobs.smartrecruiters.com/Evidence/123-role?job=123"
    )
    assert row.location == "London"
    assert row.opening_date == date(2026, 8, 24)
    assert row.closing_date == date(2026, 10, 16)
    assert row.deadline == date(2026, 10, 16)
    assert row.ats_type == "smartrecruiters"


def test_payload_without_employer_link_never_falls_back_to_tracker_as_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _fetch_rows(monkeypatch, _payload_row(url=None))[0]

    assert row.source_url == (
        "https://app.the-trackr.com/uk-finance/industrial-placements"
    )
    assert row.application_url is None
    assert row.url == row.source_url


@pytest.mark.parametrize(
    "candidate",
    [
        "not-a-url",
        "http://jobs.example.com/role",
        "https://user:password@jobs.example.com/role",
        "https://localhost/role",
        "https://localhost.localdomain/role",
        "https://127.0.0.1/role",
        "https://10.0.0.2/role",
        "https://169.254.1.2/role",
        "https://[::1]/role",
        "https://app.the-trackr.com/uk-finance/spring-weeks",
        "https://api.the-trackr.com/programmes",
        "https://www.the-trackr.com/trackers",
        "https://jobs.example.com/" + ("x" * 2050),
    ],
)
def test_invalid_or_non_public_application_targets_are_rejected(candidate: str) -> None:
    assert trackr_live._clean_url(candidate) == ""


def test_valid_payload_link_is_stored_unresolved_without_running_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    employer_url = "https://jobs.smartrecruiters.com/Evidence/123-role"
    row = _fetch_rows(monkeypatch, _payload_row(url=employer_url))[0]
    settings, db, crypto = _setup(tmp_path)

    with db.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [row], default_cycle="2027"
        )
        opportunity = session.query(Opportunity).one()

        assert report["application_urls_captured"] == 1
        assert report["without_employer_link"] == 0
        assert opportunity.url == row.source_url
        assert opportunity.application_url == employer_url
        assert opportunity.target_status == TargetKind.UNRESOLVED.value
        assert opportunity.automation_url is None


def test_missing_payload_link_has_distinct_state_and_visible_ingest_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _fetch_rows(monkeypatch, _payload_row(url=None))[0]
    settings, db, crypto = _setup(tmp_path)

    with db.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [row], default_cycle="2027"
        )
        opportunity = session.query(Opportunity).one()

        assert report["without_employer_link"] == 1
        assert report["invalid_application_urls"] == 0
        assert opportunity.url == row.source_url
        assert opportunity.application_url is None
        assert opportunity.target_status == "MISSING_EMPLOYER_LINK"
        assert opportunity.automation_url is None


def test_invalid_payload_link_is_rejected_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _fetch_rows(
        monkeypatch,
        _payload_row(url="https://127.0.0.1/private-role"),
    )[0]
    settings, db, crypto = _setup(tmp_path)

    with db.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [row], default_cycle="2027"
        )
        opportunity = session.query(Opportunity).one()

        assert report["without_employer_link"] == 1
        assert report["invalid_application_urls"] == 1
        assert opportunity.application_url is None
        assert opportunity.target_status == "MISSING_EMPLOYER_LINK"


def test_backfill_updates_existing_fingerprint_without_duplicate_or_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, db, crypto = _setup(tmp_path)
    missing = _fetch_rows(monkeypatch, _payload_row(url=None))[0]

    with db.session_scope() as session:
        ScoutService(session, settings, crypto).ingest([missing], default_cycle="2027")
        before_opportunity_ids = {
            value for (value,) in session.query(Opportunity.id).all()
        }
        before_application_ids = {
            value for (value,) in session.query(Application.id).all()
        }

    employer_url = "https://jobs.smartrecruiters.com/Evidence/123-role"
    linked = _fetch_rows(monkeypatch, _payload_row(url=employer_url))[0]
    with db.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [linked], default_cycle="2027"
        )
        opportunity = session.query(Opportunity).one()
        after_opportunity_ids = {
            value for (value,) in session.query(Opportunity.id).all()
        }
        after_application_ids = {
            value for (value,) in session.query(Application.id).all()
        }

        assert report["imported"] == 0
        assert report["applications"] == 0
        assert report["application_urls_backfilled"] == 1
        assert report["updated"] == 1
        assert opportunity.application_url == employer_url
        assert opportunity.target_status == TargetKind.UNRESOLVED.value
        assert before_opportunity_ids == after_opportunity_ids
        assert before_application_ids == after_application_ids


def test_backfill_is_idempotent_and_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, db, crypto = _setup(tmp_path)
    employer_url = "https://jobs.smartrecruiters.com/Evidence/123-role"
    linked = _fetch_rows(monkeypatch, _payload_row(url=employer_url))[0]

    def ingest_once() -> tuple[dict[str, int], set[str], set[str]]:
        with db.session_scope() as session:
            report = ScoutService(session, settings, crypto).ingest(
                [linked], default_cycle="2027"
            )
            opportunity_ids = {
                value for (value,) in session.query(Opportunity.id).all()
            }
            application_ids = {
                value for (value,) in session.query(Application.id).all()
            }
            return report, opportunity_ids, application_ids

    first_report, first_opportunities, first_applications = ingest_once()
    second_report, second_opportunities, second_applications = ingest_once()

    assert first_report["imported"] == 1
    assert second_report["imported"] == 0
    assert second_report["application_urls_backfilled"] == 0
    assert second_report["updated"] == 0
    assert second_report["duplicates"] == 1
    assert first_opportunities == second_opportunities
    assert first_applications == second_applications


def test_identified_backfill_does_not_broadly_claim_legacy_application_url_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, db, crypto = _setup(tmp_path)
    employer_url = "https://jobs.smartrecruiters.com/Evidence/123-role"
    legacy = ScrapedOpportunity(
        employer="Evidence Capital",
        role_title="Industrial Placement 2027",
        source_url=employer_url,
        source="trackr_live:industrial-placements",
    )

    with db.session_scope() as session:
        ScoutService(session, settings, crypto).ingest([legacy], default_cycle="2027")
        original_id = session.query(Opportunity.id).scalar()

    linked = _fetch_rows(monkeypatch, _payload_row(url=employer_url))[0]
    with db.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [linked], default_cycle="2027"
        )
        opportunities = session.query(Opportunity).all()
        applications = session.query(Application).all()

        assert report["legacy_sources_migrated"] == 0
        assert report["trackr_id_inserted"] == 1
        assert len(opportunities) == 2
        assert len(applications) == 2
        legacy_record = next(record for record in opportunities if record.id == original_id)
        identified_record = next(
            record for record in opportunities if record.trackr_programme_id is not None
        )
        assert legacy_record.trackr_programme_id is None
        assert legacy_record.url == employer_url
        assert identified_record.url == linked.source_url
        assert identified_record.application_url == employer_url


def test_backfill_never_overwrites_a_verified_automation_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, db, crypto = _setup(tmp_path)
    missing = _fetch_rows(monkeypatch, _payload_row(url=None))[0]
    verified_url = "https://jobs.smartrecruiters.com/Evidence/verified-role"

    with db.session_scope() as session:
        ScoutService(session, settings, crypto).ingest([missing], default_cycle="2027")
        opportunity = session.query(Opportunity).one()
        opportunity.application_url = verified_url
        opportunity.target_status = TargetKind.APPLICATION_ENTRY.value
        opportunity.resolved_ats_type = "smartrecruiters"
        opportunity.resolved_at = datetime.now(timezone.utc)

    newly_reported_url = "https://jobs.smartrecruiters.com/Evidence/new-unverified-role"
    linked = _fetch_rows(monkeypatch, _payload_row(url=newly_reported_url))[0]
    with db.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [linked], default_cycle="2027"
        )
        opportunity = session.query(Opportunity).one()

        assert report["verified_targets_preserved"] == 1
        assert opportunity.application_url == verified_url
        assert opportunity.target_status == TargetKind.APPLICATION_ENTRY.value
        assert opportunity.automation_url == verified_url
