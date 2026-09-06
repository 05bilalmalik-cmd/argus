from __future__ import annotations

import json
import subprocess
import sys
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.models import AnswerEntry, Application, AuditEvent, Opportunity
from app.scouting.service import ScoutService
from app.scouting.trackr_live import SLUG_TO_TYPE, TRACKER_URL, TrackrRow
from scripts import backfill_application_windows as backfill


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "backfill_application_windows.py"


def test_backfill_command_exists_and_defaults_to_safe_dry_run() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "--apply" in result.stdout
    assert "dry-run" in result.stdout.casefold()


def _setup_waiting_database(tmp_path: Path) -> tuple[Path, Settings]:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    from app.security.crypto import CryptoBox

    waiting = TrackrRow(
        employer="Backfill Capital",
        role_title="Industrial Placement 2027",
        tracker_url="https://app.the-trackr.com/uk-finance/industrial-placements",
        employer_application_url=None,
        opening_date=None,
        closing_date=None,
        programme_type="year_in_industry",
        source="trackr_live:industrial-placements",
        explicit_status=None,
        rolling=None,
        season="2027",
        source_record_id="backfill-programme",
    )
    with database.session_scope() as session:
        ScoutService(
            session,
            settings,
            CryptoBox.from_path(settings.secret_key_path),
        ).ingest([waiting])
    database.engine.dispose()
    return tmp_path / "argus.db", settings


def _opened_row() -> TrackrRow:
    return TrackrRow(
        employer="Backfill Capital",
        role_title="Industrial Placement 2027",
        tracker_url="https://app.the-trackr.com/uk-finance/industrial-placements",
        employer_application_url="https://jobs.example.com/backfill-placement",
        opening_date=None,
        closing_date=None,
        programme_type="year_in_industry",
        source="trackr_live:industrial-placements",
        explicit_status="open",
        rolling=False,
        season="2027",
        source_record_id="backfill-programme",
    )


def _colliding_opened_row(source_record_id: str) -> TrackrRow:
    return TrackrRow(
        employer="Backfill Capital",
        role_title="Industrial Placement 2027",
        tracker_url="https://app.the-trackr.com/uk-finance/industrial-placements",
        employer_application_url="https://jobs.example.com/different-owner",
        opening_date=date(2026, 8, 24),
        closing_date=date(2026, 10, 16),
        programme_type="year_in_industry",
        source="trackr_live:industrial-placements",
        explicit_status="open",
        rolling=True,
        season="2027",
        source_record_id=source_record_id,
    )


