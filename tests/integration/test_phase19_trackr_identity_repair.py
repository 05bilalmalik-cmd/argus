from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Database, SCHEMA_VERSION
from app.domain.targets import TargetKind
from app.models import Application, Opportunity, OpportunityArchive
from app.runtime_lock import RuntimeAlreadyRunning, RuntimeLock, runtime_lock_path
from app.scouting.service import ScoutService
from app.scouting.trackr_live import SLUG_TO_TYPE, TRACKER_URL, TrackrRow
from app.security.audit import AuditInput, append_audit
from app.security.crypto import CryptoBox
from app.services.target_resolution import TargetResolutionService
from scripts.backfill_application_windows import run_backfill
from scripts import repair_trackr_identities as repair
from scripts.repair_trackr_identities import run_identity_repair


def _database_path(tmp_path: Path) -> Path:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    database.engine.dispose()
    return tmp_path / "argus.db"


def _row(
    raw_id: str,
    *,
    employer: str = "Repair Capital",
    role_title: str = "Investment Banking Industrial Placement 2027",
    application_url: str | None = None,
    source: str = "trackr_live:industrial-placements",
    tracker_url: str = (
        "https://app.the-trackr.com/uk-finance/industrial-placements"
    ),
    programme_type: str = "year_in_industry",
    explicit_status: str | None = None,
) -> TrackrRow:
    return TrackrRow(
        employer=employer,
        role_title=role_title,
        tracker_url=tracker_url,
        employer_application_url=application_url,
        opening_date=None,
        closing_date=None,
        programme_type=programme_type,
        location="London",
        source=source,
        explicit_status=explicit_status,
        rolling=False,
        season="2027",
        source_record_id=raw_id,
    )


def _seed_rows(database_path: Path, rows: list[TrackrRow]) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(database_path.parent)})
    database = Database(settings)
    crypto = CryptoBox.from_path(settings.secret_key_path)
    try:
        with database.session_scope() as session:
            ScoutService(session, settings, crypto).ingest(rows)
    finally:
        database.engine.dispose()


def test_dry_run_is_byte_and_mtime_pure_and_projects_schema_v8(tmp_path: Path) -> None:
    database_path = _database_path(tmp_path)
    backup_dir = tmp_path / "must-not-exist"
    before_bytes = database_path.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_mtime = database_path.stat().st_mtime_ns

    report = run_identity_repair(
        database_path,
        [_row("repair-a")],
        apply=False,
        backup_dir=backup_dir,
    )

    assert report["applied"] is False
    assert report["before"]["schema_version"] == SCHEMA_VERSION
    assert report["projected"]["schema_version"] == SCHEMA_VERSION
    assert report["identity_dispositions"]["insert_new"] == 1
    assert report["projected"]["opportunities"] == 1
    assert report["projected"]["applications"] == 1
    assert report["rows_deleted"] == 0
    assert report["applications_deleted"] == 0
    assert database_path.read_bytes() == before_bytes
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before_hash
    assert database_path.stat().st_mtime_ns == before_mtime
    assert not backup_dir.exists()
    assert not runtime_lock_path(database_path.parent).exists()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 0


def test_dry_run_preserves_committed_wal_main_and_wal_bytes_and_mtimes(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    writer = sqlite3.connect(database_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].casefold() == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE wal_probe (id INTEGER PRIMARY KEY, value TEXT)")
        writer.execute("INSERT INTO wal_probe(value) VALUES ('committed')")
        writer.commit()
        wal_path = Path(str(database_path) + "-wal")
        assert wal_path.is_file()
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (database_path, wal_path)
        }

        report = run_identity_repair(database_path, [_row("wal-id")])

        assert report["projected"]["table_counts"]["wal_probe"] == 1
        for path, (raw, mtime) in before.items():
            assert path.read_bytes() == raw
            assert path.stat().st_mtime_ns == mtime
    finally:
        writer.close()


@pytest.mark.parametrize(
    "rows, message",
    [
        ([], "empty"),
        ([_row("")], "missing or invalid"),
        ([_row("same"), _row("same")], "duplicate"),
        (
            [_row("same"), _row("same", role_title="Conflicting Placement")],
            "conflicting duplicate",
        ),
    ],
)
def test_invalid_capture_refuses_before_backup_or_database_write(
    tmp_path: Path,
    rows: list[TrackrRow],
    message: str,
) -> None:
    database_path = _database_path(tmp_path)
    before = database_path.read_bytes()
    backup_dir = tmp_path / "backups"

    with pytest.raises(ValueError, match=message):
        run_identity_repair(
            database_path,
            rows,
            apply=True,
            backup_dir=backup_dir,
        )

    assert database_path.read_bytes() == before
    assert not backup_dir.exists()


