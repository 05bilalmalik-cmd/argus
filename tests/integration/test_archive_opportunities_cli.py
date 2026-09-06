from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import cli
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, AuditEvent, Opportunity


def _archive_model():
    from app import models

    model = getattr(models, "OpportunityArchive", None)
    if model is None:
        pytest.fail("Phase 11 OpportunityArchive model is not implemented")
    return model


def _seed_database(tmp_path: Path) -> Database:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    locations = (
        ("blank", ""),
        ("uk-multi", "London; Amsterdam"),
        ("uk-global", "New York, London, or Paris"),
        ("chicago", "Chicago, IL"),
        ("hong-kong", "Hong Kong"),
        ("madrid", "Madrid, Spain"),
    )
    with database.session_scope() as session:
        for identifier, location in locations:
            opportunity = Opportunity(
                id=identifier,
                employer=f"Employer {identifier}",
                role_title=f"Role {identifier}",
                programme_group="summer",
                location=location,
                cycle="2027",
                url=f"https://{identifier}.example.test/jobs/role",
                source="phase11_cli_test",
            )
            session.add(opportunity)
            session.flush()
            session.add(
                Application(
                    id=f"application-{identifier}",
                    opportunity_id=opportunity.id,
                    state=ApplicationState.DISCOVERED.value,
                )
            )
    return database


def _counts(database: Database) -> tuple[int, int, int, int]:
    archive_model = _archive_model()
    with database.session_scope() as session:
        return (
            int(session.scalar(select(func.count()).select_from(Opportunity)) or 0),
            int(session.scalar(select(func.count()).select_from(Application)) or 0),
            int(session.scalar(select(func.count()).select_from(archive_model)) or 0),
            int(
                session.scalar(
                    select(func.count())
                    .select_from(archive_model)
                    .where(archive_model.archived_at.is_not(None))
                )
                or 0
            ),
        )


def _assert_sqlite_integrity(path: Path) -> None:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_archive_parser_is_dry_run_by_default_and_requires_one_action() -> None:
    parser = cli.build_parser()

    args = parser.parse_args(["archive-opportunities", "--non-uk"])

    assert args.non_uk is True
    assert args.unarchive is False
    assert args.apply is False
    with pytest.raises(SystemExit):
        parser.parse_args(["archive-opportunities"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["archive-opportunities", "--non-uk", "--unarchive"]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["archive-opportunities", "--non-uk", "--dry-run", "--apply"]
        )


def test_archive_cli_is_reversible_idempotent_audited_and_never_deletes_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive_model = _archive_model()
    database = _seed_database(tmp_path)
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    initial_counts = _counts(database)
    assert initial_counts == (6, 6, 0, 0)
    database_path = tmp_path / "argus.db"
    dry_run_fingerprint = cli._sqlite_file_fingerprint(database_path)

    assert cli.main(["archive-opportunities", "--non-uk"]) == 0
    dry_run_output = capsys.readouterr().out
    assert "Mode: DRY RUN (no database changes written)" in dry_run_output
    assert "Rows selected: 3" in dry_run_output
    assert "chicago | Employer chicago | Role chicago | Chicago, IL" in dry_run_output
    assert "hong-kong | Employer hong-kong | Role hong-kong | Hong Kong" in dry_run_output
    assert "madrid | Employer madrid | Role madrid | Madrid, Spain" in dry_run_output
    assert "blank |" not in dry_run_output
    assert "uk-multi |" not in dry_run_output
    assert "uk-global |" not in dry_run_output
    assert _counts(database) == initial_counts
    assert cli._sqlite_file_fingerprint(database_path) == dry_run_fingerprint
    assert not (tmp_path / "backups").exists()

    assert cli.main(["archive-opportunities", "--non-uk", "--apply"]) == 0
    apply_output = capsys.readouterr().out
    assert "Mode: APPLIED" in apply_output
    assert "Rows selected: 3" in apply_output
    assert "Opportunity rows: 6 -> 6" in apply_output
    assert "Application rows: 6 -> 6" in apply_output
    assert "Active archives: 0 -> 3" in apply_output
    assert _counts(database) == (6, 6, 3, 3)
    backups = sorted((tmp_path / "backups").glob("*.bak"))
    assert len(backups) == 1
    assert f"Backup: {backups[0]}" in apply_output
    _assert_sqlite_integrity(backups[0])

    with database.session_scope() as session:
        events = list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == "opportunity.archived")
                .order_by(AuditEvent.entity_id)
            ).all()
        )
        assert len(events) == 3
        assert {event.entity_id for event in events} == {
            "chicago",
            "hong-kong",
            "madrid",
        }
        assert all(
            json.loads(event.details_json)["reason"] == "non_uk_location"
            for event in events
        )

    assert cli.main(["archive-opportunities", "--non-uk", "--apply"]) == 0
    second_output = capsys.readouterr().out
    assert "Rows selected: 0" in second_output
    assert _counts(database) == (6, 6, 3, 3)
    assert sorted((tmp_path / "backups").glob("*.bak")) == backups

    assert cli.main(["archive-opportunities", "--unarchive", "--apply"]) == 0
    unarchive_output = capsys.readouterr().out
    assert "Rows selected: 3" in unarchive_output
    assert "Opportunity rows: 6 -> 6" in unarchive_output
    assert "Application rows: 6 -> 6" in unarchive_output
    assert "Active archives: 3 -> 0" in unarchive_output
    assert _counts(database) == (6, 6, 3, 0)
    backups_after_unarchive = sorted((tmp_path / "backups").glob("*.bak"))
    assert len(backups_after_unarchive) == 2
    _assert_sqlite_integrity(backups_after_unarchive[-1])
    with database.session_scope() as session:
        rows = list(session.scalars(select(archive_model)).all())
        assert len(rows) == 3
        assert all(row.archived_at is None for row in rows)
        assert all(row.archived_reason is None for row in rows)
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "opportunity.unarchived")
        ) == 3

    assert cli.main(["archive-opportunities", "--unarchive", "--apply"]) == 0
    final_output = capsys.readouterr().out
    assert "Rows selected: 0" in final_output
    assert _counts(database) == (6, 6, 3, 0)
    assert sorted((tmp_path / "backups").glob("*.bak")) == backups_after_unarchive


def test_archive_apply_holds_sqlite_write_lock_across_validation_and_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _seed_database(tmp_path)
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    original_plan = cli._archive_plan_from_session
    lock_observed = False

    def plan_while_competing_writer_attempts(session, *, unarchive):  # noqa: ANN001
        nonlocal lock_observed
        with sqlite3.connect(
            tmp_path / "argus.db",
            timeout=0,
            isolation_level=None,
        ) as competing_writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing_writer.execute(
                    "UPDATE opportunities SET location='London' WHERE id='chicago'"
                )
            lock_observed = True
        return original_plan(session, unarchive=unarchive)

    monkeypatch.setattr(cli, "_archive_plan_from_session", plan_while_competing_writer_attempts)

    assert cli.main(["archive-opportunities", "--non-uk", "--apply"]) == 0
    assert lock_observed is True
    assert _counts(database) == (6, 6, 3, 3)