def test_dry_run_projects_without_writing_and_apply_backs_up_before_mutation(
    tmp_path: Path,
) -> None:
    database_path, _settings = _setup_waiting_database(tmp_path)
    backups_dir = tmp_path / "backups"
    before_bytes = database_path.read_bytes()
    before_mtime = database_path.stat().st_mtime_ns

    dry_run = backfill.run_backfill(
        database_path,
        [_opened_row()],
        apply=False,
        backups_dir=backups_dir,
    )

    assert dry_run["applied"] is False
    assert dry_run["backup_path"] is None
    assert dry_run["projected"]["window_states"]["OPEN"] == 1
    assert dry_run["before"]["captured_employer_application_urls"] == 0
    assert dry_run["projected"]["captured_employer_application_urls"] == 1
    assert database_path.read_bytes() == before_bytes
    assert database_path.stat().st_mtime_ns == before_mtime
    assert not backups_dir.exists()

    applied = backfill.run_backfill(
        database_path,
        [_opened_row()],
        apply=True,
        backups_dir=backups_dir,
    )
    backup_path = Path(str(applied["backup_path"]))

    assert applied["applied"] is True
    assert applied["rows_deleted"] == 0
    assert applied["applications_deleted"] == 0
    assert backup_path.parent == backups_dir
    assert backup_path.exists()
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        backup_state = connection.execute(
            "SELECT application_window_status, application_url FROM opportunities"
        ).fetchone()
    assert backup_state == ("NOT_YET_OPEN", None)
    with sqlite3.connect(database_path) as connection:
        live_state = connection.execute(
            "SELECT application_window_status, application_url FROM opportunities"
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
    assert live_state == ("OPEN", "https://jobs.example.com/backfill-placement")


def test_apply_is_idempotent_and_preserves_parent_and_child_ids(tmp_path: Path) -> None:
    database_path, _settings = _setup_waiting_database(tmp_path)
    backups_dir = tmp_path / "backups"

    with sqlite3.connect(database_path) as connection:
        before_opportunity_ids = {
            row[0] for row in connection.execute("SELECT id FROM opportunities")
        }
        before_application_ids = {
            row[0] for row in connection.execute("SELECT id FROM applications")
        }
        before_trackr_ids = list(
            connection.execute(
                "SELECT id, trackr_programme_id FROM opportunities ORDER BY id"
            )
        )
        before_identity_audits = connection.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type LIKE 'scout.trackr_identity_%'"
        ).fetchone()[0]

    first = backfill.run_backfill(
        database_path,
        [_opened_row()],
        apply=True,
        backups_dir=backups_dir,
    )
    second = backfill.run_backfill(
        database_path,
        [_opened_row()],
        apply=True,
        backups_dir=backups_dir,
    )

    assert first["ingest"]["updated"] == 1
    assert second["ingest"]["updated"] == 0
    assert second["ingest"]["duplicates"] == 1
    with sqlite3.connect(database_path) as connection:
        after_opportunity_ids = {
            row[0] for row in connection.execute("SELECT id FROM opportunities")
        }
        after_application_ids = {
            row[0] for row in connection.execute("SELECT id FROM applications")
        }
        after_trackr_ids = list(
            connection.execute(
                "SELECT id, trackr_programme_id FROM opportunities ORDER BY id"
            )
        )
        after_identity_audits = connection.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type LIKE 'scout.trackr_identity_%'"
        ).fetchone()[0]
    assert before_opportunity_ids == after_opportunity_ids
    assert before_application_ids == after_application_ids
    assert before_trackr_ids == after_trackr_ids
    assert before_identity_audits == after_identity_audits
    assert first["rows_deleted"] == second["rows_deleted"] == 0
    assert first["applications_deleted"] == second["applications_deleted"] == 0


def test_application_window_backfill_refreshes_preidentity_row_without_binding_id(
    tmp_path: Path,
) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    from app.security.crypto import CryptoBox

    waiting = TrackrRow(
        employer="Backfill Capital",
        role_title="Industrial Placement 2027",
        tracker_url="https://app.the-trackr.com/uk-finance/industrial-placements",
        employer_application_url=None,
        opening_date=None,
        closing_date=None,
        programme_type="year_in_industry",
        source="trackr_live:industrial-placements",
        explicit_status=None,
        rolling=None,
        season="2027",
        source_record_id="",
    )
    with database.session_scope() as session:
        ScoutService(
            session,
            settings,
            CryptoBox.from_path(settings.secret_key_path),
        ).ingest([waiting])
        opportunity_id = session.query(Opportunity.id).scalar()
        application_id = session.query(Application.id).scalar()
    database.engine.dispose()
    database_path = tmp_path / "argus.db"

    with sqlite3.connect(database_path) as connection:
        before_identity_audits = connection.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type LIKE 'scout.trackr_identity_%'"
        ).fetchone()[0]

    stats, _legacy = backfill._apply_rows(database_path, [_opened_row()])

    assert stats["trackr_id_legacy_claimed"] == 0
    assert stats["trackr_id_inserted"] == 0
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT id, trackr_programme_id, application_window_status, "
            "application_url FROM opportunities"
        ).fetchone()
        persisted_application_id = connection.execute(
            "SELECT id FROM applications"
        ).fetchone()[0]
        after_identity_audits = connection.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type LIKE 'scout.trackr_identity_%'"
        ).fetchone()[0]

    assert row == (
        opportunity_id,
        None,
        "OPEN",
        "https://jobs.example.com/backfill-placement",
    )
    assert persisted_application_id == application_id
    assert after_identity_audits == before_identity_audits