def test_cli_rejects_empty_capture_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repair, "fetch_programmes", lambda: [])

    def forbidden_dispatch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("empty capture must not reach identity repair")

    monkeypatch.setattr(repair, "run_identity_repair", forbidden_dispatch)
    with pytest.raises(SystemExit) as raised:
        repair.main([])
    assert raised.value.code == 2


def _complete_cli_capture() -> list[TrackrRow]:
    return [
        _row(
            f"complete-capture-{index}",
            employer=f"Complete Capture {index}",
            role_title=f"Programme {index}",
            source=f"trackr_live:{slug}",
            tracker_url=TRACKER_URL.format(slug=slug),
            programme_type=programme_type,
        )
        for index, (slug, programme_type) in enumerate(SLUG_TO_TYPE.items())
    ]


def _forbidden_cli_database_work(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("incomplete live capture must fail before database work")


def _append_unknown_cli_source(rows: list[TrackrRow]) -> None:
    extra = _complete_cli_capture()[0]
    extra.source = "trackr_live:unknown-programmes"
    extra.source_record_id = "complete-capture-unknown"
    rows.append(extra)


def test_identity_repair_cli_partial_timeout_capture_cannot_reach_apply(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = _complete_cli_capture()[:-1]
    monkeypatch.setattr(repair, "fetch_programmes", lambda: rows)
    monkeypatch.setattr(repair, "run_identity_repair", _forbidden_cli_database_work)
    monkeypatch.setattr(repair, "create_verified_backup", _forbidden_cli_database_work)
    monkeypatch.setattr(repair, "Database", _forbidden_cli_database_work)
    monkeypatch.setattr(repair.Settings, "load", _forbidden_cli_database_work)

    with pytest.raises(SystemExit) as error:
        repair.main(["--apply"])

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
def test_identity_repair_cli_rejects_unexpected_or_malformed_capture_sources(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutate,
    expected_detail: str,
) -> None:
    rows = _complete_cli_capture()
    mutate(rows)
    monkeypatch.setattr(repair, "fetch_programmes", lambda: rows)
    monkeypatch.setattr(repair, "run_identity_repair", _forbidden_cli_database_work)
    monkeypatch.setattr(repair, "create_verified_backup", _forbidden_cli_database_work)
    monkeypatch.setattr(repair, "Database", _forbidden_cli_database_work)
    monkeypatch.setattr(repair.Settings, "load", _forbidden_cli_database_work)

    with pytest.raises(SystemExit) as error:
        repair.main(["--apply"])

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert expected_detail in stderr
    assert "missing sources=" in stderr
    assert "unexpected sources=" in stderr
    assert "malformed sources=" in stderr


def test_identity_repair_cli_complete_four_slug_capture_dispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = _complete_cli_capture()
    observed: dict[str, object] = {}

    def dispatch(database_path, captured_rows, *, apply, backup_dir):
        observed.update(
            database_path=database_path,
            rows=captured_rows,
            apply=apply,
            backup_dir=backup_dir,
        )
        return {"applied": apply, "rows_deleted": 0, "applications_deleted": 0}

    database_path = tmp_path / "argus.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(repair, "fetch_programmes", lambda: rows)
    monkeypatch.setattr(repair, "run_identity_repair", dispatch)

    assert (
        repair.main(
            [
                "--apply",
                "--database",
                str(database_path),
                "--backup-dir",
                str(backup_dir),
            ]
        )
        == 0
    )
    assert observed == {
        "database_path": database_path.resolve(),
        "rows": rows,
        "apply": True,
        "backup_dir": backup_dir,
    }
    assert json.loads(capsys.readouterr().out)["applied"] is True


@pytest.mark.parametrize(
    "row, message",
    [
        (_row("bad", source="trackr_live:"), "slug"),
        (_row("bad", source="trackr_live:unknown-programmes"), "slug"),
        (
            _row(
                "bad",
                tracker_url="https://app.the-trackr.com/uk-finance/spring-weeks",
            ),
            "canonical tracker URL",
        ),
        (_row("bad", programme_type="spring_week"), "programme type"),
    ],
)
def test_capture_rejects_false_trackr_provenance_before_database_or_backup(
    tmp_path: Path,
    row: TrackrRow,
    message: str,
) -> None:
    database_path = _database_path(tmp_path)
    before = database_path.read_bytes()
    backup_dir = tmp_path / "backups"

    with pytest.raises(ValueError, match=message):
        run_identity_repair(
            database_path,
            [row],
            apply=True,
            backup_dir=backup_dir,
        )

    assert database_path.read_bytes() == before
    assert not backup_dir.exists()


def test_preheld_runtime_lock_refuses_before_backup_or_database_mutation(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    backup_dir = tmp_path / "backups"
    before = repair.database_snapshot(database_path)

    with RuntimeLock(runtime_lock_path(tmp_path), port=8787):
        with pytest.raises(RuntimeAlreadyRunning):
            run_identity_repair(
                database_path,
                [_row("locked-id")],
                apply=True,
                backup_dir=backup_dir,
            )

    assert not backup_dir.exists()
    assert repair.database_snapshot(database_path)["prewrite_guard_sha256"] == before[
        "prewrite_guard_sha256"
    ]


def test_external_wal_commit_after_verified_backup_aborts_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _database_path(tmp_path)
    _seed_rows(database_path, [_row("", employer="Concurrent Capital")])
    backup_dir = tmp_path / "backups"
    real_backup = repair.create_verified_backup
    external_url = "https://jobs.example.com/concurrent-user-update"

    def backup_then_external_update(path: Path, directory: Path):
        metadata = real_backup(path, directory)
        with sqlite3.connect(path, timeout=0) as writer:
            writer.execute(
                "UPDATE opportunities SET application_url=? WHERE employer=?",
                (external_url, "Concurrent Capital"),
            )
            writer.commit()
        return metadata

    monkeypatch.setattr(repair, "create_verified_backup", backup_then_external_update)
    with pytest.raises(RuntimeError, match="prewrite guard"):
        run_identity_repair(
            database_path,
            [
                _row(
                    "concurrent-id",
                    employer="Concurrent Capital",
                    application_url="https://jobs.example.com/captured",
                )
            ],
            apply=True,
            backup_dir=backup_dir,
        )

    with sqlite3.connect(database_path) as connection:
        live_url = connection.execute(
            "SELECT application_url FROM opportunities WHERE employer=?",
            ("Concurrent Capital",),
        ).fetchone()[0]
    backup_path = next(backup_dir.glob("*.bak"))
    with sqlite3.connect(backup_path) as connection:
        backed_up_url = connection.execute(
            "SELECT application_url FROM opportunities WHERE employer=?",
            ("Concurrent Capital",),
        ).fetchone()[0]
    assert live_url == external_url
    assert backed_up_url is None


def test_begin_immediate_reservation_rejects_second_writer_before_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _database_path(tmp_path)
    real_ingest = ScoutService.ingest
    attempts = 0
    observed: list[str] = []

    def ingest_with_writer_probe(self, *args: object, **kwargs: object):
        nonlocal attempts
        attempts += 1
        if Path(self.settings.data_dir).resolve() == database_path.parent.resolve():
            with sqlite3.connect(database_path, timeout=0) as contender:
                contender.execute("PRAGMA busy_timeout=0")
                try:
                    contender.execute(
                        "INSERT INTO conflict_rules "
                        "(id, employer_pattern, cycle, max_applications, "
                        "exclusive_groups_json, notes) "
                        "VALUES ('contender', '*', '2027', NULL, '[]', '')"
                    )
                    contender.commit()
                except sqlite3.OperationalError as exc:
                    contender.rollback()
                    observed.append(str(exc).casefold())
                else:
                    observed.append("writer_succeeded")
        return real_ingest(self, *args, **kwargs)

    monkeypatch.setattr(ScoutService, "ingest", ingest_with_writer_probe)
    run_identity_repair(
        database_path,
        [_row("reservation-id")],
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    assert attempts >= 2
    assert observed and ("locked" in observed[0] or "busy" in observed[0])
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM conflict_rules WHERE id='contender'"
        ).fetchone()[0] == 0


def test_apply_claims_unique_legacy_and_inserts_collision_replacements(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    _seed_rows(
        database_path,
        [
            _row("", employer="Unique Legacy"),
            _row("", employer="Ambiguous Legacy"),
        ],
    )
    before = repair.database_snapshot(database_path)
    rows = [
        _row("unique-id", employer="Unique Legacy"),
        _row("collision-a", employer="Ambiguous Legacy"),
        _row("collision-b", employer="Ambiguous Legacy"),
    ]
    backup_dir = tmp_path / "verified-backups"

    report = run_identity_repair(
        database_path,
        rows,
        apply=True,
        backup_dir=backup_dir,
    )

    assert report["identity_dispositions"] == {
        "existing_bound": 0,
        "claim_legacy": 1,
        "insert_new": 2,
        "ambiguous_raw_ids": 2,
        "ambiguous_legacy_archived": 1,
        "ambiguous_legacy_preserved": 0,
    }
    assert report["after"]["opportunities"] == before["opportunities"] + 2
    assert report["after"]["applications"] == before["applications"] + 2
    assert report["rows_deleted"] == 0
    assert report["applications_deleted"] == 0
    assert report["invariants"]["passed"] is True
    assert report["idempotency"]["passed"] is True
    assert report["idempotency"]["audit_events_delta"] == 0
    assert report["idempotency"]["audit_outbox_delta"] == 0
    assert report["retained_unbound_legacy"] == 1
    assert report["after"]["active_archive_reasons"] == {
        "ambiguous_trackr_identity": 1
    }
    backup = Path(report["backup"]["path"])
    assert backup.parent == backup_dir
    assert backup.is_file()
    assert report["backup"]["integrity"] == "ok"
    assert report["backup"]["foreign_key_errors"] == 0
    assert report["backup"]["sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()


def test_report_serializes_complete_per_id_plan_ambiguity_and_retained_rows(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    _seed_rows(
        database_path,
        [
            _row("bound-id", employer="Bound Capital"),
            _row("", employer="Claim Capital"),
            _row("", employer="Ambiguity Capital"),
        ],
    )
    before = repair.database_snapshot(database_path)
    by_employer = {
        row[0]: row[1]
        for row in sqlite3.connect(database_path).execute(
            "SELECT employer, id FROM opportunities"
        )
    }
    rows = [
        _row("bound-id", employer="Bound Capital"),
        _row("claim-id", employer="Claim Capital"),
        _row("ambiguous-a", employer="Ambiguity Capital"),
        _row("ambiguous-b", employer="Ambiguity Capital"),
        _row("insert-id", employer="Insert Capital"),
    ]

    report = run_identity_repair(database_path, rows)

    plan = report["identity_plan"]
    assert [entry["trackr_programme_id"] for entry in plan] == sorted(
        row.source_record_id for row in rows
    )
    by_id = {entry["trackr_programme_id"]: entry for entry in plan}
    assert by_id["bound-id"]["disposition"] == "existing_bound"
    assert by_id["bound-id"]["planned_opportunity_id"] == by_employer["Bound Capital"]
    assert by_id["claim-id"]["disposition"] == "claim_legacy"
    assert by_id["claim-id"]["planned_opportunity_id"] == by_employer["Claim Capital"]
    for raw_id in ("ambiguous-a", "ambiguous-b", "insert-id"):
        assert by_id[raw_id]["disposition"] == "insert_new"
        assert by_id[raw_id]["planned_opportunity_id"] is None
    for entry in plan:
        assert entry["source_slug"] == "industrial-placements"
        assert len(entry["strict_claim_key"]) == 8
        assert entry["actual_opportunity_id"]
    assert report["ambiguity_cohorts"] == [
        {
            "strict_claim_key": by_id["ambiguous-a"]["strict_claim_key"],
            "raw_ids": ["ambiguous-a", "ambiguous-b"],
            "retained_legacy_opportunity_ids": [
                by_employer["Ambiguity Capital"]
            ],
        }
    ]
    assert by_employer["Ambiguity Capital"] in report["retained_unbound_rows"]
    assert report["before"]["prewrite_guard_sha256"] == before[
        "prewrite_guard_sha256"
    ]
    assert "application_url" not in json.dumps(
        {
            "identity_plan": plan,
            "ambiguity_cohorts": report["ambiguity_cohorts"],
        }
    )


def test_exact_plan_comparison_rejects_equal_total_disposition_swap() -> None:
    projection = [
        {
            "trackr_programme_id": "a",
            "source_slug": "industrial-placements",
            "strict_claim_key": ["a"],
            "disposition": "existing_bound",
            "planned_opportunity_id": "op-a",
            "actual_opportunity_id": "op-a",
        },
        {
            "trackr_programme_id": "b",
            "source_slug": "industrial-placements",
            "strict_claim_key": ["b"],
            "disposition": "claim_legacy",
            "planned_opportunity_id": "op-b",
            "actual_opportunity_id": "op-b",
        },
    ]
    swapped = deepcopy(projection)
    swapped[0]["disposition"], swapped[1]["disposition"] = (
        swapped[1]["disposition"],
        swapped[0]["disposition"],
    )

    with pytest.raises(RuntimeError, match="exact identity plan"):
        repair._assert_same_identity_plan(projection, [], swapped, [])


@pytest.mark.parametrize("disposition", ["existing_bound", "claim_legacy"])
def test_exact_plan_comparison_rejects_stable_actual_id_swap(
    disposition: str,
) -> None:
    projection = [
        {
            "trackr_programme_id": "stable-id",
            "source_slug": "industrial-placements",
            "strict_claim_key": ["stable"],
            "disposition": disposition,
            "planned_opportunity_id": "op-planned",
            "actual_opportunity_id": "op-planned",
            "application_window_status": "NOT_YET_OPEN",
        }
    ]
    live = deepcopy(projection)
    live[0]["actual_opportunity_id"] = "op-wrong"

    with pytest.raises(RuntimeError, match="exact identity plan"):
        repair._assert_same_identity_plan(projection, [], live, [])


def test_exact_plan_comparison_normalizes_only_insert_uuid() -> None:
    projection = [
        {
            "trackr_programme_id": "new-id",
            "source_slug": "industrial-placements",
            "strict_claim_key": ["new"],
            "disposition": "insert_new",
            "planned_opportunity_id": None,
            "actual_opportunity_id": "projection-uuid",
            "application_window_status": "NOT_YET_OPEN",
        }
    ]
    live = deepcopy(projection)
    live[0]["actual_opportunity_id"] = "live-uuid"

    repair._assert_same_identity_plan(projection, [], live, [])


def _assert_modified_projection_rejected(
    report: dict[str, object],
    after: dict[str, object],
    message: str,
    *,
    identity_plan: list[dict[str, object]] | None = None,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        repair._assert_repair_invariants(
            report["before"],
            after,
            report["projection_ingest"],
            [entry["trackr_programme_id"] for entry in report["identity_plan"]],
            identity_plan=(
                identity_plan
                if identity_plan is not None
                else report["identity_plan"]
            ),
            ambiguity_cohorts=report["ambiguity_cohorts"],
        )


def test_exact_invariant_rejects_equal_total_application_binding_swap(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    database = Database(settings)
    try:
        with database.session_scope() as session:
            old = Opportunity(
                employer="Old Childless",
                role_title="Industrial Placement 2027",
                division="Other",
                programme_group="year_in_industry",
                location="London",
                cycle="2026-27",
                url="https://example.com/old",
                source="manual",
                ats_type="unknown",
            )
            session.add(old)
            session.flush()
            old_id = old.id
    finally:
        database.engine.dispose()
    report = run_identity_repair(database_path, [_row("new-id")])
    after = deepcopy(report["projected"])
    before_application_ids = {
        binding[0] for binding in report["before"]["application_bindings"]
    }
    new_binding = next(
        binding
        for binding in after["application_bindings"]
        if binding[0] not in before_application_ids
    )
    after["application_bindings"] = [
        [binding[0], old_id] if binding[0] == new_binding[0] else binding
        for binding in after["application_bindings"]
    ]

    _assert_modified_projection_rejected(report, after, "new application bindings")


def test_exact_invariant_rejects_allowed_reason_on_wrong_archive_subject(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    database = Database(settings)
    try:
        with database.session_scope() as session:
            old = Opportunity(
                employer="Wrong Archive Subject",
                role_title="Industrial Placement 2027",
                division="Other",
                programme_group="year_in_industry",
                location="London",
                cycle="2026-27",
                url="https://example.com/old",
                source="manual",
                ats_type="unknown",
            )
            session.add(old)
            session.flush()
            old_id = old.id
    finally:
        database.engine.dispose()
    report = run_identity_repair(
        database_path, [_row("closed-id", explicit_status="closed")]
    )
    after = deepcopy(report["projected"])
    new_subject = next(
        key
        for key in after["archives_by_opportunity"]
        if key not in report["before"]["archives_by_opportunity"]
    )
    archive = after["archives_by_opportunity"].pop(new_subject)
    after["archives_by_opportunity"][old_id] = archive

    _assert_modified_projection_rejected(report, after, "exact new archive")


def test_exact_invariant_rejects_unexpected_appended_audit_action(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    report = run_identity_repair(database_path, [_row("audit-id")])
    after = deepcopy(report["projected"])
    before_ids = {row["id"] for row in report["before"]["audit_events_rows"]}
    event = next(row for row in after["audit_events_rows"] if row["id"] not in before_ids)
    event["event_type"] = "automation.unexpected"

    _assert_modified_projection_rejected(report, after, "unexpected audit action")


@pytest.mark.parametrize("disposition", ["existing_bound", "claim_legacy"])
def test_exact_invariant_rejects_stable_plan_actual_id_mismatch(
    tmp_path: Path,
    disposition: str,
) -> None:
    database_path = _database_path(tmp_path)
    if disposition == "existing_bound":
        _seed_rows(database_path, [_row("stable-id", employer="Stable Capital")])
    else:
        _seed_rows(database_path, [_row("", employer="Stable Capital")])
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    database = Database(settings)
    try:
        with database.session_scope() as session:
            wrong = Opportunity(
                employer="Wrong Stable Owner",
                role_title="Industrial Placement 2027",
                division="Other",
                programme_group="year_in_industry",
                location="London",
                cycle="2026-27",
                url="https://example.com/wrong",
                source="manual",
                ats_type="unknown",
            )
            session.add(wrong)
            session.flush()
            wrong_id = wrong.id
    finally:
        database.engine.dispose()
    report = run_identity_repair(
        database_path, [_row("stable-id", employer="Stable Capital")]
    )
    modified_plan = deepcopy(report["identity_plan"])
    assert modified_plan[0]["disposition"] == disposition
    modified_plan[0]["actual_opportunity_id"] = wrong_id

    _assert_modified_projection_rejected(
        report,
        deepcopy(report["projected"]),
        "planned opportunity",
        identity_plan=modified_plan,
    )


def test_historical_audit_fingerprint_and_error_are_redacted_from_report(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    secret = "https://private.example.com/apply?token=sentinel-secret"
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    database = Database(settings)
    try:
        with database.session_scope() as session:
            append_audit(
                session,
                AuditInput(
                    "historical",
                    "historical.private_event",
                    "opportunity",
                    "historical-opportunity",
                    {"application_url": secret, "private": "sentinel-secret"},
                ),
            )
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "UPDATE audit_outbox SET last_error=?",
                (f"failed while visiting {secret}",),
            )
            connection.commit()
    finally:
        database.engine.dispose()

    report = run_identity_repair(database_path, [_row("redaction-id")])
    serialized = json.dumps(report, sort_keys=True, default=str)

    assert secret not in serialized
    assert "sentinel-secret" not in serialized
    row = report["before"]["audit_outbox_rows"][0]
    assert "intent_fingerprint" not in row
    assert "last_error" not in row
    assert row["intent_fingerprint_sha256"]
    assert row["last_error_sha256"]


def test_historical_archive_reason_is_redacted_from_dry_run_and_apply_reports(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    secret = "https://private.example.com/apply?token=archive-sentinel-secret"
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    database = Database(settings)
    try:
        with database.session_scope() as session:
            append_audit(
                session,
                AuditInput(
                    "historical",
                    "opportunity.archived",
                    "opportunity",
                    "historical-opportunity",
                    {"reason": secret},
                ),
            )
    finally:
        database.engine.dispose()

    dry_run = run_identity_repair(database_path, [_row("archive-redaction-id")])
    applied = run_identity_repair(
        database_path,
        [_row("archive-redaction-id")],
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    for report in (dry_run, applied):
        serialized = json.dumps(report, sort_keys=True, default=str)
        assert secret not in serialized
        assert "archive-sentinel-secret" not in serialized
        historical_rows = [
            row
            for row in report["before"]["audit_events_rows"]
            if row["entity_id"] == "historical-opportunity"
        ]
        assert len(historical_rows) == 1
        assert historical_rows[0]["archive_reason"] == "<redacted>"
        assert historical_rows[0]["archive_reason_sha256"]


def test_idempotency_rejects_full_guard_only_mutation(tmp_path: Path) -> None:
    database_path = _database_path(tmp_path)
    before = repair.database_snapshot(database_path)
    after = deepcopy(before)
    after["prewrite_evidence"]["opportunities"]["row_sha256"].append("notes-mutated")
    after["prewrite_guard_sha256"] = "different-full-row-guard"
    stats = {key: 0 for key in repair._IDEMPOTENT_ZERO_STATS}

    with pytest.raises(RuntimeError, match="idempotency prewrite guard"):
        repair._assert_idempotent(before, after, stats)


def test_post_apply_idempotency_rejects_silent_notes_mutation_on_temp_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _database_path(tmp_path)
    real_ingest = ScoutService.ingest
    calls = 0

    def mutate_only_third_ingest(self, *args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        stats = real_ingest(self, *args, **kwargs)
        if calls == 3:
            opportunity = self.session.scalar(select(Opportunity))
            opportunity.notes = "silent-temp-only-mutation"
            self.session.flush()
        return stats

    monkeypatch.setattr(ScoutService, "ingest", mutate_only_third_ingest)
    with pytest.raises(RuntimeError, match="idempotency prewrite guard"):
        run_identity_repair(
            database_path,
            [_row("idempotency-id")],
            apply=True,
            backup_dir=tmp_path / "backups",
        )

    assert calls == 3
    with sqlite3.connect(database_path) as connection:
        notes = connection.execute("SELECT notes FROM opportunities").fetchone()[0]
    assert notes == ""


def test_v7_to_current_apply_does_not_false_positive_idempotency_guard(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX uq_opportunities_trackr_programme_id")
        connection.execute("ALTER TABLE opportunities DROP COLUMN trackr_programme_id")
        connection.execute("PRAGMA user_version=7")
        connection.commit()

    report = run_identity_repair(
        database_path,
        [_row("v7-upgrade-id")],
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    assert report["before"]["schema_version"] == 7
    assert report["after"]["schema_version"] == SCHEMA_VERSION
    assert report["idempotency"]["passed"] is True


def test_apply_preserves_verified_target_non_uk_and_forbidden_evidence_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _database_path(tmp_path)
    _seed_rows(
        database_path,
        [
            _row(
                "bound-id",
                employer="Verified Existing",
                application_url="https://jobs.example.com/original",
            ),
            _row("", employer="Non UK Ambiguous"),
        ],
    )
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    database = Database(settings)
    try:
        with database.session_scope() as session:
            verified = session.scalar(
                select(Opportunity).where(
                    Opportunity.trackr_programme_id == "bound-id"
                )
            )
            verified.application_url = "https://jobs.example.com/verified"
            verified.target_status = TargetKind.APPLICATION_ENTRY.value
            verified.resolved_ats_type = "custom"
            verified.resolution_evidence_json = '{"identity_verified":true}'
            verified.resolved_at = datetime.now(timezone.utc)
            legacy = session.scalar(
                select(Opportunity).where(
                    Opportunity.employer == "Non UK Ambiguous"
                )
            )
            session.add(
                OpportunityArchive(
                    opportunity=legacy,
                    archived_at=datetime.now(timezone.utc),
                    archived_reason="non_uk",
                )
            )
            verified_id = verified.id
            legacy_id = legacy.id
    finally:
        database.engine.dispose()
    before = repair.database_snapshot(database_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("identity repair must not resolve a target")

    monkeypatch.setattr(TargetResolutionService, "record", forbidden)
    report = run_identity_repair(
        database_path,
        [
            _row(
                "bound-id",
                employer="Verified Existing",
                application_url="https://jobs.example.com/unverified-new",
            ),
            _row("nonuk-a", employer="Non UK Ambiguous"),
            _row("nonuk-b", employer="Non UK Ambiguous"),
        ],
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    after = report["after"]
    serialized_report = json.dumps(report, sort_keys=True, default=str)
    assert "https://jobs.example.com/verified" not in serialized_report
    assert "https://jobs.example.com/unverified-new" not in serialized_report
    assert after["verified_targets"] == before["verified_targets"]
    assert after["archives_by_opportunity"][legacy_id] == before[
        "archives_by_opportunity"
    ][legacy_id]
    assert after["active_archive_reasons"]["non_uk"] == 1
    assert report["identity_dispositions"]["ambiguous_legacy_preserved"] == 1
    assert report["immutable_evidence_delta"]["unchanged"] is True
    for table in (
        "automation_runs",
        "question_records",
        "submission_intents",
        "submission_authorities",
        "submission_review_bindings",
        "lab_submissions",
    ):
        assert after["immutable_evidence"][table] == before["immutable_evidence"][table]
    with sqlite3.connect(database_path) as connection:
        target = connection.execute(
            "SELECT application_url, target_status, resolution_evidence_json "
            "FROM opportunities WHERE id=?",
            (verified_id,),
        ).fetchone()
    assert target == (
        "https://jobs.example.com/verified",
        TargetKind.APPLICATION_ENTRY.value,
        '{"identity_verified":true}',
    )


def test_apply_backup_precedes_live_database_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _database_path(tmp_path)
    events: list[str] = []
    real_database = repair.Database
    real_backup = repair.create_verified_backup

    class ObservedDatabase(real_database):
        def __init__(self, settings: Settings):
            if settings.data_dir.resolve() == database_path.parent.resolve():
                events.append("live-database-constructed")
            super().__init__(settings)

    def observed_backup(path: Path, directory: Path):
        events.append("backup-verified")
        return real_backup(path, directory)

    monkeypatch.setattr(repair, "Database", ObservedDatabase)
    monkeypatch.setattr(repair, "create_verified_backup", observed_backup)

    run_identity_repair(
        database_path,
        [_row("ordered-id")],
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    assert events.index("backup-verified") < events.index("live-database-constructed")


def test_failed_live_invariant_rolls_back_business_rows_but_keeps_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _database_path(tmp_path)
    before = repair.database_snapshot(database_path)
    backup_dir = tmp_path / "backups"
    real_assert = repair._assert_repair_invariants
    calls = 0

    def fail_live_transaction(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected live invariant failure")
        return real_assert(*args, **kwargs)

    monkeypatch.setattr(repair, "_assert_repair_invariants", fail_live_transaction)
    with pytest.raises(RuntimeError, match="injected live invariant failure"):
        run_identity_repair(
            database_path,
            [_row("rollback-id")],
            apply=True,
            backup_dir=backup_dir,
        )

    after = repair.database_snapshot(database_path)
    assert after["opportunity_ids"] == before["opportunity_ids"]
    assert after["application_bindings"] == before["application_bindings"]
    assert after["trackr_bindings"] == before["trackr_bindings"]
    backups = list(backup_dir.glob("*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_second_apply_same_capture_is_noop_and_creates_separate_backup(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    backup_dir = tmp_path / "backups"
    rows = [_row("repeat-id")]
    first = run_identity_repair(
        database_path, rows, apply=True, backup_dir=backup_dir
    )
    first_business = repair._business_projection(first["after"])

    second = run_identity_repair(
        database_path, rows, apply=True, backup_dir=backup_dir
    )

    assert second["identity_dispositions"]["existing_bound"] == 1
    assert second["identity_dispositions"]["claim_legacy"] == 0
    assert second["identity_dispositions"]["insert_new"] == 0
    assert second["ingest"]["updated"] == 0
    assert second["idempotency"]["passed"] is True
    assert repair._business_projection(second["after"]) == first_business
    assert first["backup"]["path"] != second["backup"]["path"]
    assert len(list(backup_dir.glob("*.bak"))) == 2


def test_window_backfill_after_repair_preserves_every_trackr_identity(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    rows = [_row("backfill-safe-id")]
    run_identity_repair(
        database_path,
        rows,
        apply=True,
        backup_dir=tmp_path / "identity-backups",
    )
    before = repair.database_snapshot(database_path)

    backfill = run_backfill(
        database_path,
        rows,
        apply=True,
        backups_dir=tmp_path / "window-backups",
    )
    after = repair.database_snapshot(database_path)

    assert backfill["rows_deleted"] == 0
    assert backfill["applications_deleted"] == 0
    assert after["opportunity_ids"] == before["opportunity_ids"]
    assert after["application_bindings"] == before["application_bindings"]
    assert after["trackr_bindings"] == before["trackr_bindings"]


def test_repair_leaves_integrity_foreign_keys_and_unique_partial_index_valid(
    tmp_path: Path,
) -> None:
    database_path = _database_path(tmp_path)
    run_identity_repair(
        database_path,
        [_row("unique-index-id")],
        apply=True,
        backup_dir=tmp_path / "backups",
    )

    snapshot = repair.database_snapshot(database_path)
    assert snapshot["integrity"] == "ok"
    assert snapshot["foreign_key_errors"] == 0
    assert snapshot["trackr_identity_duplicates"] == {}
    assert snapshot["trackr_identity_index_valid"] is True
    with sqlite3.connect(database_path) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='uq_opportunities_trackr_programme_id'"
        ).fetchone()[0]
    assert "UNIQUE INDEX" in sql.upper()
    assert "WHERE trackr_programme_id IS NOT NULL" in sql
