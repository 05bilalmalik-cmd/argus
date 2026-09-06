from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db import Database, SCHEMA_VERSION
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.models import (
    Application,
    AuditEvent,
    AutomationRun,
    Opportunity,
    OpportunityArchive,
    SubmissionAuthority,
    SubmissionIntent,
)
from app.repositories import ApplicationRepository
from app.scouting.application_window import ApplicationWindowStatus
from app.scouting.divisions import infer_division
from app.scouting.service import ScoutService
from app.scouting.trackr import ScrapedOpportunity
from app.scouting.trackr_identity import TrackrIdentityConflictError
from app.scouting.trackr_live import TrackrRow
from app.services.target_resolution import TargetResolutionService


def _setup(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    from app.security.crypto import CryptoBox

    return settings, database, CryptoBox.from_path(settings.secret_key_path)


def _row(
    raw_id: str,
    *,
    role_title: str = "Investment Banking Industrial Placement 2027",
    location: str = "London",
    application_url: str | None = None,
    source: str = "trackr_live:industrial-placements",
) -> TrackrRow:
    slug = source.partition(":")[2] or "industrial-placements"
    return TrackrRow(
        employer="Evidence Capital",
        role_title=role_title,
        tracker_url=f"https://app.the-trackr.com/uk-finance/{slug}",
        employer_application_url=application_url,
        opening_date=None,
        closing_date=None,
        programme_type="year_in_industry",
        location=location,
        source=source,
        explicit_status="open" if application_url else None,
        rolling=False,
        season="2027",
        source_record_id=raw_id,
    )


def _seed_without_application(
    session,
    *,
    raw_id: str | None,
) -> Opportunity:
    row = _row(raw_id or "")
    record = Opportunity(
        employer=row.employer,
        role_title=row.role_title,
        division=infer_division(row.role_title, row.employer),
        programme_group=row.programme_type,
        location=row.location,
        cycle="2027",
        url=row.tracker_url,
        source=row.source,
        ats_type=row.ats_type,
        rolling=False,
        application_window_status=row.application_window_status.value,
        target_status="MISSING_EMPLOYER_LINK",
        trackr_programme_id=raw_id,
    )
    session.add(record)
    session.flush()
    return record


def test_same_key_distinct_ids_create_distinct_rows_and_repeat_is_noop(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    rows = [_row("trackr-a"), _row("trackr-b")]

    with database.session_scope() as session:
        first = ScoutService(session, settings, crypto).ingest(rows, default_cycle="2027")
        opportunities = list(session.scalars(select(Opportunity)).all())
        applications = list(session.scalars(select(Application)).all())
        assert first["trackr_ids_seen"] == 2
        assert first["trackr_id_inserted"] == 2
        assert len(opportunities) == 2
        assert len(applications) == 2
        assert {record.trackr_programme_id for record in opportunities} == {
            "trackr-a",
            "trackr-b",
        }
        assert len({record.source_fingerprint for record in opportunities}) == 1
        original_opportunity_ids = {record.id for record in opportunities}
        original_application_ids = {record.id for record in applications}

    with database.session_scope() as session:
        first_audit_count = session.query(AuditEvent).count()

    with database.session_scope() as session:
        second = ScoutService(session, settings, crypto).ingest(rows, default_cycle="2027")

        assert second["trackr_id_matched"] == 2
        assert second["trackr_id_inserted"] == 0
        assert second["updated"] == 0
        assert second["duplicates"] == 2
        assert {
            record.id for record in session.scalars(select(Opportunity)).all()
        } == original_opportunity_ids
        assert {
            record.id for record in session.scalars(select(Application)).all()
        } == original_application_ids
        assert session.query(AuditEvent).count() == first_audit_count


def test_direct_id_refresh_preserves_ids_fingerprint_and_verified_target(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    initial = _row("trackr-a", application_url="https://jobs.example.com/original")
    with database.session_scope() as session:
        ScoutService(session, settings, crypto).ingest([initial], default_cycle="2027")
        record = session.query(Opportunity).one()
        application = session.query(Application).one()
        original = (record.id, application.id, record.source_fingerprint)
        verified_url = "https://jobs.example.com/verified"
        record.application_url = verified_url
        record.target_status = TargetKind.APPLICATION_ENTRY.value
        record.resolved_ats_type = "custom"
        record.resolved_at = datetime.now(timezone.utc)

    refreshed = _row(
        "trackr-a",
        role_title="Investment Banking Placement - corrected title",
        location="London, Edinburgh",
        application_url="https://jobs.example.com/new-unverified",
    )
    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [refreshed], default_cycle="2027"
        )
        record = session.query(Opportunity).one()
        application = session.query(Application).one()

        assert report["trackr_id_matched"] == 1
        assert report["verified_targets_preserved"] == 1
        assert (record.id, application.id, record.source_fingerprint) == original
        assert record.role_title == refreshed.role_title
        assert record.location == refreshed.location
        assert record.application_url == "https://jobs.example.com/verified"
        assert record.trackr_programme_id == "trackr-a"


def test_unique_strict_legacy_candidate_is_claimed_without_new_rows(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    legacy = _row("")
    with database.session_scope() as session:
        ScoutService(session, settings, crypto).ingest([legacy], default_cycle="2027")
        original_opportunity_id = session.query(Opportunity.id).scalar()
        original_application_id = session.query(Application.id).scalar()
        original_fingerprint = session.query(Opportunity.source_fingerprint).scalar()

    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [_row("trackr-a")], default_cycle="2027"
        )
        record = session.query(Opportunity).one()

        assert report["trackr_id_legacy_claimed"] == 1
        assert report["trackr_id_inserted"] == 0
        assert record.id == original_opportunity_id
        assert record.source_fingerprint == original_fingerprint
        assert record.trackr_programme_id == "trackr-a"
        assert session.query(Application.id).scalar() == original_application_id

    with database.session_scope() as session:
        assert session.query(AuditEvent).filter_by(
            event_type="scout.trackr_identity_claimed"
        ).count() == 1


def test_existing_bound_identity_without_application_remains_childless(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    with database.session_scope() as session:
        record = _seed_without_application(session, raw_id="trackr-a")
        opportunity_id = record.id

    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [_row("trackr-a")], default_cycle="2027"
        )

        assert report["trackr_id_matched"] == 1
        assert report["applications"] == 0
        assert session.query(Opportunity.id).scalar() == opportunity_id
        assert session.query(Application).count() == 0


def test_metadata_only_legacy_claim_without_application_preserves_child_set(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    with database.session_scope() as session:
        record = _seed_without_application(session, raw_id=None)
        opportunity_id = record.id

    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [_row("trackr-a")], default_cycle="2027"
        )
        record = session.get(Opportunity, opportunity_id)

        assert report["trackr_id_legacy_claimed"] == 1
        assert report["trackr_id_inserted"] == 0
        assert report["applications"] == 0
        assert record is not None
        assert record.trackr_programme_id == "trackr-a"
        assert session.query(Application).count() == 0


def test_identity_race_refresh_does_not_create_application_for_existing_row(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    row = _row("trackr-a")
    with database.session_scope() as session:
        record = _seed_without_application(session, raw_id="trackr-a")
        candidate = Opportunity(
            employer=row.employer,
            role_title=row.role_title,
            division=infer_division(row.role_title, row.employer),
            programme_group=row.programme_type,
            location=row.location,
            cycle="2027",
            url=row.tracker_url,
            source=row.source,
            ats_type=row.ats_type,
            rolling=False,
            application_window_status=row.application_window_status.value,
            target_status="MISSING_EMPLOYER_LINK",
            trackr_programme_id="trackr-a",
        )
        stats = {
            "verified_targets_preserved": 0,
            "application_urls_backfilled": 0,
            "closed_archived": 0,
            "closed_unarchived": 0,
            "applications": 0,
            "updated": 0,
            "duplicates": 0,
        }

        ScoutService(session, settings, crypto)._refresh_identity_race_record(
            record,
            candidate,
            candidate_application_url=None,
            window_status=ApplicationWindowStatus.NOT_YET_OPEN,
            stats=stats,
        )

        assert stats["applications"] == 0
        assert session.query(Application).count() == 0


def test_ambiguous_two_id_claim_inserts_both_and_archives_legacy_reversibly(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    with database.session_scope() as session:
        ScoutService(session, settings, crypto).ingest([_row("")], default_cycle="2027")
        legacy_id = session.query(Opportunity.id).scalar()

    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [_row("trackr-a"), _row("trackr-b")], default_cycle="2027"
        )
        legacy = session.get(Opportunity, legacy_id)

        assert report["trackr_id_legacy_ambiguous"] == 2
        assert report["trackr_ambiguous_legacy_archived"] == 1
        assert report["trackr_id_inserted"] == 2
        assert session.query(Opportunity).count() == 3
        assert session.query(Application).count() == 3
        assert legacy is not None
        assert legacy.trackr_programme_id is None
        assert legacy.archived_reason == "ambiguous_trackr_identity"
        assert legacy.is_archived is True


def test_ambiguous_legacy_archive_is_deferred_when_all_replacements_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    with database.session_scope() as session:
        legacy = _seed_without_application(session, raw_id=None)
        legacy_id = legacy.id

    def fail_application_binding(*_args: object, **_kwargs: object):
        raise RuntimeError("injected application binding failure")

    monkeypatch.setattr(
        ApplicationRepository,
        "get_or_create_for_opportunity",
        fail_application_binding,
    )
    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [_row("trackr-a"), _row("trackr-b")], default_cycle="2027"
        )

        assert report["ingestion_failure"] == 2
        assert report["trackr_id_inserted"] == 0
        assert report["trackr_ambiguous_legacy_archived"] == 0
        assert report["trackr_ambiguous_legacy_deferred"] == 1

    with database.session_scope() as session:
        legacy = session.get(Opportunity, legacy_id)
        assert legacy is not None
        assert legacy.application_window_status == "NOT_YET_OPEN"
        assert legacy.is_archived is False
        assert session.query(Opportunity).filter(
            Opportunity.trackr_programme_id.is_not(None)
        ).count() == 0
        assert session.query(Application).count() == 0
        ambiguity_events = [
            event
            for event in session.query(AuditEvent).filter_by(
                event_type="opportunity.archived"
            )
            if json.loads(event.details_json).get("reason")
            == "ambiguous_trackr_identity"
        ]
        assert ambiguity_events == []


def test_ambiguous_identity_archive_preserves_existing_non_uk_reason(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    with database.session_scope() as session:
        ScoutService(session, settings, crypto).ingest([_row("")], default_cycle="2027")
        legacy = session.query(Opportunity).one()
        marker = OpportunityArchive(
            opportunity=legacy,
            archived_at=datetime.now(timezone.utc),
            archived_reason="non_uk",
        )
        session.add(marker)
        legacy_id = legacy.id

    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [_row("trackr-a"), _row("trackr-b")], default_cycle="2027"
        )
        legacy = session.get(Opportunity, legacy_id)

        assert report["trackr_ambiguous_legacy_archived"] == 0
        assert report["trackr_ambiguous_legacy_preserved"] == 1
        assert legacy is not None
        assert legacy.archived_reason == "non_uk"
        assert legacy.is_archived is True


def test_different_inbound_id_never_overwrites_existing_binding(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    with database.session_scope() as session:
        ScoutService(session, settings, crypto).ingest(
            [_row("trackr-a", application_url="https://jobs.example.com/a")],
            default_cycle="2027",
        )
        first = session.query(Opportunity).one()
        first_id = first.id

    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [_row("trackr-b", application_url="https://jobs.example.com/b")],
            default_cycle="2027",
        )
        records = {record.trackr_programme_id: record for record in session.query(Opportunity)}

        assert report["trackr_id_inserted"] == 1
        assert set(records) == {"trackr-a", "trackr-b"}
        assert records["trackr-a"].id == first_id
        assert records["trackr-a"].application_url == "https://jobs.example.com/a"
        assert records["trackr-b"].application_url == "https://jobs.example.com/b"


def test_id_b_identical_to_verified_id_a_creates_separately_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    first_row = _row("trackr-a", application_url="https://jobs.example.com/a")
    with database.session_scope() as session:
        ScoutService(session, settings, crypto).ingest(
            [first_row], default_cycle="2027"
        )
        first = session.query(Opportunity).one()
        first.application_url = "https://jobs.example.com/verified-a"
        first.target_status = TargetKind.APPLICATION_ENTRY.value
        first.resolved_at = datetime.now(timezone.utc)
        first_id = first.id

    def forbidden_target_resolution(*_args: object, **_kwargs: object):
        raise AssertionError("identified ingest must not invoke target resolution")

    monkeypatch.setattr(
        TargetResolutionService,
        "record",
        forbidden_target_resolution,
    )
    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            [_row("trackr-b", application_url="https://jobs.example.com/b")],
            default_cycle="2027",
        )
        records = {
            record.trackr_programme_id: record
            for record in session.query(Opportunity).all()
        }

        assert report["trackr_id_inserted"] == 1
        assert records["trackr-a"].id == first_id
        assert records["trackr-a"].application_url == (
            "https://jobs.example.com/verified-a"
        )
        assert records["trackr-a"].target_status == TargetKind.APPLICATION_ENTRY.value
        assert records["trackr-b"].id != first_id
        assert records["trackr-b"].application_url == "https://jobs.example.com/b"
        assert session.query(AutomationRun).count() == 0
        assert session.query(SubmissionIntent).count() == 0
        assert session.query(SubmissionAuthority).count() == 0


def test_conflicting_duplicate_id_fails_before_any_mutation(tmp_path: Path) -> None:
    settings, database, crypto = _setup(tmp_path)
    first = _row("trackr-a")
    conflicting = _row("trackr-a", role_title="Different role")
    with database.session_scope() as session:
        before_audits = session.query(AuditEvent).count()

        with pytest.raises(TrackrIdentityConflictError, match="trackr-a"):
            ScoutService(session, settings, crypto).ingest(
                [first, conflicting], default_cycle="2027"
            )

        assert session.query(Opportunity).count() == 0
        assert session.query(Application).count() == 0
        assert session.query(AuditEvent).count() == before_audits


def test_idless_live_saved_html_and_manual_rows_never_receive_fabricated_ids(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _setup(tmp_path)
    inputs = [
        _row(""),
        ScrapedOpportunity(
            employer="Saved Evidence",
            role_title="Industrial Placement 2027",
            source_url="https://saved.example.com/role",
            source="trackr_html",
        ),
        ScrapedOpportunity(
            employer="Manual Evidence",
            role_title="Industrial Placement 2027",
            source_url="https://manual.example.com/role",
            source="manual",
        ),
    ]

    with database.session_scope() as session:
        report = ScoutService(session, settings, crypto).ingest(
            inputs, default_cycle="2027"
        )
        records = list(session.scalars(select(Opportunity)).all())

        assert report["imported"] == 3
        assert report["trackr_ids_missing_or_invalid"] == 1
        assert all(record.trackr_programme_id is None for record in records)


def test_partial_unique_index_allows_nulls_and_rejects_duplicate_non_null(
    tmp_path: Path,
) -> None:
    settings, database, _crypto = _setup(tmp_path)
    with database.session_scope() as session:
        for suffix in ("one", "two"):
            session.add(
                Opportunity(
                    employer="Manual",
                    role_title=suffix,
                    cycle="2027",
                    url=f"https://example.com/{suffix}",
                    source="manual",
                    trackr_programme_id=None,
                )
            )
        session.flush()
        assert session.query(Opportunity).count() == 2

    with pytest.raises(IntegrityError):
        with database.session_scope() as session:
            session.add_all(
                [
                    Opportunity(
                        employer="Trackr",
                        role_title="A",
                        cycle="2027",
                        url="https://example.com/a",
                        source="trackr_live:test",
                        trackr_programme_id="same-id",
                    ),
                    Opportunity(
                        employer="Trackr",
                        role_title="B",
                        cycle="2027",
                        url="https://example.com/b",
                        source="trackr_live:test",
                        trackr_programme_id="same-id",
                    ),
                ]
            )
            session.flush()


def test_explicit_v7_to_current_migration_is_additive_and_idempotent(tmp_path: Path) -> None:
    settings, database, crypto = _setup(tmp_path)
    with database.session_scope() as session:
        ScoutService(session, settings, crypto).ingest([_row("")], default_cycle="2027")
        opportunity_id = session.query(Opportunity.id).scalar()
        application_id = session.query(Application.id).scalar()
    database.engine.dispose()

    database_path = settings.data_dir / "argus.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX uq_opportunities_trackr_programme_id")
        connection.execute("ALTER TABLE opportunities DROP COLUMN trackr_programme_id")
        connection.execute("PRAGMA user_version = 7")
        connection.commit()

    migrated = Database(settings)
    migrated.create_schema()
    migrated.create_schema()
    with migrated.engine.connect() as connection:
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("opportunities")
        }
        indexes = {
            index["name"]: index
            for index in inspect(connection).get_indexes("opportunities")
        }
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
        index_sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='uq_opportunities_trackr_programme_id'"
            )
        ).scalar_one()

    assert version == SCHEMA_VERSION
    assert "trackr_programme_id" in columns
    assert bool(indexes["uq_opportunities_trackr_programme_id"]["unique"]) is True
    assert (
        "where trackr_programme_id is not null"
        in " ".join(str(index_sql).casefold().split())
    )
    with migrated.session_scope() as session:
        assert session.query(Opportunity.id).scalar() == opportunity_id
        assert session.query(Application.id).scalar() == application_id
        assert session.query(Opportunity.trackr_programme_id).scalar() is None