@pytest.mark.parametrize("source_record_id", ["different-programme", ""])
def test_backfill_never_refreshes_evidence_owned_by_a_different_trackr_id(
    tmp_path: Path,
    source_record_id: str,
) -> None:
    database_path, _settings = _setup_waiting_database(tmp_path)
    with sqlite3.connect(database_path) as connection:
        before_opportunity = connection.execute(
            "SELECT * FROM opportunities"
        ).fetchone()
        before_applications = connection.execute(
            "SELECT * FROM applications ORDER BY id"
        ).fetchall()
        before_archives = connection.execute(
            "SELECT * FROM opportunity_archives ORDER BY opportunity_id"
        ).fetchall()
        before_audits = connection.execute(
            "SELECT COUNT(*) FROM audit_events"
        ).fetchone()[0]
        before_identity_audits = connection.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type LIKE 'scout.trackr_identity_%'"
        ).fetchone()[0]

    stats, _legacy = backfill._apply_rows(
        database_path,
        [_colliding_opened_row(source_record_id)],
    )

    with sqlite3.connect(database_path) as connection:
        after_opportunity = connection.execute(
            "SELECT * FROM opportunities"
        ).fetchone()
        after_applications = connection.execute(
            "SELECT * FROM applications ORDER BY id"
        ).fetchall()
        after_archives = connection.execute(
            "SELECT * FROM opportunity_archives ORDER BY opportunity_id"
        ).fetchall()
        after_audits = connection.execute(
            "SELECT COUNT(*) FROM audit_events"
        ).fetchone()[0]
        after_identity_audits = connection.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type LIKE 'scout.trackr_identity_%'"
        ).fetchone()[0]

    assert stats["updated"] == 0
    assert stats["trackr_id_legacy_claimed"] == 0
    assert stats["trackr_id_inserted"] == 0
    assert before_opportunity == after_opportunity
    assert before_applications == after_applications
    assert before_archives == after_archives
    assert before_audits == after_audits
    assert before_identity_audits == after_identity_audits


def test_backfill_marks_residual_real_url_inventory_open_without_guessing_trackr(
    tmp_path: Path,
) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        session.add_all(
            [
                Opportunity(
                    employer="Direct Employer",
                    role_title="Summer Internship",
                    programme_group="summer",
                    cycle="2026-27",
                    url="https://jobs.direct-employer.example/internship",
                    source="manual",
                    rolling=True,
                    application_window_status="UNKNOWN",
                ),
                Opportunity(
                    employer="Unseen Trackr Employer",
                    role_title="Spring Insight",
                    programme_group="spring_week",
                    cycle="2026-27",
                    url="https://app.the-trackr.com/uk-finance/spring-weeks",
                    source="trackr_live:spring-weeks",
                    target_status="MISSING_EMPLOYER_LINK",
                    application_window_status="UNKNOWN",
                ),
            ]
        )
    database.engine.dispose()

    report = backfill.run_backfill(
        tmp_path / "argus.db",
        [],
        apply=False,
        backups_dir=tmp_path / "backups",
    )

    assert report["projected"]["window_states"] == {"OPEN": 1, "UNKNOWN": 1}
    assert report["projected"]["rolling_true"] == 0
    assert report["projected"]["rolling_false"] == 2
    assert report["legacy_window_backfill"] == {
        "opened": 1,
        "not_yet_open": 0,
        "closed": 0,
        "left_unknown": 1,
        "rolling_cleared": 1,
    }


