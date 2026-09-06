"""Safely remove known tracking parameters from stored source URLs.

The default mode is a read-only dry run. Applying a report requires an
explicit, nonexistent backup path; a consistent SQLite backup is completed
before the first update. Database, backup, and report targets must be pairwise
distinct after alias normalization. Apply-mode reports are staged and
published inside the SQLite transaction so a report failure rolls back the
live update. Only ``opportunities.url`` (the source URL) is ever updated.
``application_url`` is deliberately neither read as a candidate nor written,
because an application target must come from verified resolution.

Examples::

    python scripts/fix_tracking_urls.py --report repair.json
    python scripts/fix_tracking_urls.py --apply --backup backups/before-repair.db --report repair.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from app.config import Settings
from app.domain.targets import strip_tracking_parameters


@dataclass(frozen=True, slots=True)
class RepairChange:
    opportunity_id: str
    employer: str
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class InvalidSource:
    opportunity_id: str
    employer: str
    source_url: str
    error: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite database to inspect (defaults to ARGUS_DATA_DIR/argus.db)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="JSON report path (required with --apply)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the reported source-URL changes (default: dry run)",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        help="Required with --apply; must name a file that does not exist",
    )
    return parser


_TRACKER_PATH_AMPERSAND_RE = re.compile(r"^(https?://[^/]+/[^?#]*?)(&)([a-z_]+=)")


def _fix_query_separator(raw: str) -> str:
    """Replace the first path-embedded ``&`` with ``?`` before a query-like param.

    Corrupted URLs like
    ``https://job-boards.greenhouse.io/liontree/jobs/5213296007&gh_src=Trackr``
    embed query-string parameters in the path using ``&`` where ``?`` belongs.
    Only URLs that have NO existing query string are repaired.
    """
    if "?" in raw:
        return raw  # already has a valid query separator
    match = _TRACKER_PATH_AMPERSAND_RE.match(raw)
    if match:
        return f"{match.group(1)}?{match.group(3)}{raw[match.end():]}"
    return raw


def _scan(
    connection: sqlite3.Connection,
) -> tuple[list[RepairChange], list[InvalidSource], int]:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(opportunities)").fetchall()
    }
    required = {"id", "employer", "url"}
    if not required <= columns:
        missing = ", ".join(sorted(required - columns))
        raise RuntimeError(f"opportunities table is missing required columns: {missing}")

    rows = connection.execute(
        "SELECT id, employer, url, application_url FROM opportunities ORDER BY id"
    ).fetchall()
    changes: list[RepairChange] = []
    application_url_changes: list[RepairChange] = []
    invalid: list[InvalidSource] = []
    for opportunity_id, employer, source_url, application_url in rows:
        original = str(source_url or "")
        try:
            repaired = strip_tracking_parameters(_fix_query_separator(original))
        except ValueError as exc:
            invalid.append(
                InvalidSource(
                    opportunity_id=str(opportunity_id),
                    employer=str(employer),
                    source_url=original,
                    error=str(exc),
                )
            )
            continue
        if repaired != original:
            changes.append(
                RepairChange(
                    opportunity_id=str(opportunity_id),
                    employer=str(employer),
                    before=original,
                    after=repaired,
                )
            )
        # Apply the same tracking-parameter and query-separator fix to
        # application_url when it is non-empty and different from the
        # source URL (which has already been repaired above).
        if application_url is not None:
            app_original = str(application_url)
            try:
                app_repaired = strip_tracking_parameters(
                    _fix_query_separator(app_original)
                )
            except ValueError:
                continue
            if app_repaired != app_original:
                application_url_changes.append(
                    RepairChange(
                        opportunity_id=str(opportunity_id),
                        employer=str(employer),
                        before=app_original,
                        after=app_repaired,
                    )
                )
    return changes, application_url_changes, invalid, len(rows)


def _target_key(path: Path) -> str:
    """Return a case-insensitive, symlink-resolved identity for a file target."""

    return str(path.expanduser().resolve(strict=False)).replace("\\", "/").casefold()


def _targets_are_same(left: Path, right: Path) -> bool:
    if _target_key(left) == _target_key(right):
        return True
    # ``resolve(strict=False)`` handles normal aliases, while samefile also
    # catches an existing hard-link alias without weakening the non-existent
    # backup-path requirement.
    if left.exists() and right.exists():
        try:
            return os.path.samefile(left, right)
        except OSError:
            return False
    return False


def _validate_distinct_targets(
    database_path: Path,
    backup_path: Path | None,
    report_path: Path | None,
) -> None:
    targets = [("database", database_path)]
    if backup_path is not None:
        targets.append(("backup", backup_path))
    if report_path is not None:
        targets.append(("report", report_path))
    for index, (left_name, left_path) in enumerate(targets):
        for right_name, right_path in targets[index + 1 :]:
            if _targets_are_same(left_path, right_path):
                raise ValueError(
                    f"{left_name} and {right_name} paths must be pairwise distinct"
                )


def _consistent_backup(database_path: Path, backup_path: Path) -> None:
    """Create a non-overwriting SQLite backup, removing partial output on error."""

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_path.exists() or backup_path.is_symlink():
        raise FileExistsError(f"refusing to overwrite backup: {backup_path}")
    descriptor = os.open(
        str(backup_path),
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    os.close(descriptor)
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(database_path)
        destination = sqlite3.connect(backup_path)
        source.backup(destination)
        destination.commit()
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


def _stage_report(path: Path, payload: dict[str, object]) -> Path:
    """Preflight report serialization into a same-directory durable temp file."""

    if path.exists():
        if path.is_dir():
            raise IsADirectoryError(f"report target is a directory: {path}")
        if not path.is_file():
            raise OSError(f"report target is not a regular file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.parent.is_dir():
        raise NotADirectoryError(f"report parent is not a directory: {path.parent}")
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _sync_directory(path: Path) -> None:
    """Best-effort directory durability for platforms that expose it."""

    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _publish_staged_report(temporary: Path, destination: Path) -> None:
    """Atomically replace the final report with a same-directory stage."""

    os.replace(temporary, destination)
    _sync_directory(destination.parent)


def _snapshot_report(path: Path) -> Path | None:
    """Preserve an existing report so a later DB failure can restore it."""

    if not path.exists():
        return None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".previous", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle, path.open("rb") as source:
            shutil.copyfileobj(source, handle)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _restore_report(path: Path, previous: Path | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        os.replace(previous, path)
        _sync_directory(path.parent)


def _apply(
    database_path: Path,
    changes: Sequence[RepairChange],
    application_url_changes: Sequence[RepairChange] = (),
    *,
    staged_report: Path | None = None,
    report_path: Path | None = None,
) -> int:
    connection = sqlite3.connect(database_path, timeout=30)
    applied = 0
    previous_report: Path | None = None
    report_published = False
    try:
        if staged_report is not None:
            if report_path is None:
                raise ValueError("staged report requires a report path")
            previous_report = _snapshot_report(report_path)
        connection.execute("BEGIN IMMEDIATE")
        for change in changes:
            cursor = connection.execute(
                "UPDATE opportunities SET url = ? WHERE id = ? AND url = ?",
                (change.after, change.opportunity_id, change.before),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "database changed after the report was built; no repair was committed"
                )
            applied += 1
        for change in application_url_changes:
            cursor = connection.execute(
                "UPDATE opportunities SET application_url = ? WHERE id = ? AND application_url = ?",
                (change.after, change.opportunity_id, change.before),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "database changed after the report was built; no repair was committed"
                )
            applied += 1
        if staged_report is not None and report_path is not None:
            _publish_staged_report(staged_report, report_path)
            report_published = True
        connection.commit()
    except BaseException:
        connection.rollback()
        if report_published and report_path is not None:
            _restore_report(report_path, previous_report)
        raise
    finally:
        try:
            connection.close()
        finally:
            if previous_report is not None and previous_report.exists():
                try:
                    previous_report.unlink(missing_ok=True)
                except OSError:
                    # The live transaction and report have already reached
                    # their terminal state. Cleanup is deliberately
                    # best-effort and must not turn success into failure.
                    pass
    return applied


def _write_report(path: Path, payload: dict[str, object]) -> None:
    temporary = _stage_report(path, payload)
    try:
        _publish_staged_report(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_path = (
        args.database.expanduser().resolve(strict=False)
        if args.database is not None
        else (Settings.load().data_dir / "argus.db").expanduser().resolve(strict=False)
    )
    backup_path: Path | None = (
        args.backup.expanduser().resolve(strict=False) if args.backup else None
    )
    report_path: Path | None = (
        args.report.expanduser().resolve(strict=False) if args.report else None
    )
    try:
        _validate_distinct_targets(database_path, backup_path, report_path)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: refusing overlapping targets: {exc}", file=sys.stderr)
        return 2

    if not database_path.is_file():
        print(f"error: database does not exist: {database_path}", file=sys.stderr)
        return 2

    if args.apply:
        if report_path is None:
            print("error: --apply requires --report", file=sys.stderr)
            return 2
        if backup_path is None:
            print("error: --apply requires --backup", file=sys.stderr)
            return 2
        if backup_path.exists() or backup_path.is_symlink():
            print(f"error: refusing to overwrite backup: {backup_path}", file=sys.stderr)
            return 2
    elif backup_path is not None:
        print("error: --backup requires --apply", file=sys.stderr)
        return 2

    try:
        read_only_uri = database_path.as_uri() + "?mode=ro"
        with sqlite3.connect(read_only_uri, uri=True) as read_connection:
            changes, application_url_changes, invalid, total = _scan(read_connection)
    except (RuntimeError, sqlite3.Error) as exc:
        print(f"error: could not inspect database: {exc}", file=sys.stderr)
        return 2

    payload: dict[str, object] = {
        "mode": "apply" if args.apply else "dry-run",
        "database": str(database_path),
        "backup": str(backup_path) if backup_path else None,
        "total": total,
        "changed": len(changes),
        # The update set is deterministic after the read-only scan.  Staging
        # this exact report before BEGIN IMMEDIATE means a report failure can
        # never follow a committed live mutation.
        "applied": (len(changes) + len(application_url_changes)) if args.apply else 0,
        "invalid": len(invalid),
        "changes": [asdict(change) for change in changes],
        "invalid_sources": [asdict(item) for item in invalid],
        "application_url_changes": [asdict(change) for change in application_url_changes],
        "application_urls_promoted": len(application_url_changes),
    }

    staged_report: Path | None = None
    applied = 0
    try:
        if report_path is not None:
            # Serialization, parent creation, and an fsynced same-directory
            # stage all happen before the backup or live transaction.
            staged_report = _stage_report(report_path, payload)
        if args.apply:
            assert backup_path is not None
            _consistent_backup(database_path, backup_path)
            applied = _apply(
                database_path,
                changes,
                application_url_changes,
                staged_report=staged_report,
                report_path=report_path,
            )
            staged_report = None  # moved atomically by _apply
        elif staged_report is not None and report_path is not None:
            _publish_staged_report(staged_report, report_path)
            staged_report = None
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"error: repair aborted: {exc}", file=sys.stderr)
        return 2
    finally:
        if staged_report is not None:
            staged_report.unlink(missing_ok=True)

    payload["applied"] = applied

    print(
        f"mode={payload['mode']} total={total} changed={len(changes)} "
        f"applied={applied} invalid={len(invalid)} "
        f"application_url_changed={len(application_url_changes)}"
    )
    if backup_path is not None and args.apply:
        print(f"backup={backup_path}")
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(main())
