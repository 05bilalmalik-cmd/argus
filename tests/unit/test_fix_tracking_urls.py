from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import scripts.fix_tracking_urls as repair_tool
from scripts.fix_tracking_urls import main


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE opportunities (
                id TEXT PRIMARY KEY,
                employer TEXT NOT NULL,
                role_title TEXT NOT NULL,
                cycle TEXT NOT NULL,
                url TEXT NOT NULL,
                application_url TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO opportunities VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "one",
                    "Acme",
                    "Analyst",
                    "2027",
                    "https://jobs.example.test/1?utm_source=mail&gh_src=feed",
                    "https://boards.example.test/apply/1?token=functional",
                ),
                (
                    "two",
                    "Beta",
                    "Intern",
                    "2027",
                    "https://jobs.example.test/2&gh_src=legacy-shape",
                    None,
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _row(path: Path, row_id: str) -> tuple[str, str | None]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT url, application_url FROM opportunities WHERE id = ?", (row_id,)
        ).fetchone()
    finally:
        connection.close()


def test_default_is_dry_run_and_writes_machine_readable_report(tmp_path: Path) -> None:
    database_path = tmp_path / "argus.db"
    report_path = tmp_path / "repair-report.json"
    _database(database_path)

    result = main(["--database", str(database_path), "--report", str(report_path)])

    assert result == 0
    assert _row(database_path, "one")[0].endswith(
        "?utm_source=mail&gh_src=feed"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "dry-run"
    # Both rows have url changes: row "one" has utm_source stripped,
    # row "two" has the path-embedded & fixed to ?.
    assert report["changed"] == 2
    assert len(report["changes"]) == 2
    # Row "two" had its path-embedded & repaired to ?
    two_change = [c for c in report["changes"] if c["opportunity_id"] == "two"][0]
    assert two_change["after"].endswith("?gh_src=legacy-shape")
    assert "&" not in two_change["after"].split("/")[-1]
    # Dry-run never touches the database
    assert _row(database_path, "two")[0].endswith("/2&gh_src=legacy-shape")


def test_apply_requires_an_explicit_nonexistent_backup_path(tmp_path: Path) -> None:
    database_path = tmp_path / "argus.db"
    _database(database_path)

    assert main(["--database", str(database_path), "--apply"]) == 2
    assert _row(database_path, "one")[0].endswith(
        "?utm_source=mail&gh_src=feed"
    )

    existing_backup = tmp_path / "existing.db"
    existing_backup.write_bytes(b"do not overwrite")
    assert (
        main(
            [
                "--database",
                str(database_path),
                "--apply",
                "--backup",
                str(existing_backup),
            ]
        )
        == 2
    )
    assert existing_backup.read_bytes() == b"do not overwrite"


def test_apply_requires_report_before_any_backup_or_database_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "argus.db"
    backup_path = tmp_path / "before.db"
    _database(database_path)
    before = database_path.read_bytes()

    result = main(
        [
            "--database",
            str(database_path),
            "--apply",
            "--backup",
            str(backup_path),
        ]
    )

    assert result == 2
    assert database_path.read_bytes() == before
    assert not backup_path.exists()


def test_apply_backs_up_first_and_never_promotes_application_url(tmp_path: Path) -> None:
    database_path = tmp_path / "argus.db"
    backup_path = tmp_path / "backups" / "before-url-repair.db"
    report_path = tmp_path / "applied-report.json"
    _database(database_path)
    original_application_url = _row(database_path, "one")[1]

    result = main(
        [
            "--database",
            str(database_path),
            "--apply",
            "--backup",
            str(backup_path),
            "--report",
            str(report_path),
        ]
    )

    assert result == 0
    assert backup_path.is_file()
    assert _row(backup_path, "one")[0].endswith("?utm_source=mail&gh_src=feed")
    repaired_source, application_url = _row(database_path, "one")
    assert repaired_source.endswith("?gh_src=feed")
    assert application_url == original_application_url
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "apply"
    assert report["applied"] == 2  # 1 url + 1 url (row "two"'s path-embedded &)


def test_report_target_aliasing_database_is_refused_before_any_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "argus.db"
    _database(database_path)
    before = database_path.read_bytes()

    # The parent does not need to exist: resolve(strict=False) must still
    # collapse this relative alias to the database target.
    report_alias = tmp_path / "missing" / ".." / "argus.db"

    assert main(["--database", str(database_path), "--report", str(report_alias)]) == 2
    assert database_path.read_bytes() == before


def test_apply_report_target_database_is_refused_before_backup_or_update(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "argus.db"
    backup_path = tmp_path / "before.db"
    _database(database_path)
    before = database_path.read_bytes()

    assert (
        main(
            [
                "--database",
                str(database_path),
                "--apply",
                "--backup",
                str(backup_path),
                "--report",
                str(database_path),
            ]
        )
        == 2
    )
    assert database_path.read_bytes() == before
    assert not backup_path.exists()


def test_case_alias_between_database_and_report_is_refused(tmp_path: Path) -> None:
    database_path = tmp_path / "argus.db"
    _database(database_path)
    before = database_path.read_bytes()

    # Case-folding is intentional even on case-sensitive development hosts;
    # the packaged Windows application must not have a case alias escape hatch.
    report_alias = tmp_path / "ARGUS.DB"

    assert main(["--database", str(database_path), "--report", str(report_alias)]) == 2
    assert database_path.read_bytes() == before


def test_case_alias_between_database_and_backup_is_refused_before_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "argus.db"
    backup_alias = tmp_path / "ARGUS.DB"
    report_path = tmp_path / "repair.json"
    _database(database_path)
    before = database_path.read_bytes()

    result = main(
        [
            "--database",
            str(database_path),
            "--apply",
            "--backup",
            str(backup_alias),
            "--report",
            str(report_path),
        ]
    )

    assert result == 2
    assert database_path.read_bytes() == before
    assert not report_path.exists()


def test_apply_rejects_backup_report_alias_before_creating_backup(tmp_path: Path) -> None:
    database_path = tmp_path / "argus.db"
    _database(database_path)
    before = database_path.read_bytes()
    same_target = tmp_path / "repair-output"

    result = main(
        [
            "--database",
            str(database_path),
            "--apply",
            "--backup",
            str(same_target),
            "--report",
            str(same_target),
        ]
    )

    assert result == 2
    assert database_path.read_bytes() == before
    assert not same_target.exists()


def test_symlink_alias_between_database_and_report_is_refused(tmp_path: Path) -> None:
    database_path = tmp_path / "argus.db"
    _database(database_path)
    alias = tmp_path / "database-alias.db"
    try:
        alias.symlink_to(database_path)
    except NotImplementedError:
        pytest.skip("this operating system does not implement symlinks")
    except OSError as exc:
        # Windows without SeCreateSymbolicLinkPrivilege reports WinError 1314.
        # Any other failure is a real test failure, not a permissible skip.
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation requires unavailable privilege")
        raise

    before = database_path.read_bytes()
    assert main(["--database", str(database_path), "--report", str(alias)]) == 2
    assert database_path.read_bytes() == before


def test_report_publish_failure_rolls_back_live_database_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "argus.db"
    backup_path = tmp_path / "before.db"
    report_path = tmp_path / "repair.json"
    _database(database_path)
    before = database_path.read_bytes()

    real_replace = repair_tool.os.replace

    def refuse_report_publish(source: str | bytes, destination: str | bytes) -> None:
        if Path(destination).resolve(strict=False) == report_path.resolve(strict=False):
            raise OSError("simulated report publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(repair_tool.os, "replace", refuse_report_publish)

    result = main(
        [
            "--database",
            str(database_path),
            "--apply",
            "--backup",
            str(backup_path),
            "--report",
            str(report_path),
        ]
    )

    assert result == 2
    assert database_path.read_bytes() == before
    assert backup_path.is_file()
    assert not report_path.exists()


def test_previous_snapshot_cleanup_failure_does_not_mask_committed_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "argus.db"
    backup_path = tmp_path / "before.db"
    report_path = tmp_path / "repair.json"
    _database(database_path)
    report_path.write_bytes(b"previous report")
    original_unlink = Path.unlink

    def refuse_previous_cleanup(self: Path, missing_ok: bool = False) -> None:
        if self.name.endswith(".previous"):
            raise OSError("simulated previous-report cleanup failure")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_previous_cleanup)

    result = main(
        [
            "--database",
            str(database_path),
            "--apply",
            "--backup",
            str(backup_path),
            "--report",
            str(report_path),
        ]
    )

    assert result == 0
    assert _row(database_path, "one")[0].endswith("?gh_src=feed")
    assert json.loads(report_path.read_text(encoding="utf-8"))["applied"] == 2


def test_commit_failure_restores_previous_report_and_rolls_back_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "argus.db"
    backup_path = tmp_path / "before.db"
    report_path = tmp_path / "repair.json"
    _database(database_path)
    report_path.write_bytes(b"previous report")
    before = _row(database_path, "one")
    real_connect = repair_tool.sqlite3.connect
    database_connections = 0

    class CommitFailingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def __getattr__(self, name: str) -> object:
            return getattr(self._connection, name)

        def commit(self) -> None:
            raise sqlite3.OperationalError("simulated commit failure")

    def tracked_connect(database: object, *args: object, **kwargs: object) -> object:
        nonlocal database_connections
        if Path(str(database)).resolve(strict=False) == database_path.resolve(strict=False):
            database_connections += 1
            connection = real_connect(database, *args, **kwargs)
            if database_connections == 2:
                return CommitFailingConnection(connection)
            return connection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(repair_tool.sqlite3, "connect", tracked_connect)

    result = main(
        [
            "--database",
            str(database_path),
            "--apply",
            "--backup",
            str(backup_path),
            "--report",
            str(report_path),
        ]
    )

    assert result == 2
    assert _row(database_path, "one") == before
    assert report_path.read_bytes() == b"previous report"
    assert backup_path.is_file()


def test_backup_copy_failure_removes_partial_backup_and_leaves_database_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "argus.db"
    backup_path = tmp_path / "before.db"
    report_path = tmp_path / "repair.json"
    _database(database_path)
    before = database_path.read_bytes()
    real_connect = repair_tool.sqlite3.connect

    def fail_backup_destination(database: object, *args: object, **kwargs: object) -> object:
        if Path(str(database)).resolve(strict=False) == backup_path.resolve(strict=False):
            raise sqlite3.OperationalError("simulated backup-copy failure")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(repair_tool.sqlite3, "connect", fail_backup_destination)

    result = main(
        [
            "--database",
            str(database_path),
            "--apply",
            "--backup",
            str(backup_path),
            "--report",
            str(report_path),
        ]
    )

    assert result == 2
    assert database_path.read_bytes() == before
    assert not backup_path.exists()
    assert not report_path.exists()


# ---------- application_url repair tests (TASK 3) ----------


def _application_url_database(path: Path) -> None:
    """Seed a database with cases covering application_url repair."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE opportunities (
                id TEXT PRIMARY KEY,
                employer TEXT NOT NULL,
                role_title TEXT NOT NULL,
                cycle TEXT NOT NULL,
                url TEXT NOT NULL,
                application_url TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO opportunities VALUES (?, ?, ?, ?, ?, ?)",
            [
                # Row with corrupt application_url only
                (
                    "app_corrupt",
                    "Aquatic",
                    "Analyst",
                    "2027",
                    "https://boards.example.test/1?token=clean&gh_src=trackr",
                    "https://boards.example.test/apply/1&gh_src=Trackr",
                ),
                # Row with legitimate application_url (no repair needed)
                (
                    "app_clean",
                    "Beta",
                    "Intern",
                    "2027",
                    "https://jobs.example.test/2?a=1&b=2",
                    "https://boards.example.test/apply/2?a=1&b=2",
                ),
                # Row corrupt in BOTH columns
                (
                    "both_corrupt",
                    "LionTree",
                    "Analyst",
                    "2027",
                    "https://job-boards.greenhouse.io/liontree/jobs/5213296007&gh_src=Trackr",
                    "https://job-boards.greenhouse.io/liontree/jobs/apply/5213296007&gh_src=Trackr",
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _application_url_row(
    path: Path, row_id: str
) -> tuple[str, str | None]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT url, application_url FROM opportunities WHERE id = ?",
            (row_id,),
        ).fetchone()
    finally:
        connection.close()


def test_dry_run_reports_application_url_changes(tmp_path: Path) -> None:
    """A corrupt application_url is detected and reported in dry-run mode."""
    database_path = tmp_path / "argus.db"
    report_path = tmp_path / "repair-report.json"
    _application_url_database(database_path)

    result = main(
        ["--database", str(database_path), "--report", str(report_path)]
    )

    assert result == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "dry-run"
    assert report["application_urls_promoted"] == 2  # app_corrupt + both_corrupt
    assert report["changed"] == 1  # url changes: only both_corrupt
    assert len(report["application_url_changes"]) == 2

    # Verify both_corrupt appears in url changes AND application_url changes
    both_url_ids = {c["opportunity_id"] for c in report["changes"]}
    assert "both_corrupt" in both_url_ids
    both_app_ids = {c["opportunity_id"] for c in report["application_url_changes"]}
    assert "both_corrupt" in both_app_ids
    assert "app_corrupt" in both_app_ids

    # Verify the clean row has no changes
    app_clean_app_ids = {
        c["opportunity_id"]
        for c in report["application_url_changes"]
        if c["opportunity_id"] == "app_clean"
    }
    assert len(app_clean_app_ids) == 0

    # Dry-run never modifies the database
    assert _application_url_row(database_path, "app_corrupt") == (
        "https://boards.example.test/1?token=clean&gh_src=trackr",
        "https://boards.example.test/apply/1&gh_src=Trackr",
    )


def test_application_url_repair_applies_to_both_columns(tmp_path: Path) -> None:
    """Apply-mode repairs both url and application_url atomically."""
    database_path = tmp_path / "argus.db"
    backup_path = tmp_path / "before.db"
    report_path = tmp_path / "applied-report.json"
    _application_url_database(database_path)
    before_bytes = database_path.read_bytes()

    result = main(
        [
            "--database",
            str(database_path),
            "--apply",
            "--backup",
            str(backup_path),
            "--report",
            str(report_path),
        ]
    )

    assert result == 0
    assert backup_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["applied"] == 3  # 1 url + 2 application_url = 3

    # app_corrupt: only application_url was repaired
    url, app_url = _application_url_row(database_path, "app_corrupt")
    assert url.endswith("?token=clean&gh_src=trackr")  # already clean
    assert app_url.casefold().endswith("?gh_src=trackr".casefold())  # repaired

    # app_clean: neither column was changed
    url, app_url = _application_url_row(database_path, "app_clean")
    assert url.endswith("?a=1&b=2")
    assert app_url.endswith("?a=1&b=2")

    # both_corrupt: both columns repaired
    url, app_url = _application_url_row(database_path, "both_corrupt")
    assert "?" in url  # query separator repaired
    assert "?" in app_url  # query separator repaired
    assert url.casefold().endswith("?gh_src=trackr".casefold())
    assert app_url.casefold().endswith("?gh_src=trackr".casefold())

    # Backup preserves originals
    backup_url, backup_app_url = _application_url_row(backup_path, "both_corrupt")
    assert "&gh_src=" in backup_url  # original corrupt form preserved in backup
    assert "&gh_src=" in backup_app_url


def test_legitimate_query_string_on_application_url_is_left_alone(
    tmp_path: Path,
) -> None:
    """A legitimate ?a=1&b=2 on application_url is not touched."""
    database_path = tmp_path / "argus.db"
    report_path = tmp_path / "repair.json"
    _application_url_database(database_path)

    result = main(
        ["--database", str(database_path), "--report", str(report_path)]
    )

    assert result == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    app_clean_changes = [
        c
        for c in report["application_url_changes"]
        if c["opportunity_id"] == "app_clean"
    ]
    assert len(app_clean_changes) == 0
    # Verify the original is preserved
    _, app_url = _application_url_row(database_path, "app_clean")
    assert app_url == "https://boards.example.test/apply/2?a=1&b=2"
