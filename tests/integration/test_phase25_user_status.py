from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.db import Database
from app.domain import states
from app.models import Application, AuditEvent, Opportunity


def _settings(tmp_path: Path) -> Settings:
    return Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_API_TOKEN": "phase25-test-token",
            "ARGUS_AUTOMATION_MODE": "OFF",
            "ARGUS_ENABLE_NOTIFICATIONS": "false",
        }
    )


def _opportunity(identifier: str = "phase25") -> Opportunity:
    return Opportunity(
        id=identifier,
        employer="Example Capital",
        role_title="Summer Analyst Programme",
        programme_group="summer",
        location="London",
        cycle="2027",
        url=f"https://example.com/jobs/{identifier}",
        application_url=f"https://example.com/apply/{identifier}",
        target_status="APPLICATION_FORM",
        resolved_ats_type="example",
        resolved_at=datetime.now(timezone.utc),
        application_window_status="OPEN",
    )


def test_user_application_status_has_exact_trackr_vocabulary_and_work_policy() -> None:
    status_type = getattr(states, "UserApplicationStatus")
    actor_type = getattr(states, "UserStatusActor")
    eligible = getattr(states, "user_status_is_automation_eligible")
    rank = getattr(states, "user_status_work_rank")

    assert tuple(item.value for item in status_type) == (
        "NOT_APPLIED",
        "INTERESTED",
        "NOT_INTERESTED",
        "APPLICATION_SUBMITTED",
        "ONLINE_ASSESSMENT",
        "HIREVUE",
        "ONLINE_TEST",
        "FIRST_ROUND",
        "OFFER",
        "REJECTED",
    )
    assert tuple(item.value for item in actor_type) == ("default", "user", "import")
    assert eligible("INTERESTED") is True
    assert eligible("NOT_APPLIED") is True
    assert eligible("APPLICATION_SUBMITTED") is False
    assert eligible("HIREVUE") is False
    assert eligible("NOT_INTERESTED") is False
    assert eligible("not-a-real-status") is False
    assert rank("INTERESTED") < rank("NOT_APPLIED") < rank("HIREVUE")


def test_opportunity_defaults_to_not_applied_and_status_masks_only_automation_url(
    tmp_path: Path,
) -> None:
    database = Database(_settings(tmp_path))
    database.create_schema()

    with database.session_scope() as session:
        record = _opportunity()
        assert record.user_status is None
        assert record.automation_url == "https://example.com/apply/phase25"
        session.add(record)
        session.flush()

        assert record.user_status == "NOT_APPLIED"
        assert record.user_status_actor == "default"
        assert record.user_status_updated_at is not None
        assert record.automation_url == "https://example.com/apply/phase25"

        record.user_status = "HIREVUE"
        session.flush()
        assert record.automation_url is None
        assert record.application_url == "https://example.com/apply/phase25"
        assert record.is_archived is False

        record.user_status = "INTERESTED"
        session.flush()
        assert record.automation_url == "https://example.com/apply/phase25"