def test_residual_rolling_only_mutation_is_audited_once_and_is_idempotent(
    tmp_path: Path,
) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        record = Opportunity(
            employer="Unseen Trackr Employer",
            role_title="Spring Insight",
            programme_group="spring_week",
            cycle="2026-27",
            url="https://app.the-trackr.com/uk-finance/spring-weeks",
            source="trackr_live:spring-weeks",
            target_status="MISSING_EMPLOYER_LINK",
            rolling=True,
            application_window_status="UNKNOWN",
        )
        session.add(record)
        session.flush()
        opportunity_id = record.id

    from app.security.crypto import CryptoBox

    with database.session_scope() as session:
        stats = ScoutService(
            session,
            settings,
            CryptoBox.from_path(settings.secret_key_path),
        ).backfill_unknown_application_windows()
        assert stats == {
            "opened": 0,
            "not_yet_open": 0,
            "closed": 0,
            "left_unknown": 1,
            "rolling_cleared": 1,
        }

    with database.session_scope() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type == "scout.application_window_backfilled",
                    AuditEvent.entity_id == opportunity_id,
                )
            ).all()
        )
        assert len(events) == 1
        assert json.loads(events[0].details_json) == {
            "application_window_status": "UNKNOWN",
            "archive_change": "",
            "evidence": "rolling_default_cleared",
            "rolling_cleared": True,
        }

    with database.session_scope() as session:
        second_stats = ScoutService(
            session,
            settings,
            CryptoBox.from_path(settings.secret_key_path),
        ).backfill_unknown_application_windows()
        assert second_stats["rolling_cleared"] == 0

    with database.session_scope() as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(AuditEvent).where(
                            AuditEvent.event_type
                            == "scout.application_window_backfilled",
                            AuditEvent.entity_id == opportunity_id,
                        )
                    ).all()
                )
            )
            == 1
        )
    database.engine.dispose()


def test_backfill_report_keeps_all_evidence_table_counts(tmp_path: Path) -> None:
    database_path, _settings = _setup_waiting_database(tmp_path)

    report = backfill.run_backfill(
        database_path,
        [],
        apply=False,
        backups_dir=tmp_path / "backups",
    )

    expected_keys = {
        "automation_runs",
        "question_records",
        "answer_entries",
        "documents",
        "email_messages",
        "submission_review_bindings",
    }
    for snapshot_name in ("before", "projected", "after"):
        snapshot = report[snapshot_name]
        assert expected_keys <= set(snapshot)
        assert {key: snapshot[key] for key in expected_keys} == {
            "automation_runs": 0,
            "question_records": 0,
            "answer_entries": 0,
            "documents": 0,
            "email_messages": 0,
            "submission_review_bindings": 0,
        }


def test_backfill_rolls_back_injected_form_evidence_count_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path, _settings = _setup_waiting_database(tmp_path)
    original = ScoutService.backfill_unknown_application_windows

    def inject_answer_entry(self: ScoutService) -> dict[str, int]:
        stats = original(self)
        self.session.add(AnswerEntry(canonical_key="phase19-injected-drift"))  # gitleaks:allow -- synthetic answer identifier, not a credential
        return stats

    monkeypatch.setattr(
        ScoutService,
        "backfill_unknown_application_windows",
        inject_answer_entry,
    )

    with pytest.raises(RuntimeError, match="answer_entries row count changed"):
        backfill.run_backfill(
            database_path,
            [],
            apply=True,
            backups_dir=tmp_path / "backups",
        )

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM answer_entries "
                "WHERE canonical_key='phase19-injected-drift'"  # gitleaks:allow -- synthetic answer identifier, not a credential
            ).fetchone()[0]
            == 0
        )


def test_empty_live_scrape_refuses_before_backfill_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutation_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("run_backfill must not be called for an empty live scrape")

    monkeypatch.setattr(backfill, "fetch_programmes", lambda: [])
    monkeypatch.setattr(backfill, "run_backfill", mutation_must_not_run)

    with pytest.raises(SystemExit) as error:
        backfill.main(["--database", str(tmp_path / "argus.db")])

    assert error.value.code == 2


