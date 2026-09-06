"""Repair the accidental 22 August 2026 ARGUS requeue batch.

The historical script selected every ``BLOCKED`` application.  This command
uses the immutable audit rows from that specific batch as its selection
evidence, defaults to a read-only dry run, and only applies the legal
``ELIGIBILITY_CHECKED -> BLOCKED`` transition.  Rows that have progressed
since the batch are reported but left untouched.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState, InvalidTransition, validate_transition
from app.models import Application, AuditEvent
from app.security.audit import AuditInput, _hash_event, append_audit, verify_audit_chain

UTC = timezone.utc
BATCH_START = datetime(2026, 8, 22, 23, 34, 0, tzinfo=UTC)
BATCH_END = datetime(2026, 8, 22, 23, 35, 0, tzinfo=UTC)
BATCH_REASON = "allowlist and destination checks improved"
REQUEUE_EVENT = "application.requeued"
REPAIR_EVENT = "application.requeue_repaired"
SUMMARY_EVENT = "maintenance.requeue_repair"
BATCH_KEY = "2026-08-22T23:34:31Z:allowlist-and-destination-checks"


@dataclass(frozen=True, slots=True)
class RepairCandidate:
    source_event_id: int
    application_id: str
    source_created_at: str
    current_state: str
    risk_level: int
    action: str
    reason: str


@dataclass(frozen=True, slots=True)
class RepairPlan:
    candidates: tuple[RepairCandidate, ...]

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def to_block_count(self) -> int:
        return sum(item.action == "block" for item in self.candidates)

    @property
    def skipped_count(self) -> int:
        return self.candidate_count - self.to_block_count


@dataclass(frozen=True, slots=True)
class RepairReport:
    dry_run: bool
    candidate_count: int
    repaired_count: int
    skipped_count: int
    backup_path: Path | None
    candidates: tuple[RepairCandidate, ...] = ()


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _batch_event(event: AuditEvent, *, start: datetime, end: datetime) -> bool:
    if (
        event.event_type != REQUEUE_EVENT
        or event.actor != "maintenance"
        or event.entity_type != "application"
    ):
        return False
    created = _parse_timestamp(event.created_at)
    if created is None or not (start <= created < end):
        return False
    try:
        details = json.loads(event.details_json)
    except (TypeError, ValueError):
        return False
    return isinstance(details, dict) and details.get("reason") == BATCH_REASON


def _repaired_source_ids(session: Session) -> set[int]:
    repaired: set[int] = set()
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == REPAIR_EVENT)
    ).all()
    for event in events:
        try:
            details = json.loads(event.details_json)
            source_id = int(details.get("source_event_id"))
        except (TypeError, ValueError, AttributeError):
            continue
        repaired.add(source_id)
    return repaired


def plan_repair(
    session: Session,
    *,
    start: datetime = BATCH_START,
    end: datetime = BATCH_END,
) -> RepairPlan:
    """Build an evidence-backed, side-effect-free repair plan."""

    repaired_ids = _repaired_source_ids(session)
    events = session.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
    candidates: list[RepairCandidate] = []
    for event in events:
        if not _batch_event(event, start=start, end=end):
            continue
        application = session.get(Application, event.entity_id)
        if application is None:
            candidates.append(
                RepairCandidate(
                    source_event_id=event.id,
                    application_id=event.entity_id,
                    source_created_at=event.created_at,
                    current_state="MISSING",
                    risk_level=-1,
                    action="skip",
                    reason="application row is missing",
                )
            )
            continue
        state = application.state
        if event.id in repaired_ids:
            action, reason = "skip", "repair already recorded"
        elif state == ApplicationState.BLOCKED.value:
            action, reason = "skip", "already BLOCKED"
        elif state == ApplicationState.ELIGIBILITY_CHECKED.value:
            # Validate before presenting a plan.  If the domain graph ever
            # changes, this command fails closed instead of inventing a state.
            try:
                validate_transition(
                    ApplicationState(state), ApplicationState.BLOCKED
                )
            except (InvalidTransition, ValueError):
                action, reason = "skip", "domain transition is not permitted"
            else:
                action, reason = "block", "audited requeue batch still at eligibility gate"
        else:
            # This includes unresolved R4 rows in NEEDS_USER/FAILED_RETRYABLE
            # and every other advanced state.  Never downgrade such a row.
            action, reason = "skip", "application advanced after audited batch"
        candidates.append(
            RepairCandidate(
                source_event_id=event.id,
                application_id=application.id,
                source_created_at=event.created_at,
                current_state=state,
                risk_level=application.risk_level,
                action=action,
                reason=reason,
            )
        )
    return RepairPlan(tuple(candidates))


def _integrity_check(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {path}: {result}")
    finally:
        connection.close()


def _verify_audit_chain_read_only(path: Path) -> None:
    """Verify audit rows without loading the post-migration ORM schema."""

    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        previous_hash = ""
        rows = connection.execute(
            """
            SELECT id, created_at, actor, event_type, entity_type, entity_id,
                   details_json, previous_hash, event_hash
            FROM audit_events ORDER BY id
            """
        )
        for row in rows:
            (
                event_id,
                created_at,
                actor,
                event_type,
                entity_type,
                entity_id,
                details_json,
                stored_previous,
                stored_hash,
            ) = row
            expected_hash = _hash_event(
                previous_hash,
                created_at,
                actor,
                event_type,
                entity_type,
                entity_id,
                details_json,
            )
            if stored_previous != previous_hash or stored_hash != expected_hash:
                raise RuntimeError(
                    f"audit chain is broken at event {event_id}; refusing repair"
                )
            previous_hash = stored_hash
    finally:
        connection.close()


def _consistent_backup(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"argus-requeue-repair-{stamp}-{uuid4().hex[:8]}.db"
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    _integrity_check(destination)
    return destination


def _inspection_copy(source: Path, destination: Path) -> None:
    """Copy a live/legacy SQLite file for read-only planning.

    SQLAlchemy models include the new nullable columns, so planning directly
    against a pre-migration database would fail before the dry-run could even
    report its candidates.  The online copy is migrated in a temporary
    directory; the source remains byte-for-byte untouched.
    """

    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()


def _summary_exists(session: Session) -> bool:
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == SUMMARY_EVENT)
    ).all()
    for event in events:
        try:
            details = json.loads(event.details_json)
        except (TypeError, ValueError):
            continue
        if isinstance(details, dict) and details.get("batch_key") == BATCH_KEY:
            return True
    return False


def _run_repair_session(
    database: Database,
    *,
    apply: bool,
    start: datetime,
    end: datetime,
    backup_path: Path | None,
) -> RepairReport:
    with database.session_scope() as session:
        verification = verify_audit_chain(session)
        if not verification.valid:
            raise RuntimeError(
                "audit chain is broken at event "
                f"{verification.broken_event_id}; refusing repair"
            )
        plan = plan_repair(session, start=start, end=end)
        repaired = 0
        if apply:
            for candidate in plan.candidates:
                if candidate.action != "block":
                    continue
                application = session.get(Application, candidate.application_id)
                if application is None or application.state != candidate.current_state:
                    continue
                validate_transition(
                    ApplicationState(application.state), ApplicationState.BLOCKED
                )
                previous_state = application.state
                application.state = ApplicationState.BLOCKED.value
                application.next_action = "Review accidental requeue repair before retry"
                append_audit(
                    session,
                    AuditInput(
                        "maintenance",
                        REPAIR_EVENT,
                        "application",
                        application.id,
                        {
                            "batch_key": BATCH_KEY,
                            "source_event_id": candidate.source_event_id,
                            "source_created_at": candidate.source_created_at,
                            "previous_state": previous_state,
                            "new_state": ApplicationState.BLOCKED.value,
                            "risk_level_preserved": application.risk_level,
                        },
                    ),
                )
                repaired += 1
            if not _summary_exists(session):
                append_audit(
                    session,
                    AuditInput(
                        "maintenance",
                        SUMMARY_EVENT,
                        "system",
                        "requeue-repair",
                        {
                            "batch_key": BATCH_KEY,
                            "source_event_count": plan.candidate_count,
                            "repaired_count": repaired,
                            "skipped_count": plan.skipped_count,
                            "backup_created": backup_path is not None,
                        },
                    ),
                )
    return RepairReport(
        dry_run=not apply,
        candidate_count=plan.candidate_count,
        repaired_count=repaired if apply else 0,
        skipped_count=plan.skipped_count,
        backup_path=backup_path,
        candidates=plan.candidates,
    )


def repair_database(
    settings: Settings,
    *,
    apply: bool = False,
    start: datetime = BATCH_START,
    end: datetime = BATCH_END,
    backup_dir: Path | None = None,
) -> RepairReport:
    """Plan or apply the repair; dry-run is the safe default."""

    database_path = settings.data_dir / "argus.db"
    if not database_path.is_file():
        raise FileNotFoundError(database_path)

    if not apply:
        # Keep the dry run genuinely read-only even for a legacy DB that does
        # not yet have the nullable metadata columns.
        with tempfile.TemporaryDirectory(prefix="argus-requeue-dry-run-") as directory:
            inspection_dir = Path(directory)
            inspection_path = inspection_dir / "argus.db"
            _inspection_copy(database_path, inspection_path)
            values = dict(os.environ)
            values["ARGUS_DATA_DIR"] = str(inspection_dir)
            inspection_settings = Settings.load(values)
            inspection_database = Database(inspection_settings)
            inspection_database.create_schema()
            try:
                return _run_repair_session(
                    inspection_database,
                    apply=False,
                    start=start,
                    end=end,
                    backup_path=None,
                )
            finally:
                inspection_database.engine.dispose()

    backup_path: Path | None = None
    _integrity_check(database_path)
    backup_path = _consistent_backup(
        database_path,
        backup_dir or settings.data_dir / "backups",
    )
    _verify_audit_chain_read_only(database_path)

    database = Database(settings)
    # Additive migration happens only after the consistent backup exists.
    database.create_schema()
    report = _run_repair_session(
        database,
        apply=True,
        start=start,
        end=end,
        backup_path=backup_path,
    )
    _integrity_check(database_path)
    with database.session_scope() as session:
        verification = verify_audit_chain(session)
        if not verification.valid:
            raise RuntimeError(
                f"audit chain failed after repair at event {verification.broken_event_id}"
            )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit and repair the accidental 2026-08-22 ARGUS requeue batch"
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--apply", action="store_true", help="Apply the repair (default is dry-run)"
    )
    parser.add_argument("--batch-start", type=str, default=BATCH_START.isoformat())
    parser.add_argument("--batch-end", type=str, default=BATCH_END.isoformat())
    parser.add_argument("--backup-dir", type=Path)
    return parser


def _parse_cli_timestamp(value: str) -> datetime:
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"invalid UTC timestamp: {value}")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    values = dict(os.environ)
    if args.data_dir is not None:
        values["ARGUS_DATA_DIR"] = str(args.data_dir.expanduser().resolve())
    settings = Settings.load(values)
    try:
        report = repair_database(
            settings,
            apply=args.apply,
            start=_parse_cli_timestamp(args.batch_start),
            end=_parse_cli_timestamp(args.batch_end),
            backup_dir=args.backup_dir,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"ARGUS repair refused: {exc}", file=os.sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry-run",
                "candidate_count": report.candidate_count,
                "repaired_count": report.repaired_count,
                "skipped_count": report.skipped_count,
                "backup_path": str(report.backup_path) if report.backup_path else None,
                "candidates": [asdict(item) for item in report.candidates],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
