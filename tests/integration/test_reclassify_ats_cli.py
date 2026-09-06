from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

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


def _seed_opportunity(
    employer: str,
    url: str,
    ats_type: str,
) -> Opportunity:
    return Opportunity(
        employer=employer,
        role_title=f"{employer} Summer Internship",
        cycle="2027",
        url=url,
        source="reclassify_test",
        ats_type=ats_type,
    )


def _classifications(database: Database) -> dict[str, str]:
    with Session(database.engine) as session:
        rows = session.scalars(select(Opportunity).order_by(Opportunity.employer)).all()
        return {row.employer: row.ats_type for row in rows}


def test_reclassify_ats_cli_is_dry_run_safe_and_idempotent(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        session.add_all(
            [
                _seed_opportunity(
                    "Travelers",
                    "https://travelers.wd5.myworkdayjobs.com/en-US/External/job/Intern_R1",
                    "unknown",
                ),
                _seed_opportunity(
                    "Acme Workable",
                    "https://apply.workable.com/acme/j/ABC123/",
                    "lever",
                ),
                _seed_opportunity(
                    "Short Link",
                    "https://grnh.se/twyh9job1us",
                    "unknown",
                ),
                _seed_opportunity(
                    "Custom Known",
                    "https://gic.careers/jobs/summer-internship",
                    "lever",
                ),
                _seed_opportunity(
                    "Custom Unknown",
                    "https://higher.gs.com/campus?type=internships",
                    "unknown",
                ),
            ]
        )

    with Session(database.engine) as session:
        append_audit(
            session,
            AuditInput(
                "test",
                "pending.before_dry_run",
                "fixture",
                "pending-1",
                {"safe": True},
            ),
        )
        session.commit()
    database.engine.dispose()
    database_path = tmp_path / "argus.db"
    with sqlite3.connect(database_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    assert journal_mode.casefold() == "delete"
    bytes_before_dry_run = database_path.read_bytes()

    before = _classifications(database)
    dry_run = _run_cli(tmp_path, "reclassify-ats", "--dry-run")

    assert dry_run.returncode == 0, dry_run.stderr
    assert "Mode: DRY RUN (no database changes written)" in dry_run.stdout
    assert "Before ats_type histogram:" in dry_run.stdout
    assert "After ats_type histogram:" in dry_run.stdout
    assert "Rows changed: 3" in dry_run.stdout
    assert _classifications(database) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "delete"
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_outbox WHERE status='PENDING'"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    assert database_path.read_bytes() == bytes_before_dry_run

    first = _run_cli(tmp_path, "reclassify-ats")

    assert first.returncode == 0, first.stderr
    assert "Mode: APPLIED" in first.stdout
    assert "Rows changed: 3" in first.stdout
    expected = {
        "Acme Workable": "workable",
        "Custom Known": "lever",
        "Custom Unknown": "unknown",
        "Short Link": "greenhouse_shortlink",
        "Travelers": "workday",
    }
    assert _classifications(database) == expected
    with database.session_scope() as session:
        assert session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "opportunity.ats_reclassified")
        ) == 3
        assert session.scalar(
            select(func.count())
            .select_from(AuditOutbox)
            .where(AuditOutbox.status == AuditOutbox.PENDING)
        ) == 0

    second = _run_cli(tmp_path, "reclassify-ats")

    assert second.returncode == 0, second.stderr
    assert "Mode: APPLIED" in second.stdout
    assert "Rows changed: 0" in second.stdout
    assert _classifications(database) == expected
