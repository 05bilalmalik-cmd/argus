from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cli import (
    _create_verified_programme_backup,
    _programme_from_retained_evidence,
)
from app.config import Settings
from app.db import Database
from app.models import AuditEvent, AuditOutbox, Opportunity
from app.security.audit import AuditInput, append_audit


ROOT = Path(__file__).resolve().parents[2]


def _run_cli(data_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["ARGUS_DATA_DIR"] = str(data_dir)
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _opportunity(
    employer: str,
    role_title: str,
    programme_group: str,
    source: str,
    *,
    url: str | None = None,
) -> Opportunity:
    return Opportunity(
        employer=employer,
        role_title=role_title,
        programme_group=programme_group,
        cycle="2027",
        url=url or f"https://jobs.example.test/{employer.casefold().replace(' ', '-')}",
        source=source,
        ats_type="custom",
    )


def _seed_database(data_dir: Path) -> tuple[Database, Path]:
    settings = Settings.load({"ARGUS_DATA_DIR": str(data_dir)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        session.add_all(
            [
                _opportunity(
                    "Exact Placement",
                    "Deliberately misleading Summer Internship",
                    "summer",
                    "trackr_live:industrial-placements",
                ),
                _opportunity(
                    "Exact Off Cycle",
                    "Deliberately misleading Placement Year",
                    "year_in_industry",
                    "trackr_live:off-cycle-internships",
                ),
                _opportunity(
                    "Exact Tracker URL",
                    "Deliberately unclassified role",
                    "other",
                    "trackr_live",
                    url="https://app.the-trackr.com/uk-finance/spring-weeks/",
                ),
                _opportunity(
                    "Lost Trackr Provenance",
                    "Summer Internship",
                    "legacy_unknown",
                    "trackr_live",
                    url="https://jobs.example.test/lost-trackr-row",
                ),
                _opportunity(
                    "Heuristic Summer",
                    "Summer Analyst Internship",
                    "other",
                    "csv_import",
                ),
                _opportunity(
                    "Heuristic Other",
                    "Graduate Analyst",
                    "summer",
                    "manual_entry",
                ),
            ]
        )

    with Session(database.engine) as session:
        append_audit(
            session,
            AuditInput(
                "test",
                "pending.before_programme_backfill",
                "fixture",
                "pending-1",
                {"safe": True},
            ),
        )
        session.commit()
    database.engine.dispose()
    database_path = data_dir / "argus.db"
    with sqlite3.connect(database_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    assert str(journal_mode).casefold() == "delete"
    return database, database_path


def _classifications(database_path: Path) -> dict[str, str]:
    with sqlite3.connect(database_path) as connection:
        return dict(
            connection.execute(
                "SELECT employer, programme_group FROM opportunities ORDER BY employer"
            ).fetchall()
        )


def _assert_integrity(database_path: Path) -> None:
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _file_snapshot(path: Path) -> tuple[bool, bytes]:
    return path.exists(), path.read_bytes() if path.exists() else b""


def test_apply_prints_verified_backup_before_later_database_failure(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "argus.db"
    tmp_path.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

    result = _run_cli(tmp_path, "reclassify-programmes")

    assert result.returncode == 1
    backups = list((tmp_path / "backups").glob("*.bak"))
    assert len(backups) == 1
    _assert_integrity(backups[0])
    assert f"Backup: {backups[0]}" in result.stdout


def test_failed_backup_removes_only_its_exclusively_created_artifact(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "argus.db"
    database_path.write_bytes(b"not a SQLite database")
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    existing_backup = backup_directory / "existing.bak"
    existing_backup.write_bytes(b"preserve this existing backup")

    with pytest.raises(sqlite3.DatabaseError):
        _create_verified_programme_backup(database_path, backup_directory)

    assert {
        path.name: path.read_bytes() for path in backup_directory.iterdir()
    } == {"existing.bak": b"preserve this existing backup"}


def test_dry_run_reads_uncheckpointed_wal_without_touching_database_sidecars(
    tmp_path: Path,
) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    database_path = tmp_path / "argus.db"
    wal_path = Path(f"{database_path}-wal")
    shm_path = Path(f"{database_path}-shm")

    with database.engine.connect() as keepalive:
        keepalive.exec_driver_sql("PRAGMA wal_autocheckpoint=0")
        keepalive.commit()
        keepalive.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        keepalive.commit()
        with Session(bind=keepalive) as session:
            session.add(
                _opportunity(
                    "Uncheckpointed WAL Row",
                    "Summer Analyst Internship",
                    "other",
                    "manual_entry",
                )
            )
            session.commit()

        before = {
            path: _file_snapshot(path)
            for path in (database_path, wal_path, shm_path)
        }
        assert before[wal_path][0] is True
        assert before[wal_path][1]
        assert before[shm_path][0] is True

        result = _run_cli(tmp_path, "reclassify-programmes", "--dry-run")

        assert result.returncode == 0, result.stderr
        assert "Rows changed: 1" in result.stdout
        assert "After programme_group histogram:" in result.stdout
        assert "  summer: 1" in result.stdout
        assert {
            path: _file_snapshot(path)
            for path in (database_path, wal_path, shm_path)
        } == before

    database.engine.dispose()


@pytest.mark.parametrize(
    ("source", "url"),
    [
        (
            "manual_entry",
            "http://app.the-trackr.com/uk-finance/spring-weeks",
        ),
        (
            "manual_entry",
            "https://app.the-trackr.example/uk-finance/spring-weeks",
        ),
        (
            "manual_entry",
            "https://app.the-trackr.com:443/uk-finance/spring-weeks",
        ),
        (
            "manual_entry",
            "https://user" "@app.the-trackr.com/uk-finance/spring-weeks",
        ),
        (
            "manual_entry",
            "https://app.the-trackr.com/uk-finance/spring-weeks/archive",
        ),
        (
            "trackr_live:autumn-internships",
            "https://jobs.example.test/unmapped-trackr-slug",
        ),
        (
            "trackr_lively:industrial-placements",
            "https://jobs.example.test/unrelated-near-prefix",
        ),
    ],
)
def test_near_match_trackr_evidence_uses_non_authoritative_title_fallback(
    source: str,
    url: str,
) -> None:
    opportunity = _opportunity(
        "Boundary Employer",
        "Summer Analyst Internship",
        "spring_week",
        source,
        url=url,
    )

    programme_group, unrecoverable = _programme_from_retained_evidence(opportunity)

    assert programme_group == "summer"
    assert unrecoverable is False


def test_reclassify_programmes_dry_run_is_read_only_and_reports_honest_plan(
    tmp_path: Path,
) -> None:
    _database, database_path = _seed_database(tmp_path)
    before = _classifications(database_path)
    bytes_before = database_path.read_bytes()

    result = _run_cli(tmp_path, "reclassify-programmes", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "Mode: DRY RUN (no database changes written)" in result.stdout
    assert "Before programme_group histogram:" in result.stdout
    assert "  legacy_unknown: 1" in result.stdout
    assert "  other: 2" in result.stdout
    assert "  summer: 2" in result.stdout
    assert "  year_in_industry: 1" in result.stdout
    assert "After programme_group histogram:" in result.stdout
    assert "  legacy_unknown: 1" in result.stdout
    assert "  other: 1" in result.stdout
    assert "  spring_week: 1" in result.stdout
    assert "  summer: 2" in result.stdout
    assert "  year_in_industry: 1" in result.stdout
    assert "Rows changed: 5" in result.stdout
    assert "Authoritative provenance unrecoverable: 1" in result.stdout
    assert "Backup:" not in result.stdout
    assert "Lost Trackr Provenance" not in result.stdout
    assert _classifications(database_path) == before
    assert not (tmp_path / "backups").exists()
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "delete"
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_outbox WHERE status='PENDING'"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    assert database_path.read_bytes() == bytes_before


def test_reclassify_programmes_apply_backs_up_audits_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database, database_path = _seed_database(tmp_path)
    before = _classifications(database_path)

    first = _run_cli(tmp_path, "reclassify-programmes")

    assert first.returncode == 0, first.stderr
    assert "Mode: APPLIED" in first.stdout
    assert "Rows changed: 5" in first.stdout
    assert "Authoritative provenance unrecoverable: 1" in first.stdout
    backups = sorted((tmp_path / "backups").glob("*.bak"))
    assert len(backups) == 1
    assert f"Backup: {backups[0]}" in first.stdout
    _assert_integrity(backups[0])
    assert _classifications(backups[0]) == before
    with sqlite3.connect(f"file:{backups[0]}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_outbox WHERE status='PENDING'"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0

    expected = {
        "Exact Off Cycle": "summer",
        "Exact Placement": "year_in_industry",
        "Exact Tracker URL": "spring_week",
        "Heuristic Other": "other",
        "Heuristic Summer": "summer",
        "Lost Trackr Provenance": "legacy_unknown",
    }
    assert _classifications(database_path) == expected
    with database.session_scope() as session:
        events = list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == "opportunity.programme_reclassified")
                .order_by(AuditEvent.id)
            ).all()
        )
        assert len(events) == 5
        assert {
            (details["old_programme_group"], details["new_programme_group"])
            for details in (json.loads(event.details_json) for event in events)
        } == {
            ("summer", "year_in_industry"),
            ("year_in_industry", "summer"),
            ("other", "spring_week"),
            ("other", "summer"),
            ("summer", "other"),
        }
        assert session.scalar(
            select(func.count())
            .select_from(AuditOutbox)
            .where(AuditOutbox.status == AuditOutbox.PENDING)
        ) == 0

    second = _run_cli(tmp_path, "reclassify-programmes")

    assert second.returncode == 0, second.stderr
    assert "Mode: APPLIED" in second.stdout
    assert "Rows changed: 0" in second.stdout
    assert "Authoritative provenance unrecoverable: 1" in second.stdout
    second_backups = sorted((tmp_path / "backups").glob("*.bak"))
    assert len(second_backups) == 2
    new_backup = next(path for path in second_backups if path not in backups)
    assert f"Backup: {new_backup}" in second.stdout
    _assert_integrity(new_backup)
    assert _classifications(new_backup) == expected
    assert _classifications(database_path) == expected
    with database.session_scope() as session:
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "opportunity.programme_reclassified")
        ) == 5
