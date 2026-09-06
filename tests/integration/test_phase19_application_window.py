from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.models import Application, Opportunity
from app.scouting import trackr_live
from app.scouting.service import ScoutService
from app.scouting.trackr import ScrapedOpportunity
from app.scouting.trackr_live import TrackrRow
from app.services.applications import ApplicationService
from app.services.batch_target_resolution import (
    BatchResolveOptions,
    BatchTargetResolutionDriver,
)
from app.services.target_resolution import TargetResolutionService


def _setup(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    from app.security.crypto import CryptoBox

    return settings, database, CryptoBox.from_path(settings.secret_key_path)


def _payload(*, application_url: str | None, opening_date: str | None) -> dict[str, object]:
    return {
        "id": "programme-window-transition",
        "companyId": "window-capital",
        "company": {"id": "window-capital", "name": "Window Capital"},
        "name": "Industrial Placement 2027",
        "url": application_url,
        "openingDate": opening_date,
        "closingDate": "2026-10-16T00:00:00.000Z" if application_url else None,
        "locations": ["London"],
        "rolling": False,
        "status": None,
        "type": "industrial-placements",
        "season": "2027",
    }


def _rows(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
):
    async def fake_scrape_via_browser() -> list[tuple[str, object]]:
        return [
            (
                "industrial-placements",
                {"groups": [], "programmes": [payload]},
            )
        ]

    monkeypatch.setattr(trackr_live, "_scrape_via_browser", fake_scrape_via_browser)
    return trackr_live.fetch_programmes()


def test_not_yet_open_row_transitions_in_place_and_becomes_work_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing link becoming real must change state, never create a new row."""

    settings, database, crypto = _setup(tmp_path)
    waiting = _rows(
        monkeypatch,
        _payload(application_url=None, opening_date=None),
    )

    with database.session_scope() as session:
        ScoutService(session, settings, crypto).ingest(waiting, default_cycle="2027")
        opportunity = session.query(Opportunity).one()
        original_opportunity_id = opportunity.id
        original_application_id = session.query(Application.id).scalar()

        assert opportunity.application_window_status == "NOT_YET_OPEN"
        assert opportunity.application_url is None
        assert opportunity.is_archived is False
        assert ScoutService(session, settings, crypto).autopilot_candidates() == []

    waiting_selection = BatchTargetResolutionDriver(database, settings)._select(
        BatchResolveOptions(limit=10, dry_run=True, delay_seconds=0)
    )
    assert waiting_selection == []

    employer_url = "https://jobs.smartrecruiters.com/WindowCapital/placement-2027"
    opened = _rows(
        monkeypatch,
        _payload(
            application_url=employer_url,
            opening_date="2026-08-24T00:00:00.000Z",
        ),
    )
    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            opened,
            default_cycle="2027",
        )
        opportunity = session.query(Opportunity).one()

        assert report["updated"] == 1
        assert report["imported"] == 0
        assert opportunity.id == original_opportunity_id
        assert session.query(Application.id).scalar() == original_application_id
        assert opportunity.application_window_status == "OPEN"
        assert opportunity.application_url == employer_url
        assert opportunity.is_archived is False

    open_selection = BatchTargetResolutionDriver(database, settings)._select(
        BatchResolveOptions(limit=10, dry_run=True, delay_seconds=0)
    )
    assert [item.opportunity_id for item in open_selection] == [
        original_opportunity_id
    ]


def test_closed_row_is_reversibly_archived_and_stops_being_worked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    payload = _payload(
        application_url="https://jobs.example.com/closed-placement",
        opening_date="2026-07-01T00:00:00.000Z",
    )
    payload["closingDate"] = "2026-08-20T00:00:00.000Z"
    closed = _rows(monkeypatch, payload)

    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            closed,
            default_cycle="2027",
        )
        opportunity = session.query(Opportunity).one()

        assert report["closed_archived"] == 1
        assert opportunity.application_window_status == "CLOSED"
        assert opportunity.is_archived is True
        assert opportunity.archived_reason == "closed_application_window"
        assert ScoutService(session, settings, crypto).autopilot_candidates() == []

    assert BatchTargetResolutionDriver(database, settings)._select(
        BatchResolveOptions(limit=10, dry_run=True, delay_seconds=0)
    ) == []

    payload["status"] = "open"
    reopened = _rows(monkeypatch, payload)
    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            reopened,
            default_cycle="2027",
        )
        opportunity = session.query(Opportunity).one()

        assert report["closed_unarchived"] == 1
        assert opportunity.application_window_status == "OPEN"
        assert opportunity.is_archived is False
        assert opportunity.archive_record is not None
        assert opportunity.archive_record.archived_reason is None


def test_unknown_is_fail_closed_for_resolution_autopilot_and_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    payload = _payload(
        application_url="https://jobs.example.com/unknown-window",
        opening_date=None,
    )
    payload["closingDate"] = None
    payload["status"] = "source_specific_mystery"
    unknown = _rows(monkeypatch, payload)

    with database.session_scope() as session:
        ScoutService(session, settings, crypto).ingest(
            unknown,
            default_cycle="2027",
        )
        opportunity = session.query(Opportunity).one()
        application = session.query(Application).one()
        opportunity_id = opportunity.id
        application.state = "QUEUED"

        assert opportunity.application_window_status == "UNKNOWN"
        assert opportunity.automation_url is None
        assert ScoutService(session, settings, crypto).autopilot_candidates() == []
        prepared = ApplicationService(session, settings, crypto).prepare(application.id)
        assert prepared.ready is False
        assert prepared.reason_codes == ("application_window_not_open",)

    called = False

    def forbidden_resolver(_context):
        nonlocal called
        called = True
        return None

    with database.session_scope() as session:
        with pytest.raises(ValueError, match="application window is not OPEN"):
            TargetResolutionService(session).resolve(
                opportunity_id,
                resolver=forbidden_resolver,
            )
    assert called is False


def test_payload_dates_and_literal_rolling_are_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    opened = _rows(
        monkeypatch,
        _payload(
            application_url="https://jobs.example.com/date-evidence",
            opening_date="2026-08-24T00:00:00.000Z",
        ),
    )

    with database.session_scope() as session:
        ScoutService(session, settings, crypto).ingest(opened, default_cycle="2027")
        opportunity = session.query(Opportunity).one()

        assert opportunity.opening_date.isoformat() == "2026-08-24"
        assert opportunity.deadline.isoformat() == "2026-10-16"
        assert opportunity.rolling is False
        assert opportunity.application_window_status == "OPEN"


def test_identified_row_does_not_claim_unscoped_legacy_trackr_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    legacy = ScrapedOpportunity(
        employer="Centerview Partners",
        role_title="2027 Summer Internship Programme",
        source_url="https://app.the-trackr.com/job/legacy-id",
        source="trackr_live",
    )
    with database.session_scope() as session:
        ScoutService(session, settings, crypto).ingest([legacy])
        original_id = session.query(Opportunity.id).scalar()

    payload = _payload(
        application_url="https://app.the-trackr.com/job/current-id",
        opening_date="2026-08-24T00:00:00.000Z",
    )
    payload["company"] = {"name": "Centerview Partners"}
    payload["name"] = "2027 Summer Internship Programme"
    current = _rows(monkeypatch, payload)
    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(current)
        records = session.query(Opportunity).all()

        assert report["legacy_sources_migrated"] == 0
        assert report["trackr_id_inserted"] == 1
        assert len(records) == 2
        legacy_record = next(record for record in records if record.id == original_id)
        identified_record = next(
            record for record in records if record.trackr_programme_id is not None
        )
        assert legacy_record.trackr_programme_id is None
        assert legacy_record.source == "trackr_live"
        assert identified_record.url.endswith("/industrial-placements")
        assert identified_record.application_url is None


def test_unique_trackr_employer_programme_match_absorbs_title_correction(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    tracker_url = "https://app.the-trackr.com/uk-finance/summer-internships"
    old = TrackrRow(
        employer="Capital One",
        role_title="Strategy Analyst Intern",
        tracker_url=tracker_url,
        employer_application_url=None,
        opening_date=None,
        closing_date=None,
        programme_type="summer",
        source="trackr_live:summer-internships",
    )
    new = TrackrRow(
        employer="Capital One",
        role_title="Strategy Analyst Summer Internship",
        tracker_url=tracker_url,
        employer_application_url="https://capitalone.example/jobs/strategy-internship",
        opening_date=None,
        closing_date=None,
        programme_type="summer",
        source="trackr_live:summer-internships",
        explicit_status="open",
    )

    with database.session_scope() as session:
        ScoutService(session, settings, crypto).ingest([old])
        original_id = session.query(Opportunity.id).scalar()
    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest([new])
        records = session.query(Opportunity).all()

        assert report["legacy_sources_migrated"] == 1
        assert len(records) == 1
        assert records[0].id == original_id
        assert records[0].role_title == "Strategy Analyst Summer Internship"