def _complete_cli_capture() -> list[TrackrRow]:
    return [
        TrackrRow(
            employer=f"Capture Guard {index}",
            role_title=f"Complete Capture {index}",
            tracker_url=TRACKER_URL.format(slug=slug),
            employer_application_url=None,
            opening_date=None,
            closing_date=None,
            programme_type=programme_type,
            source=f"trackr_live:{slug}",
            rolling=False,
            season="2027",
            source_record_id=f"capture-guard-{index}",
        )
        for index, (slug, programme_type) in enumerate(SLUG_TO_TYPE.items())
    ]


def _forbidden_cli_database_work(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("incomplete live capture must fail before database work")


def _append_unknown_cli_source(rows: list[TrackrRow]) -> None:
    extra = _complete_cli_capture()[0]
    extra.source = "trackr_live:unknown-programmes"
    extra.source_record_id = "capture-guard-unknown"
    rows.append(extra)


def test_backfill_cli_partial_timeout_capture_cannot_reach_apply(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = _complete_cli_capture()[:-1]
    monkeypatch.setattr(backfill, "fetch_programmes", lambda: rows)
    monkeypatch.setattr(backfill, "run_backfill", _forbidden_cli_database_work)
    monkeypatch.setattr(backfill, "create_verified_backup", _forbidden_cli_database_work)
    monkeypatch.setattr(backfill, "Database", _forbidden_cli_database_work)
    monkeypatch.setattr(backfill.Settings, "load", _forbidden_cli_database_work)

    with pytest.raises(SystemExit) as error:
        backfill.main(["--apply"])

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "missing sources" in stderr
    assert "off-cycle-internships" in stderr
    assert "unexpected sources=[]" in stderr
    assert "malformed sources=[]" in stderr


@pytest.mark.parametrize(
    "mutate, expected_detail",
    [
        (
            _append_unknown_cli_source,
            "unknown-programmes",
        ),
        (lambda rows: setattr(rows[0], "source", "trackr_live:"), "trackr_live:"),
        (
            lambda rows: setattr(
                rows[0],
                "tracker_url",
                TRACKER_URL.format(slug="spring-weeks"),
            ),
            "tracker_url",
        ),
        (
            lambda rows: setattr(rows[0], "programme_type", "spring_week"),
            "programme_type",
        ),
    ],
)
def test_backfill_cli_rejects_unexpected_or_malformed_capture_sources(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutate,
    expected_detail: str,
) -> None:
    rows = _complete_cli_capture()
    mutate(rows)
    monkeypatch.setattr(backfill, "fetch_programmes", lambda: rows)
    monkeypatch.setattr(backfill, "run_backfill", _forbidden_cli_database_work)
    monkeypatch.setattr(backfill, "create_verified_backup", _forbidden_cli_database_work)
    monkeypatch.setattr(backfill, "Database", _forbidden_cli_database_work)
    monkeypatch.setattr(backfill.Settings, "load", _forbidden_cli_database_work)

    with pytest.raises(SystemExit) as error:
        backfill.main(["--apply"])

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert expected_detail in stderr
    assert "missing sources=" in stderr
    assert "unexpected sources=" in stderr
    assert "malformed sources=" in stderr


def test_backfill_cli_complete_four_slug_capture_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = _complete_cli_capture()
    observed: dict[str, object] = {}

    def dispatch(database_path, captured_rows, *, apply, backups_dir):
        observed.update(
            database_path=database_path,
            rows=captured_rows,
            apply=apply,
            backups_dir=backups_dir,
        )
        return {"applied": apply, "rows_deleted": 0, "applications_deleted": 0}

    database_path = tmp_path / "argus.db"
    monkeypatch.setattr(backfill, "fetch_programmes", lambda: rows)
    monkeypatch.setattr(backfill, "run_backfill", dispatch)

    assert backfill.main(["--apply", "--database", str(database_path)]) == 0
    assert observed == {
        "database_path": database_path.resolve(),
        "rows": rows,
        "apply": True,
        "backups_dir": database_path.resolve().parent / "backups",
    }
    assert json.loads(capsys.readouterr().out)["applied"] is True
