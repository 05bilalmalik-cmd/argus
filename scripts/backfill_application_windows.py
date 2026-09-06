"""Backup-gated Trackr application-window backfill.

Dry-run is the default. Apply mode is implemented through the same bounded
ingestion path and never invokes target resolution, automation, or submission.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Sequence
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.models import (  # noqa: E402
    AnswerEntry,
    Application,
    AutomationRun,
    Document,
    EmailMessage,
    LabSubmission,
    Opportunity,
    OpportunityArchive,
    QuestionRecord,
    SubmissionAuthority,
    SubmissionIntent,
    SubmissionReviewBinding,
)
from app.scouting.application_window import tracker_owned_host  # noqa: E402
from app.scouting.service import IngestibleOpportunity, ScoutService  # noqa: E402
from app.scouting.trackr_live import (  # noqa: E402
    SLUG_TO_TYPE,
    TRACKER_URL,
    fetch_programmes,
)
from app.security.crypto import CryptoBox  # noqa: E402


def _readonly_connection(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _assert_integrity(connection: sqlite3.Connection, *, label: str) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if not integrity or any(str(row[0]).casefold() != "ok" for row in integrity):
        raise RuntimeError(f"{label} integrity check failed: {integrity!r}")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RuntimeError(f"{label} foreign-key check failed: {foreign_keys!r}")


def _online_copy(source_path: Path, destination_path: Path) -> Path:
    source_path = source_path.resolve()
    destination_path = destination_path.resolve()
    if destination_path.exists():
        raise FileExistsError(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_readonly_connection(source_path)) as source:
        _assert_integrity(source, label="source")
        with closing(sqlite3.connect(destination_path)) as destination:
            source.backup(destination)
            destination.commit()
            _assert_integrity(destination, label="backup")
    return destination_path


def create_verified_backup(database_path: Path, backups_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backups_dir / (
        f"{database_path.name}.pre-phase19-application-window-"
        f"{stamp}-{uuid4().hex}.bak"
    )
    return _online_copy(database_path, backup_path)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _table_count(connection: sqlite3.Connection, name: str) -> int:
    if not _table_exists(connection, name):
        return 0
    return int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])


_PRESERVED_EVIDENCE_TABLES = {
    "opportunities": Opportunity,
    "applications": Application,
    "documents": Document,
    "answer_entries": AnswerEntry,
    "automation_runs": AutomationRun,
    "question_records": QuestionRecord,
    "email_messages": EmailMessage,
    "submission_intents": SubmissionIntent,
    "submission_authorities": SubmissionAuthority,
    "submission_review_bindings": SubmissionReviewBinding,
    "lab_submissions": LabSubmission,
}


def _session_table_counts(session) -> dict[str, int]:
    """Count evidence rows inside the active backfill transaction."""

    return {
        name: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for name, model in _PRESERVED_EVIDENCE_TABLES.items()
    }


def database_snapshot(database_path: Path) -> dict[str, object]:
    with closing(_readonly_connection(database_path)) as connection:
        _assert_integrity(connection, label="database")
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(opportunities)").fetchall()
        }
        total = _table_count(connection, "opportunities")
        has_window = "application_window_status" in columns
        state_expression = (
            "COALESCE(NULLIF(application_window_status, ''), 'UNKNOWN')"
            if has_window
            else "'UNKNOWN'"
        )
        window_states = Counter(
            {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    f"SELECT {state_expression}, COUNT(*) FROM opportunities "
                    f"GROUP BY {state_expression}"
                ).fetchall()
            }
        )
        by_programme: dict[str, dict[str, int]] = defaultdict(dict)
        for programme, state, count in connection.execute(
            f"SELECT programme_group, {state_expression}, COUNT(*) "
            f"FROM opportunities GROUP BY programme_group, {state_expression}"
        ).fetchall():
            by_programme[str(programme or "")] [str(state)] = int(count)
        link_rows = connection.execute(
            "SELECT url, application_url FROM opportunities"
            if "application_url" in columns
            else "SELECT url, NULL AS application_url FROM opportunities"
        ).fetchall()
        real_links = 0
        captured_employer_application_urls = 0
        tracker_sources = 0
        for source_url, application_url in link_rows:
            source = str(source_url or "")
            candidate = str(application_url or "")
            if tracker_owned_host(source):
                tracker_sources += 1
            if candidate and not tracker_owned_host(candidate):
                captured_employer_application_urls += 1
                real_links += 1
            elif source and not tracker_owned_host(source):
                real_links += 1
        active_archives = 0
        if _table_exists(connection, "opportunity_archives"):
            active_archives = int(
                connection.execute(
                    "SELECT COUNT(*) FROM opportunity_archives "
                    "WHERE archived_at IS NOT NULL"
                ).fetchone()[0]
            )
        return {
            "opportunities": total,
            "applications": _table_count(connection, "applications"),
            "archives": _table_count(connection, "opportunity_archives"),
            "active_archives": active_archives,
            "automation_runs": _table_count(connection, "automation_runs"),
            "question_records": _table_count(connection, "question_records"),
            "answer_entries": _table_count(connection, "answer_entries"),
            "documents": _table_count(connection, "documents"),
            "email_messages": _table_count(connection, "email_messages"),
            "submission_intents": _table_count(connection, "submission_intents"),
            "submission_authorities": _table_count(connection, "submission_authorities"),
            "submission_review_bindings": _table_count(
                connection, "submission_review_bindings"
            ),
            "lab_submissions": _table_count(connection, "lab_submissions"),
            "window_states": dict(sorted(window_states.items())),
            "by_programme": {
                programme: dict(sorted(states.items()))
                for programme, states in sorted(by_programme.items())
            },
            "real_employer_links": real_links,
            "captured_employer_application_urls": captured_employer_application_urls,
            "tracker_source_urls": tracker_sources,
            "deadlines": int(
                connection.execute(
                    "SELECT COUNT(*) FROM opportunities WHERE deadline IS NOT NULL"
                ).fetchone()[0]
            ),
            "rolling_true": int(
                connection.execute(
                    "SELECT COUNT(*) FROM opportunities WHERE rolling = 1"
                ).fetchone()[0]
            ),
            "rolling_false": int(
                connection.execute(
                    "SELECT COUNT(*) FROM opportunities WHERE rolling = 0"
                ).fetchone()[0]
            ),
        }


def _settings_for_database(database_path: Path) -> Settings:
    database_path = database_path.resolve()
    if database_path.name.casefold() != "argus.db":
        raise ValueError("Backfill database must be named argus.db")
    return Settings.load(
        {
            "ARGUS_DATA_DIR": str(database_path.parent),
            "ARGUS_AUTOMATION_MODE": "OFF",
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_ENABLE_TRACKR_LIVE": "false",
        }
    )


def _apply_rows(
    database_path: Path,
    rows: Sequence[IngestibleOpportunity],
) -> tuple[dict[str, int], dict[str, int]]:
    settings = _settings_for_database(database_path)
    settings.ensure_directories()
    database = Database(settings)
    try:
        database.create_schema()
        crypto = CryptoBox.from_path(settings.secret_key_path)
        with database.session_scope() as session:
            before_counts = _session_table_counts(session)
            before_archives = int(
                session.scalar(select(func.count()).select_from(OpportunityArchive)) or 0
            )
            stats = ScoutService(session, settings, crypto).ingest(
                rows,
                allow_trackr_identity_mutation=False,
            )
            legacy_window_backfill = ScoutService(
                session,
                settings,
                crypto,
            ).backfill_unknown_application_windows()
            session.flush()
            after_counts = _session_table_counts(session)
            for table, before_count in before_counts.items():
                after_count = after_counts[table]
                if after_count != before_count:
                    raise RuntimeError(
                        f"Backfill refused: {table} row count changed "
                        f"({before_count} -> {after_count})"
                    )
            after_archives = int(
                session.scalar(select(func.count()).select_from(OpportunityArchive)) or 0
            )
            if after_archives < before_archives:
                raise RuntimeError(
                    "Backfill refused: opportunity_archives row count decreased "
                    f"({before_archives} -> {after_archives})"
                )
            return stats, legacy_window_backfill
    finally:
        database.engine.dispose()


def run_backfill(
    database_path: Path,
    rows: Sequence[IngestibleOpportunity],
    *,
    apply: bool,
    backups_dir: Path,
) -> dict[str, object]:
    database_path = database_path.resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    before = database_snapshot(database_path)
    backup_path: Path | None = None
    if apply:
        backup_path = create_verified_backup(database_path, backups_dir.resolve())
        ingest, legacy_window_backfill = _apply_rows(database_path, rows)
        projected = database_snapshot(database_path)
    else:
        with TemporaryDirectory(prefix="argus-phase19-dry-run-") as temporary:
            dry_path = Path(temporary) / "argus.db"
            _online_copy(database_path, dry_path)
            ingest, legacy_window_backfill = _apply_rows(dry_path, rows)
            projected = database_snapshot(dry_path)
    rows_deleted = max(0, int(before["opportunities"]) - int(projected["opportunities"]))
    applications_deleted = max(
        0,
        int(before["applications"]) - int(projected["applications"]),
    )
    if rows_deleted or applications_deleted:
        raise RuntimeError("Backfill deletion invariant failed")
    return {
        "applied": apply,
        "backup_path": str(backup_path) if backup_path is not None else None,
        "before": before,
        "projected": projected,
        "after": projected if apply else before,
        "ingest": ingest,
        "legacy_window_backfill": legacy_window_backfill,
        "rows_deleted": rows_deleted,
        "applications_deleted": applications_deleted,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or apply the Trackr application-window backfill without "
            "deleting opportunities/applications or running target resolution."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write only after an integrity-checked SQLite backup is complete",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="explicit SQLite database path (defaults to ARGUS settings)",
    )
    return parser


def _validate_complete_cli_capture(
    rows: Sequence[IngestibleOpportunity],
) -> None:
    """Require complete, canonical evidence from every production Trackr source."""

    expected_slugs = set(SLUG_TO_TYPE)
    observed_slugs: set[str] = set()
    unexpected_sources: list[str] = []
    malformed_sources: list[str] = []
    for position, row in enumerate(rows):
        source = str(getattr(row, "source", "") or "")
        prefix, separator, slug = source.partition(":")
        if prefix != "trackr_live" or separator != ":" or slug not in SLUG_TO_TYPE:
            unexpected_sources.append(
                f"row {position} source={source!r}" if source else f"row {position} source=<empty>"
            )
            continue
        observed_slugs.add(slug)
        tracker_url = str(
            getattr(row, "tracker_url", getattr(row, "source_url", "")) or ""
        )
        if tracker_url != TRACKER_URL.format(slug=slug):
            malformed_sources.append(
                f"row {position} source={source!r} tracker_url"
            )
        programme_type = str(getattr(row, "programme_type", "") or "")
        if programme_type != SLUG_TO_TYPE[slug]:
            malformed_sources.append(
                f"row {position} source={source!r} programme_type"
            )
    missing_sources = sorted(expected_slugs - observed_slugs)
    if missing_sources or unexpected_sources or malformed_sources:
        raise ValueError(
            "Live Trackr capture is incomplete or malformed; "
            f"missing sources={missing_sources!r}; "
            f"unexpected sources={unexpected_sources!r}; "
            f"malformed sources={malformed_sources!r}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Capture completeness is established before any database path discovery or work.
    rows = fetch_programmes()
    try:
        _validate_complete_cli_capture(rows)
    except ValueError as exc:
        parser.error(str(exc))
    database_path = (
        args.database.resolve()
        if args.database is not None
        else (Settings.load().data_dir / "argus.db").resolve()
    )
    report = run_backfill(
        database_path,
        rows,
        apply=bool(args.apply),
        backups_dir=database_path.parent / "backups",
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