def test_v8_migration_rejects_preexisting_duplicate_non_null_ids(tmp_path: Path) -> None:
    settings, database, _crypto = _setup(tmp_path)
    # Seed through the ORM so new required fields receive their real defaults;
    # raw SQL should introduce only the legacy identity conflict under test.
    with database.session_scope() as session:
        _seed_without_application(session, raw_id="trackr-a")
        _seed_without_application(session, raw_id="trackr-b")
    database.engine.dispose()
    database_path = settings.data_dir / "argus.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX uq_opportunities_trackr_programme_id")
        connection.execute(
            "UPDATE opportunities SET trackr_programme_id='duplicate-id'"
        )
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
        original_rows = connection.execute(
            "SELECT * FROM opportunities ORDER BY id"
        ).fetchall()
        assert len(original_rows) == 2

    with pytest.raises(RuntimeError, match="duplicate Trackr programme ID.*duplicate-id"):
        Database(settings).create_schema()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunities WHERE trackr_programme_id='duplicate-id'"
        ).fetchone()[0] == 2
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert connection.execute(
            "SELECT * FROM opportunities ORDER BY id"
        ).fetchall() == original_rows
        assert connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name='uq_opportunities_trackr_programme_id'"
        ).fetchone() is None


@pytest.mark.parametrize(
    "replacement_sql",
    [
        "CREATE INDEX uq_opportunities_trackr_programme_id "
        "ON opportunities(employer)",
        "CREATE UNIQUE INDEX uq_opportunities_trackr_programme_id "
        "ON opportunities(trackr_programme_id)",
        "CREATE UNIQUE INDEX uq_opportunities_trackr_programme_id "
        "ON opportunities(trackr_programme_id) "
        "WHERE trackr_programme_id IS NULL",
    ],
)
def test_v8_migration_rejects_wrong_same_named_identity_index_without_rewrite(
    tmp_path: Path,
    replacement_sql: str,
) -> None:
    settings, database, _crypto = _setup(tmp_path)
    database.engine.dispose()
    database_path = settings.data_dir / "argus.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX uq_opportunities_trackr_programme_id")
        connection.execute(replacement_sql)
        connection.execute("PRAGMA user_version = 7")
        original_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='uq_opportunities_trackr_programme_id'"
        ).fetchone()[0]
        connection.commit()

    with pytest.raises(RuntimeError, match="Trackr identity index.*mismatch"):
        Database(settings).create_schema()

    with sqlite3.connect(database_path) as connection:
        surviving_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='uq_opportunities_trackr_programme_id'"
        ).fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert surviving_sql == original_sql
    assert version == 7