def test_schema_v9_migration_is_additive_indexed_backfilled_and_idempotent(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    database = Database(settings)
    created_at = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with database.engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE opportunities (
                  id VARCHAR(36) NOT NULL PRIMARY KEY,
                  employer VARCHAR(240) NOT NULL,
                  role_title VARCHAR(320) NOT NULL,
                  division VARCHAR(240) NOT NULL DEFAULT '',
                  programme_group VARCHAR(120) NOT NULL DEFAULT '',
                  location VARCHAR(240) NOT NULL DEFAULT '',
                  cycle VARCHAR(40) NOT NULL,
                  url TEXT NOT NULL,
                  source_fingerprint VARCHAR(64) NOT NULL,
                  application_url TEXT,
                  target_status VARCHAR(40) NOT NULL DEFAULT 'UNRESOLVED',
                  resolved_ats_type VARCHAR(80) NOT NULL DEFAULT '',
                  resolution_evidence_json TEXT NOT NULL DEFAULT '{}',
                  resolved_at DATETIME,
                  resolution_attempted_at DATETIME,
                  source VARCHAR(120) NOT NULL DEFAULT 'manual',
                  ats_type VARCHAR(80) NOT NULL DEFAULT 'unknown',
                  deadline DATE,
                  rolling BOOLEAN NOT NULL DEFAULT 0,
                  min_graduation_year INTEGER,
                  max_graduation_year INTEGER,
                  sponsorship_supported BOOLEAN,
                  cv_required BOOLEAN NOT NULL DEFAULT 1,
                  cover_letter_required BOOLEAN NOT NULL DEFAULT 0,
                  written_answers_required BOOLEAN NOT NULL DEFAULT 0,
                  notes TEXT NOT NULL DEFAULT '',
                  created_at DATETIME NOT NULL,
                  updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO opportunities (
                  id, employer, role_title, programme_group, location, cycle,
                  url, source_fingerprint, created_at, updated_at
                ) VALUES (
                  'legacy-phase25', 'Legacy Capital', 'Legacy Internship',
                  'summer', 'London', '2027', 'https://example.com/legacy',
                  'legacy-fingerprint', :created_at, :created_at
                )
                """
            ),
            {"created_at": created_at},
        )
        connection.execute(text("PRAGMA user_version = 8"))

    database.create_schema()
    first_columns = {
        column["name"] for column in inspect(database.engine).get_columns("opportunities")
    }
    first_indexes = {
        index["name"] for index in inspect(database.engine).get_indexes("opportunities")
    }
    with database.engine.connect() as connection:
        first = connection.execute(
            text(
                "SELECT user_status, user_status_actor, user_status_updated_at "
                "FROM opportunities WHERE id='legacy-phase25'"
            )
        ).one()
        assert connection.execute(text("PRAGMA user_version")).scalar_one() == 9

    database.create_schema()
    with database.engine.connect() as connection:
        second = connection.execute(
            text(
                "SELECT user_status, user_status_actor, user_status_updated_at "
                "FROM opportunities WHERE id='legacy-phase25'"
            )
        ).one()
        assert connection.execute(text("SELECT COUNT(*) FROM opportunities")).scalar_one() == 1
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    assert {
        "user_status",
        "user_status_actor",
        "user_status_updated_at",
    } <= first_columns
    assert "ix_opportunities_user_status" in first_indexes
    assert first[0] == "NOT_APPLIED"
    assert first[1] == "default"
    assert first[2] is not None
    assert second == first


def test_explicit_status_change_is_audited_idempotent_and_never_starts_automation(
    tmp_path: Path,
) -> None:
    from app.security.audit import verify_audit_chain
    from app.services.user_statuses import set_user_status

    database = Database(_settings(tmp_path))
    database.create_schema()

    with database.session_scope() as session:
        record = _opportunity("audited")
        session.add(record)
        session.flush()

        first = set_user_status(session, record, "HIREVUE", actor="user")
        duplicate = set_user_status(session, record, "HIREVUE", actor="user")
        ignored_import = set_user_status(session, record, "OFFER", actor="import")

        assert first.changed is True
        assert duplicate.changed is False
        assert ignored_import.changed is False
        assert ignored_import.reason == "user_owned_status_preserved"
        assert record.user_status == "HIREVUE"
        assert record.user_status_actor == "user"
        assert record.automation_url is None

        verification = verify_audit_chain(session)
        assert verification.valid is True

    with database.session_scope() as session:
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "opportunity.user_status_changed"
            )
        ).all()

    assert len(events) == 1
    details = json.loads(events[0].details_json)
    assert events[0].actor == "user"
    assert details == {
        "application_state_changed": False,
        "automation_started": False,
        "new_actor": "user",
        "new_status": "HIREVUE",
        "old_actor": "default",
        "old_status": "NOT_APPLIED",
        "submission_started": False,
    }


def test_exact_status_import_reports_unmatched_and_ambiguous_without_guessing(
    tmp_path: Path,
) -> None:
    from app.services.user_statuses import (
        UserStatusImportRow,
        import_user_statuses,
    )

    database = Database(_settings(tmp_path))
    database.create_schema()
    rows = (
        UserStatusImportRow(
            source_line=1,
            status="Interested",
            employer="DE SHAW",
            programme_name="Trader / Analyst Intern (London) Summer 2027",
            programme_group="SUMMER",
        ),
        UserStatusImportRow(
            source_line=2,
            status="Not Interested",
            employer="Ambiguous & Co",
            programme_name="Internship Programme",
            programme_group="summer",
        ),
        UserStatusImportRow(
            source_line=3,
            status="Application Submitted",
            employer="Missing Employer",
            programme_name="Missing Programme",
            programme_group="summer",
        ),
    )

    with database.session_scope() as session:
        exact = _opportunity("exact")
        exact.employer = "D.E. Shaw"
        exact.role_title = "Trader/Analyst Intern (London) - Summer 2027"
        ambiguous_one = _opportunity("ambiguous-one")
        ambiguous_one.employer = "Ambiguous and Co."
        ambiguous_one.role_title = "Internship Programme"
        ambiguous_two = _opportunity("ambiguous-two")
        ambiguous_two.employer = "ambiguous & co"
        ambiguous_two.role_title = "Internship Programme"
        session.add_all((exact, ambiguous_one, ambiguous_two))
        session.flush()

        dry_run = import_user_statuses(session, rows, dry_run=True)
        assert dry_run.matched_count == 1
        assert dry_run.changed_count == 1
        assert dry_run.unmatched_count == 1
        assert dry_run.ambiguous_count == 1
        assert dry_run.unmatched_lines == (3,)
        assert dry_run.ambiguous_lines == (2,)
        assert exact.user_status == "NOT_APPLIED"

        applied = import_user_statuses(session, rows, dry_run=False)
        assert applied.matched_count == 1
        assert applied.changed_count == 1
        assert applied.unmatched_lines == (3,)
        assert applied.ambiguous_lines == (2,)
        assert exact.user_status == "INTERESTED"
        assert exact.user_status_actor == "import"
        assert ambiguous_one.user_status == "NOT_APPLIED"
        assert ambiguous_two.user_status == "NOT_APPLIED"

        repeated = import_user_statuses(session, rows, dry_run=False)
        assert repeated.matched_count == 1
        assert repeated.changed_count == 0
        assert repeated.unchanged_count == 1


def test_status_import_applies_case_whitespace_duplicate_role_to_every_copy(
    tmp_path: Path,
) -> None:
    """Catches regressing equivalent duplicate roles back to ambiguity."""

    from app.services.user_statuses import (
        UserStatusImportRow,
        import_user_statuses,
        set_user_status,
    )

    database = Database(_settings(tmp_path))
    database.create_schema()

    with database.session_scope() as session:
        first = _opportunity("case-copy-one")
        first.employer = "LionTree"
        first.role_title = "2027 Summer Internship Programme"
        second = _opportunity("case-copy-two")
        second.employer = "  liontree  "
        second.role_title = first.role_title
        second.url = first.url
        second.application_url = first.application_url
        first.source_fingerprint = "same-role-fingerprint"
        second.source_fingerprint = first.source_fingerprint
        session.add_all((first, second))
        session.flush()

        before_count = session.query(Opportunity).count()
        report = import_user_statuses(
            session,
            (
                UserStatusImportRow(
                    source_line=15,
                    status="Not Interested",
                    employer="LIONTREE",
                    programme_name=first.role_title,
                    programme_group="summer",
                ),
            ),
            dry_run=False,
        )

        assert report.matched_count == 1
        assert report.matched_candidate_count == 2
        assert report.changed_count == 2
        assert report.ambiguous_count == 0
        assert report.outcomes[0].reason == "equivalent_duplicate_role"
        assert tuple(
            candidate.opportunity_id for candidate in report.outcomes[0].candidates
        ) == ("case-copy-one", "case-copy-two")
        assert {first.user_status, second.user_status} == {"NOT_INTERESTED"}
        assert first.is_archived is False
        assert second.is_archived is False
        assert session.query(Opportunity).count() == before_count

        set_user_status(session, first, "INTERESTED", actor="user")
        set_user_status(session, second, "INTERESTED", actor="user")
        session.flush()
        assert first.automation_url == first.application_url
        assert second.automation_url == second.application_url
        assert session.query(Opportunity).count() == before_count


def test_status_import_keeps_different_roles_ambiguous_with_candidate_evidence(
    tmp_path: Path,
) -> None:
    """Catches unsafe coalescing of same-label roles in different locations."""

    from app.services.user_statuses import (
        UserStatusImportRow,
        import_user_statuses,
    )

    database = Database(_settings(tmp_path))
    database.create_schema()

    with database.session_scope() as session:
        london = _opportunity("same-label-london")
        london.employer = "Example & Co"
        london.role_title = "Quantitative Internship"
        new_york = _opportunity("same-label-new-york")
        new_york.employer = "Example and Co."
        new_york.role_title = london.role_title
        new_york.location = "New York"
        session.add_all((london, new_york))
        session.flush()

        report = import_user_statuses(
            session,
            (
                UserStatusImportRow(
                    source_line=88,
                    status="Application Submitted",
                    employer="Example & Co.",
                    programme_name=london.role_title,
                    programme_group="summer",
                ),
            ),
            dry_run=False,
        )

        assert report.matched_count == 0
        assert report.changed_count == 0
        assert report.ambiguous_lines == (88,)
        assert report.outcomes[0].reason == "different_role_candidates"
        assert tuple(
            candidate.opportunity_id for candidate in report.outcomes[0].candidates
        ) == ("same-label-london", "same-label-new-york")
        assert london.user_status == "NOT_APPLIED"
        assert new_york.user_status == "NOT_APPLIED"


def test_reviewed_duplicate_candidate_set_is_exact_and_fails_closed_on_drift(
    tmp_path: Path,
) -> None:
    """Catches extending a reviewed live decision to an unreviewed row."""

    from app.services.user_statuses import (
        UserStatusImportRow,
        import_user_statuses,
        status_import_cohort_sha256,
    )

    database = Database(_settings(tmp_path))
    database.create_schema()
    row = UserStatusImportRow(
        source_line=37,
        status="Not Interested",
        employer="Xantium",
        programme_name="Quantitative Researcher Internship",
        programme_group="summer",
    )
    with database.session_scope() as session:
        first = _opportunity("reviewed-one")
        first.employer = row.employer
        first.role_title = row.programme_name
        second = _opportunity("reviewed-two")
        second.employer = row.employer
        second.role_title = row.programme_name
        second.location = "New York"
        session.add_all((first, second))
        session.flush()
        attestation = status_import_cohort_sha256(row, (first, second))

        drifted = import_user_statuses(
            session,
            (row,),
            dry_run=False,
            reviewed_duplicate_candidates={37: (first.id,)},
        )
        assert drifted.ambiguous_lines == (37,)
        assert drifted.outcomes[0].reason == "reviewed_candidate_set_mismatch"
        assert {first.user_status, second.user_status} == {"NOT_APPLIED"}

        reviewed = import_user_statuses(
            session,
            (row,),
            dry_run=False,
            reviewed_duplicate_candidates={37: (first.id, second.id)},
            reviewed_duplicate_attestations={37: attestation},
        )
        assert reviewed.ambiguous_count == 0
        assert reviewed.changed_count == 2
        assert reviewed.outcomes[0].reason == "reviewed_duplicate_candidate_set"
        assert {first.user_status, second.user_status} == {"NOT_INTERESTED"}


def test_status_import_rejects_conflicting_duplicate_input_rows_before_mutation(
    tmp_path: Path,
) -> None:
    """Catches sequential rewrites from contradictory source rows."""

    from app.services.user_statuses import UserStatusImportRow, import_user_statuses

    database = Database(_settings(tmp_path))
    database.create_schema()
    with database.session_scope() as session:
        record = _opportunity("conflicting-source")
        session.add(record)
        session.flush()
        rows = (
            UserStatusImportRow(
                source_line=91,
                status="Interested",
                employer=record.employer,
                programme_name=record.role_title,
                programme_group=record.programme_group,
            ),
            UserStatusImportRow(
                source_line=92,
                status="Not Interested",
                employer=record.employer,
                programme_name=record.role_title,
                programme_group=record.programme_group,
            ),
        )

        report = import_user_statuses(session, rows, dry_run=False)

        assert report.changed_count == 0
        assert report.ambiguous_lines == (91, 92)
        assert tuple(outcome.reason for outcome in report.outcomes) == (
            "conflicting_import_statuses",
            "conflicting_import_statuses",
        )
        assert record.user_status == "NOT_APPLIED"


def test_import_never_overwrites_a_user_owned_status(tmp_path: Path) -> None:
    from app.services.user_statuses import (
        UserStatusImportRow,
        import_user_statuses,
        set_user_status,
    )

    database = Database(_settings(tmp_path))
    database.create_schema()

    with database.session_scope() as session:
        record = _opportunity("user-wins")
        session.add(record)
        session.flush()
        set_user_status(session, record, "OFFER", actor="user")

        report = import_user_statuses(
            session,
            (
                UserStatusImportRow(
                    source_line=99,
                    status="Rejected",
                    employer=record.employer,
                    programme_name=record.role_title,
                    programme_group=record.programme_group,
                ),
            ),
            dry_run=False,
        )

        assert report.matched_count == 1
        assert report.changed_count == 0
        assert report.user_owned_count == 1
        assert report.user_owned_lines == (99,)
        assert record.user_status == "OFFER"
        assert record.user_status_actor == "user"


def test_supplied_status_manifest_is_complete_and_preserves_the_reported_discrepancy() -> None:
    from scripts.import_phase25_statuses import SUPPLIED_STATUS_ROWS

    assert len(SUPPLIED_STATUS_ROWS) == 71
    assert Counter(row.programme_group for row in SUPPLIED_STATUS_ROWS) == {
        "summer": 60,
        "spring_week": 6,
        "year_in_industry": 5,
    }
    assert tuple(row.source_line for row in SUPPLIED_STATUS_ROWS) == tuple(range(1, 72))

    evidence = {
        (row.employer, row.programme_name, row.programme_group): row.status
        for row in SUPPLIED_STATUS_ROWS
    }
    assert evidence[
        (
            "Evercore",
            "2027 Private Funds Group - Industrial Placement",
            "year_in_industry",
        )
    ] == "HIREVUE"
    assert evidence[
        (
            "Goldman Sachs",
            "2027 One Year Work Placement",
            "year_in_industry",
        )
    ] == "NOT_APPLIED"
    assert evidence[
        ("Goldman Sachs", "2027 Summer Analyst Programme", "summer")
    ] == "NOT_APPLIED"


def test_status_import_runner_is_dry_by_default_backup_gated_and_idempotent(
    tmp_path: Path,
) -> None:
    from scripts.import_phase25_statuses import run_status_import

    data_dir = tmp_path / "live"
    data_dir.mkdir()
    database = Database(_settings(data_dir))
    database.create_schema()
    with database.session_scope() as session:
        record = _opportunity("manifest-match")
        record.employer = "Ares Management"
        record.role_title = "Summer Analyst Programme 2027"
        session.add(record)

    database.engine.dispose()
    database_path = data_dir / "argus.db"

    projected = run_status_import(database_path, apply=False)
    assert projected["mode"] == "dry_run"
    assert projected["report"]["matched"] == 1
    assert projected["report"]["changed"] == 1
    assert projected["excluded_by_status"] == 1
    assert projected["idempotency"]["changed"] == 0
    assert projected["backup"] is None
    with Database(_settings(data_dir)).session_scope() as session:
        assert session.get(Opportunity, "manifest-match").user_status == "NOT_APPLIED"

    applied = run_status_import(database_path, apply=True)
    assert applied["mode"] == "apply"
    assert applied["report"]["matched"] == 1
    assert applied["report"]["changed"] == 1
    assert applied["idempotency"]["changed"] == 0
    backup_path = data_dir / "backups" / str(applied["backup_file"])
    assert backup_path.is_file()
    assert applied["backup"] == f"<data-dir>/backups/{backup_path.name}"
    assert applied["rows_deleted"] == 0
    assert applied["submissions"] == 0
    assert applied["forms_filled"] == 0
    assert applied["statuses_guessed"] == 0
    with Database(_settings(data_dir)).session_scope() as session:
        record = session.get(Opportunity, "manifest-match")
        assert record.user_status == "APPLICATION_SUBMITTED"
        assert record.user_status_actor == "import"

    repeated = run_status_import(database_path, apply=True)
    assert repeated["report"]["changed"] == 0
    assert repeated["idempotency"]["changed"] == 0


def test_status_import_persists_a_privacy_safe_re_readable_candidate_report(
    tmp_path: Path,
) -> None:
    """Catches returning the match report only through ephemeral stdout."""

    from scripts.import_phase25_statuses import (
        load_status_import_report,
        run_status_import,
    )
    from scripts.privacy_scan import load_config, scan_artifact

    data_dir = tmp_path / "live"
    data_dir.mkdir()
    database = Database(_settings(data_dir))
    database.create_schema()
    with database.session_scope() as session:
        record = _opportunity("persisted-report-match")
        record.employer = "Ares Management"
        record.role_title = "Summer Analyst Programme 2027"
        session.add(record)
    database.engine.dispose()

    report_path = tmp_path / "evidence" / "phase25-status-report.json"
    result = run_status_import(
        data_dir / "argus.db",
        apply=False,
        report_path=report_path,
    )
    document = load_status_import_report(report_path)
    raw = report_path.read_text(encoding="utf-8")

    assert result["report_file"] == report_path.name
    assert document["schema_version"] == 3
    assert document["payload"]["mode"] == "dry_run"
    assert document["payload"]["report"]["outcomes"]
    assert document["payload_sha256"]
    assert document["envelope_sha256"]
    assert str(tmp_path) not in raw
    assert scan_artifact(
        report_path,
        load_config(Path(__file__).resolve().parents[2] / "packaging/privacy_scan_config.json"),
    ) == []

    tampered = json.loads(raw)
    tampered["payload"]["excluded_by_status"] = -1
    report_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="payload digest mismatch"):
        load_status_import_report(report_path)


def test_status_import_retries_only_database_locked_failures(monkeypatch) -> None:
    """Catches dropping the required bounded SQLite lock retry."""

    from scripts import import_phase25_statuses as status_script

    attempts = 0

    def flaky_operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError(
                "UPDATE opportunities",
                {},
                sqlite_error("database is locked"),
            )
        return "ok"

    def sqlite_error(message: str) -> Exception:
        return Exception(message)

    assert status_script.retry_database_locked(
        flaky_operation,
        attempts=3,
        retry_delay=0,
    ) == "ok"
    assert attempts == 3

    with pytest.raises(OperationalError):
        status_script.retry_database_locked(
            lambda: (_ for _ in ()).throw(
                OperationalError(
                    "UPDATE opportunities",
                    {},
                    sqlite_error("disk I/O error"),
                )
            ),
            attempts=3,
            retry_delay=0,
        )


def test_automatic_duplicate_rule_compares_every_immutable_opportunity_column(
    tmp_path: Path,
) -> None:
    """Catches identity fields being omitted from automatic coalescence."""

    from app.services.user_statuses import (
        STATUS_IMPORT_ATTESTATION_EXCLUDED_COLUMNS,
        UserStatusImportRow,
        import_user_statuses,
        status_import_attestation_columns,
    )

    expected_columns = {
        column.name for column in Opportunity.__table__.columns
    } - set(STATUS_IMPORT_ATTESTATION_EXCLUDED_COLUMNS)
    assert set(status_import_attestation_columns()) == expected_columns

    database = Database(_settings(tmp_path))
    database.create_schema()
    with database.session_scope() as session:
        first = _opportunity("identity-one")
        first.employer = "Case Employer"
        first.source_fingerprint = "same-fingerprint"
        first.trackr_programme_id = "summer-role-one"
        second = _opportunity("identity-two")
        second.employer = " case employer "
        second.url = first.url
        second.application_url = first.application_url
        second.source_fingerprint = first.source_fingerprint
        second.trackr_programme_id = "different-role"
        session.add_all((first, second))
        session.flush()

        report = import_user_statuses(
            session,
            (
                UserStatusImportRow(
                    source_line=101,
                    status="Not Interested",
                    employer="CASE EMPLOYER",
                    programme_name=first.role_title,
                    programme_group=first.programme_group,
                ),
            ),
            dry_run=False,
        )

        assert report.ambiguous_lines == (101,)
        assert report.changed_count == 0
        assert {first.user_status, second.user_status} == {"NOT_APPLIED"}


def test_reviewed_attestation_rejects_same_uuid_attribute_drift(tmp_path: Path) -> None:
    """Catches a reviewed UUID silently changing role identity in place."""

    from app.services.user_statuses import (
        UserStatusImportRow,
        import_user_statuses,
        status_import_cohort_sha256,
    )

    database = Database(_settings(tmp_path))
    database.create_schema()
    row = UserStatusImportRow(
        source_line=37,
        status="Not Interested",
        employer="Xantium",
        programme_name="Quantitative Researcher Internship",
        programme_group="summer",
    )
    with database.session_scope() as session:
        first = _opportunity("attested-one")
        first.employer = row.employer
        first.role_title = row.programme_name
        second = _opportunity("attested-two")
        second.employer = row.employer
        second.role_title = row.programme_name
        second.location = "New York"
        session.add_all((first, second))
        session.flush()
        expected = status_import_cohort_sha256(row, (first, second))

        second.trackr_programme_id = "same-id-new-identity"
        session.flush()
        report = import_user_statuses(
            session,
            (row,),
            dry_run=False,
            reviewed_duplicate_candidates={37: (first.id, second.id)},
            reviewed_duplicate_attestations={37: expected},
        )

        assert report.ambiguous_lines == (37,)
        assert report.outcomes[0].reason == "reviewed_candidate_attribute_drift"
        assert report.changed_count == 0
        assert {first.user_status, second.user_status} == {"NOT_APPLIED"}


def test_historical_status_report_loads_with_explicit_manifest_mismatch(
    tmp_path: Path,
) -> None:
    """Catches making valid historical evidence unreadable after source changes."""

    from scripts.import_phase25_statuses import (
        load_status_import_report,
        save_status_import_report,
    )

    path = tmp_path / "historical.json"
    save_status_import_report(path, {"mode": "historical"})
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("envelope_sha256")
    document["schema_version"] = 1
    document["manifest_sha256"] = "0" * 64
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded = load_status_import_report(path)
    assert loaded["payload"] == {"mode": "historical"}
    assert loaded["verification"]["manifest_matches_current"] is False
    with pytest.raises(ValueError, match="manifest digest mismatch"):
        load_status_import_report(path, require_current_manifest=True)


def test_database_safety_metrics_detect_replacement_submission_and_form_run(
    tmp_path: Path,
) -> None:
    """Catches safety totals being hard-coded or based only on net counts."""

    from app.models import AutomationRun
    from scripts.import_phase25_statuses import (
        _database_safety_snapshot,
        _safety_metrics,
    )

    database = Database(_settings(tmp_path))
    database.create_schema()
    with database.session_scope() as session:
        original = _opportunity("safety-original")
        application = Application(
            id="safety-application",
            opportunity=original,
            state="PACKAGE_PREPARED",
        )
        session.add(application)
    database.engine.dispose()
    database_path = tmp_path / "argus.db"
    before = _database_safety_snapshot(database_path)

    database = Database(_settings(tmp_path))
    with database.session_scope() as session:
        original = session.get(Opportunity, "safety-original")
        session.execute(
            text("DELETE FROM applications WHERE id = 'safety-application'")
        )
        session.delete(original)
        session.flush()
        replacement = _opportunity("safety-replacement")
        replacement_application = Application(
            id="replacement-application",
            opportunity=replacement,
            state="SUBMITTED",
            submission_reference="observed-submission",
            applied_at=datetime.now(timezone.utc),
        )
        session.add(replacement_application)
        session.flush()
        session.add(
            AutomationRun(
                id="observed-form-run",
                application_id=replacement_application.id,
                mode="REVIEW_ONLY",
            )
        )
    database.engine.dispose()

    metrics = _safety_metrics(before, _database_safety_snapshot(database_path))
    assert metrics["rows_deleted"] >= 1
    assert metrics["submissions"] >= 1
    assert metrics["forms_filled"] >= 1


@pytest.mark.parametrize("mode", ("REVIEW_ONLY", "DRY_RUN"))
def test_form_safety_metric_detects_modified_supported_run_modes(
    tmp_path: Path,
    mode: str,
) -> None:
    from app.models import AutomationRun
    from scripts.import_phase25_statuses import (
        _database_safety_snapshot,
        _safety_metrics,
    )

    database = Database(_settings(tmp_path))
    database.create_schema()
    with database.session_scope() as session:
        opportunity = _opportunity(f"form-mode-{mode.casefold()}")
        application = Application(
            id=f"form-mode-application-{mode.casefold()}",
            opportunity=opportunity,
            state="PACKAGE_PREPARED",
        )
        session.add(
            AutomationRun(
                id=f"form-mode-run-{mode.casefold()}",
                application=application,
                mode=mode,
                state="QUEUED",
            )
        )
    database.engine.dispose()
    database_path = tmp_path / "argus.db"
    before = _database_safety_snapshot(database_path)

    database = Database(_settings(tmp_path))
    with database.session_scope() as session:
        run = session.get(AutomationRun, f"form-mode-run-{mode.casefold()}")
        run.state = "RUNNING"
    database.engine.dispose()

    metrics = _safety_metrics(before, _database_safety_snapshot(database_path))
    assert metrics["forms_filled"] >= 1


def test_report_publication_failure_preserves_checkpoint_and_final_stage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Catches losing all report evidence after a committed import."""

    from scripts import import_phase25_statuses as status_script

    destination = tmp_path / "status-report.json"
    status_script.reserve_status_import_report(
        destination,
        {"mode": "apply", "commit_state": "prepared"},
    )

    def fail_publish(_source, _destination) -> None:  # noqa: ANN001
        raise OSError("injected publication failure")

    monkeypatch.setattr(status_script, "_atomic_replace", fail_publish)
    with pytest.raises(status_script.ReportPublicationError) as captured:
        status_script.replace_status_import_report(
            destination,
            {"mode": "apply", "commit_state": "verified"},
        )

    assert status_script.load_status_import_report(destination)["payload"][
        "commit_state"
    ] == "prepared"
    stage = captured.value.staged_report_path
    assert stage.is_file()
    assert status_script.load_status_import_report(stage)["payload"][
        "commit_state"
    ] == "verified"


def test_reviewed_attestation_binds_identity_but_allows_status_side_effects(
    tmp_path: Path,
) -> None:
    from app.services.user_statuses import (
        UserStatusImportRow,
        status_import_candidate_sha256,
        status_import_cohort_sha256,
    )

    database = Database(_settings(tmp_path))
    database.create_schema()
    with database.session_scope() as session:
        row = UserStatusImportRow(
            source_line=201,
            status="Interested",
            employer="Attested Employer",
            programme_name="Attested Role",
            programme_group="summer",
        )
        record = _opportunity("attestation-status-side-effects")
        record.employer = row.employer
        record.role_title = row.programme_name
        session.add(record)
        session.flush()
        candidate_before = status_import_candidate_sha256(record)
        cohort_before = status_import_cohort_sha256(row, (record,))

        record.user_status = "INTERESTED"
        record.user_status_actor = "import"
        record.user_status_updated_at = datetime.now(timezone.utc)
        record.updated_at = datetime.now(timezone.utc)
        assert status_import_candidate_sha256(record) == candidate_before
        assert status_import_cohort_sha256(row, (record,)) == cohort_before

        changed_row = UserStatusImportRow(
            source_line=row.source_line,
            status="Not Interested",
            employer=row.employer,
            programme_name=row.programme_name,
            programme_group=row.programme_group,
        )
        assert status_import_cohort_sha256(changed_row, (record,)) != cohort_before
        record.target_status = "JOB_DETAIL"
        assert status_import_candidate_sha256(record) != candidate_before


def test_reviewed_cohort_cannot_fall_back_to_unique_after_candidate_deletion(
    tmp_path: Path,
) -> None:
    from app.services.user_statuses import (
        UserStatusImportRow,
        import_user_statuses,
        status_import_cohort_sha256,
    )

    database = Database(_settings(tmp_path))
    database.create_schema()
    with database.session_scope() as session:
        row = UserStatusImportRow(
            source_line=202,
            status="Not Interested",
            employer="Reviewed Employer",
            programme_name="Reviewed Role",
            programme_group="summer",
        )
        first = _opportunity("reviewed-cardinality-one")
        second = _opportunity("reviewed-cardinality-two")
        for record in (first, second):
            record.employer = row.employer
            record.role_title = row.programme_name
        session.add_all((first, second))
        session.flush()
        attestation = status_import_cohort_sha256(row, (first, second))
        session.delete(second)
        session.flush()

        report = import_user_statuses(
            session,
            (row,),
            dry_run=False,
            reviewed_duplicate_candidates={202: (first.id, second.id)},
            reviewed_duplicate_attestations={202: attestation},
        )

        assert report.ambiguous_lines == (202,)
        assert report.outcomes[0].reason == "reviewed_candidate_set_mismatch"
        assert first.user_status == "NOT_APPLIED"


def test_reviewed_attestation_precedes_automatic_duplicate_fallback(
    tmp_path: Path,
) -> None:
    from app.services.user_statuses import UserStatusImportRow, import_user_statuses

    database = Database(_settings(tmp_path))
    database.create_schema()
    with database.session_scope() as session:
        first = _opportunity("configured-equivalent-one")
        second = _opportunity("configured-equivalent-two")
        first.employer = "Configured Employer"
        second.employer = " configured employer "
        second.url = first.url
        second.application_url = first.application_url
        session.add_all((first, second))
        session.flush()
        row = UserStatusImportRow(
            source_line=203,
            status="Not Interested",
            employer="CONFIGURED EMPLOYER",
            programme_name=first.role_title,
            programme_group=first.programme_group,
        )

        report = import_user_statuses(
            session,
            (row,),
            dry_run=False,
            reviewed_duplicate_candidates={203: (first.id, second.id)},
            reviewed_duplicate_attestations={203: "0" * 64},
        )

        assert report.ambiguous_lines == (203,)
        assert report.outcomes[0].reason == "reviewed_candidate_attribute_drift"
        assert {first.user_status, second.user_status} == {"NOT_APPLIED"}


def test_candidate_report_uses_digests_instead_of_raw_destination_evidence(
    tmp_path: Path,
) -> None:
    from app.services.user_statuses import (
        UserStatusImportRow,
        import_user_statuses,
        status_import_cohort_sha256,
    )
    from scripts.import_phase25_statuses import _report_dict

    database = Database(_settings(tmp_path))
    database.create_schema()
    with database.session_scope() as session:
        row = UserStatusImportRow(
            source_line=204,
            status="Not Interested",
            employer="Privacy Employer",
            programme_name="Privacy Role",
            programme_group="summer",
        )
        records = []
        for suffix in ("one", "two"):
            record = _opportunity(f"privacy-{suffix}")
            record.employer = row.employer
            record.role_title = row.programme_name
            record.url = f"https://secret.invalid/{suffix}/listing"
            record.application_url = f"https://secret.invalid/{suffix}/apply"
            record.resolution_evidence_json = f'{{"private":"evidence-{suffix}"}}'
            record.notes = f"private-note-{suffix}"
            records.append(record)
        session.add_all(records)
        session.flush()
        attestation = status_import_cohort_sha256(row, records)
        report = import_user_statuses(
            session,
            (row,),
            dry_run=True,
            reviewed_duplicate_candidates={204: tuple(record.id for record in records)},
            reviewed_duplicate_attestations={204: attestation},
        )
        raw = json.dumps(_report_dict(report), sort_keys=True)

        assert "secret.invalid" not in raw
        assert "private-note" not in raw
        assert "private\\\":\\\"evidence" not in raw
        assert '"identity_sha256"' in raw
        assert '"destination_sha256"' in raw


def test_manifest_digest_binds_reviewed_attestations_and_unknown_schema_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import import_phase25_statuses as status_script

    original = status_script._manifest_sha256()
    monkeypatch.setitem(
        status_script.REVIEWED_DUPLICATE_ATTESTATIONS,
        1,
        "0" * 64,
    )
    assert status_script._manifest_sha256() != original

    envelope_path = tmp_path / "tampered-envelope.json"
    status_script.save_status_import_report(envelope_path, {"mode": "test"})
    envelope_document = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope_document["manifest_sha256"] = "f" * 64
    envelope_path.write_text(json.dumps(envelope_document), encoding="utf-8")
    with pytest.raises(ValueError, match="envelope digest mismatch"):
        status_script.load_status_import_report(envelope_path)

    path = tmp_path / "unknown-schema.json"
    status_script.save_status_import_report(path, {"mode": "test"})
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("envelope_sha256")
    document["schema_version"] = 99
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported status import report schema"):
        status_script.load_status_import_report(path)


def test_run_once_rejects_non_status_mutation_before_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import import_phase25_statuses as status_script

    database = Database(_settings(tmp_path))
    database.create_schema()
    with database.session_scope() as session:
        record = _opportunity("precommit-scope")
        record.employer = "Ares Management"
        record.role_title = "Summer Analyst Programme 2027"
        session.add(
            Application(
                id="precommit-scope-application",
                opportunity=record,
                state="PACKAGE_PREPARED",
            )
        )
    database.engine.dispose()
    real_import = status_script.import_user_statuses

    def mutate_application(session, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        report = real_import(session, *args, **kwargs)
        application = session.get(Application, "precommit-scope-application")
        application.state = "SUBMITTED"
        return report

    monkeypatch.setattr(status_script, "import_user_statuses", mutate_application)
    with pytest.raises(RuntimeError, match="non-opportunity"):
        status_script._run_once(tmp_path / "argus.db", dry_run=False)

    with Database(_settings(tmp_path)).session_scope() as session:
        record = session.get(Opportunity, "precommit-scope")
        application = session.get(Application, "precommit-scope-application")
        assert record.user_status == "NOT_APPLIED"
        assert application.state == "PACKAGE_PREPARED"


def test_apply_reserves_prepared_checkpoint_before_live_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import import_phase25_statuses as status_script

    database = Database(_settings(tmp_path))
    database.create_schema()
    with database.session_scope() as session:
        record = _opportunity("checkpoint-before-write")
        record.employer = "Ares Management"
        record.role_title = "Summer Analyst Programme 2027"
        session.add(record)
    database.engine.dispose()
    database_path = (tmp_path / "argus.db").resolve()
    report_path = tmp_path / "checkpoint.json"
    real_run_once = status_script._run_once

    def fail_live_write(path: Path, *, dry_run: bool):
        if path.resolve() == database_path and not dry_run:
            raise RuntimeError("injected pre-write failure")
        return real_run_once(path, dry_run=dry_run)

    monkeypatch.setattr(status_script, "_run_once", fail_live_write)
    with pytest.raises(RuntimeError, match="injected pre-write failure"):
        status_script.run_status_import(
            database_path,
            apply=True,
            report_path=report_path,
        )

    checkpoint = status_script.load_status_import_report(report_path)
    assert checkpoint["payload"]["commit_state"] == "prepared"
    assert checkpoint["payload"]["backup_sha256"]
    with Database(_settings(tmp_path)).session_scope() as session:
        assert session.get(Opportunity, "checkpoint-before-write").user_status == (
            "NOT_APPLIED"
        )


def test_apply_holds_runtime_lock_through_final_report_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import import_phase25_statuses as status_script

    database = Database(_settings(tmp_path))
    database.create_schema()
    with database.session_scope() as session:
        record = _opportunity("lock-through-publication")
        record.employer = "Ares Management"
        record.role_title = "Summer Analyst Programme 2027"
        session.add(record)
    database.engine.dispose()

    state = {"active": False, "released": False}

    class FakeLock:
        def release(self) -> None:
            assert state["active"] is True
            state["active"] = False
            state["released"] = True

    def acquire(*_args, **_kwargs):
        state["active"] = True
        return FakeLock()

    real_snapshot = status_script._database_safety_snapshot
    real_replace = status_script._atomic_replace

    def locked_snapshot(path: Path):
        assert state["active"] is True
        return real_snapshot(path)

    def locked_replace(source: Path, destination: Path) -> None:
        assert state["active"] is True
        real_replace(source, destination)

    monkeypatch.setattr(status_script, "_acquire_with_retries", acquire)
    monkeypatch.setattr(status_script, "_database_safety_snapshot", locked_snapshot)
    monkeypatch.setattr(status_script, "_atomic_replace", locked_replace)
    result = status_script.run_status_import(
        tmp_path / "argus.db",
        apply=True,
        report_path=tmp_path / "locked-report.json",
    )

    assert result["commit_state"] == "verified"
    assert state == {"active": False, "released": True}
