"""Archive synthetic employer rows (ARGUS Test Capital) with reason synthetic_test_fixture.

Uses the existing OpportunityArchive model (reversible archive path). Does not
add a new archive mechanism. Does not delete rows.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Add project root to path so we can import app modules
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault(
    "ARGUS_DATA_DIR",
    str(Path(os.environ.get("LOCALAPPDATA", "/c/Users/demo/AppData/Local")) / "ARGUS"),
)

from app.config import Settings
from app.db import Database
from app.models import Opportunity, OpportunityArchive
from app.security.audit import AuditInput, append_audit
from sqlalchemy import func, select

SYNTHETIC_EMPLOYER = "ARGUS Test Capital"
ARCHIVE_REASON = "synthetic_test_fixture"


def backup_db(database_path: Path, backup_dir: Path) -> Path:
    """Create a verified backup before writing."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"argus.db.pre-synthetic-archive-{uuid4().hex}.bak"
    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(descriptor)
        with closing(
            sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
        ) as source:
            with closing(sqlite3.connect(backup_path)) as destination:
                source.backup(destination)
                destination.commit()
        # Verify integrity
        with closing(sqlite3.connect(backup_path)) as verifier:
            integrity = verifier.execute("PRAGMA integrity_check").fetchall()
            if not integrity or any(str(r[0]).casefold() != "ok" for r in integrity):
                raise RuntimeError(f"Backup integrity check failed: {integrity!r}")
            fk_errors = verifier.execute("PRAGMA foreign_key_check").fetchall()
            if fk_errors:
                raise RuntimeError(f"Backup foreign key check failed: {fk_errors!r}")
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def main() -> int:
    settings = Settings.load()
    database_path = settings.data_dir / "argus.db"
    backup_dir = settings.data_dir / "backups"

    if not database_path.is_file():
        print(f"ERROR: database not found: {database_path}", file=sys.stderr)
        return 2

    # ---- Step 1: Backup ----
    print(f"Database: {database_path}")
    backup_path = backup_db(database_path, backup_dir)
    print(f"Backup:   {backup_path}")
    print()

    # ---- Step 2: Archive synthetic rows ----
    database = Database(settings)
    database.create_schema()

    with database.session_scope() as session:
        # Get counts before
        total_opps_before = session.scalar(select(func.count()).select_from(Opportunity)) or 0
        total_apps_before = session.scalar(select(func.count()).select_from(Application)) if "Application" in dir() else 0
        from app.models import Application
        total_apps_before = session.scalar(select(func.count()).select_from(Application)) or 0
        archive_meta_before = session.scalar(select(func.count()).select_from(OpportunityArchive)) or 0
        active_archives_before = (
            session.scalar(
                select(func.count())
                .select_from(OpportunityArchive)
                .where(OpportunityArchive.archived_at.is_not(None))
            )
            or 0
        )

        # Find synthetic opportunities
        synthetic_opps = session.scalars(
            select(Opportunity).where(Opportunity.employer == SYNTHETIC_EMPLOYER)
        ).all()

        print(f"Synthetic opportunities found: {len(synthetic_opps)}")
        for opp in synthetic_opps:
            print(f"  {opp.id} | {opp.employer} | {opp.role_title} | {opp.location or '(no location)'}")

        if not synthetic_opps:
            print("Nothing to archive.")
            return 0

        # Archive each one
        archived_at = datetime.now(timezone.utc)
        for opp in synthetic_opps:
            marker = session.get(OpportunityArchive, opp.id)
            if marker is None:
                marker = OpportunityArchive(opportunity_id=opp.id)
                session.add(marker)
            marker.archived_at = archived_at
            marker.archived_reason = ARCHIVE_REASON
            append_audit(
                session,
                AuditInput(
                    "maintenance",
                    "opportunity.archived",
                    "opportunity",
                    opp.id,
                    {
                        "employer": opp.employer,
                        "reason": ARCHIVE_REASON,
                    },
                ),
            )

        session.flush()

        # Get counts after
        total_opps_after = session.scalar(select(func.count()).select_from(Opportunity)) or 0
        total_apps_after = session.scalar(select(func.count()).select_from(Application)) or 0
        archive_meta_after = session.scalar(select(func.count()).select_from(OpportunityArchive)) or 0
        active_archives_after = (
            session.scalar(
                select(func.count())
                .select_from(OpportunityArchive)
                .where(OpportunityArchive.archived_at.is_not(None))
            )
            or 0
        )

        # Verify opportunity/app count unchanged
        if total_opps_after != total_opps_before:
            raise RuntimeError(
                f"Opportunity row count changed: {total_opps_before} -> {total_opps_after}"
            )
        if total_apps_after != total_apps_before:
            raise RuntimeError(
                f"Application row count changed: {total_apps_before} -> {total_apps_after}"
            )

        print()
        print("=== ARCHIVE RESULT ===")
        print(f"Rows archived: {len(synthetic_opps)}")
        print(f"Opportunity rows: {total_opps_before} -> {total_opps_after}")
        print(f"Application rows: {total_apps_before} -> {total_apps_after}")
        print(f"Archive metadata rows: {archive_meta_before} -> {archive_meta_after}")
        print(f"Active archives: {active_archives_before} -> {active_archives_after}")
        print()

        # Verify the archive is applied
        archived_tc = session.scalars(
            select(Opportunity)
            .join(OpportunityArchive, OpportunityArchive.opportunity_id == Opportunity.id)
            .where(
                Opportunity.employer == SYNTHETIC_EMPLOYER,
                OpportunityArchive.archived_at.is_not(None),
                OpportunityArchive.archived_reason == ARCHIVE_REASON,
            )
        ).all()
        print(f"Verified archived synthetic rows: {len(archived_tc)}/{len(synthetic_opps)}")

    database.engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())