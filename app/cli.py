from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import webbrowser
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

import uvicorn
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.automation.runner import AutomationRunner, SubmissionBlocked
from app.automation.types import RunMode
from app.config import Settings
from app.db import Database
from app.domain.opportunity_scope import classify_location_scope
from app.domain.states import user_status_exclusion_reason
from app.models import (
    Application,
    AuditChainState,
    AuditEvent,
    Document,
    Opportunity,
    OpportunityArchive,
)
from app.runtime_lock import (
    RuntimeAlreadyRunning,
    RuntimeLock,
    runtime_lock_path,
)
from app.security.audit import AuditInput, append_audit, verify_audit_chain
from app.security.crypto import CryptoBox
from app.scouting.application_window import ApplicationWindowStatus
from app.scouting.programmes import ProgrammeType, classify_programme
from app.scouting.sources.base import _ats_from_url
from app.scouting.trackr_live import SLUG_TO_TYPE
from app.services.documents import DocumentService
from app.services.cv_integrity import (
    CV_VARIANT_TAGS,
    MATRIX_CONTENT_COLUMNS,
    CvReselectionReport,
    apply_cv_tag_repairs,
    contradictory_selected_application_ids,
    create_verified_cv_backup,
    plan_cv_tag_repairs,
    planned_invariant_violations,
    reselect_applications,
    single_query_invariant_violations,
)
from app.services.opportunities import OpportunityService
from app.services.profile import ProfileService, ProfileUpdate
from app.services.batch_target_resolution import (
    BatchResolveOptions,
    BatchTargetResolutionDriver,
)

_DEMO_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _runtime() -> tuple[Settings, Database, CryptoBox]:
    settings = Settings.load()
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    crypto = CryptoBox.from_path(settings.secret_key_path)
    return settings, database, crypto


def _command_init(_: argparse.Namespace) -> int:
    settings, _, _ = _runtime()
    print("ARGUS initialised")
    print(f"  dashboard: http://{settings.host}:{settings.port}")
    print(f"  data:      {settings.data_dir}")
    print(f"  token:     {settings.api_token_path}")
    print("  mode:      live submission disabled by default")
    return 0


def _seed_opportunities(settings: Settings) -> tuple[Opportunity, ...]:
    scenarios = (
        ("standard", "Standard application"),
        ("sensitive", "Sensitive-question handoff"),
        ("essay", "Written-answer review"),
        ("assessment", "Assessment handoff"),
        ("mismatch", "Destination-mismatch guard"),
    )
    return tuple(
        Opportunity(
            employer="ARGUS Test Capital",
            role_title="Summer Analyst",
            division="Investment Banking",
            programme_group=scenario,
            location="London",
            cycle="2027",
            url=f"http://{settings.host}:{settings.port}/lab/ats/{scenario}",
            source="argus_demo_seed",
            ats_type="greenhouse",
            sponsorship_supported=True,
            cv_required=True,
            notes=f"Synthetic local ATS fixture: {title}. Never submit this as a real application.",
        )
        for scenario, title in scenarios
    )


def _command_seed(_: argparse.Namespace) -> int:
    settings, database, crypto = _runtime()
    with database.session_scope() as session:
        profile_service = ProfileService(session, crypto)
        profile = profile_service.get_model()
        if not profile.first_name and not profile.email:
            profile_service.update(
                ProfileUpdate(
                    first_name="Demo",
                    last_name="Candidate",
                    email="demo@argus.local",
                    university="ARGUS Local Laboratory",
                    degree="Synthetic Test Profile",
                    graduation_year=2029,
                    current_study_year="Demo only",
                    preferred_locations=("London",),
                    work_authorisation="LAB-ONLY DEMO VALUE — replace before any real use",
                    requires_sponsorship=False,
                    work_authorisation_approved=True,
                ),
                actor="demo_seed",
            )

        documents = DocumentService(session, settings.documents_dir)
        if session.scalar(select(Document).where(Document.sha256 != "")) is None:
            documents.store_bytes(
                filename="ARGUS_Demo_CV.pdf",
                content=_DEMO_PDF,
                kind="cv",
                tags=("investment-banking", "london", "summer-analyst"),
                approved=True,
                actor="demo_seed",
            )

        opportunity_service = OpportunityService(session)
        for opportunity in _seed_opportunities(settings):
            opportunity_service.add(opportunity, actor="demo_seed")

    print("ARGUS demo fixture ready: 1 profile, 1 approved lab CV, 5 ATS scenarios.")
    print("The fixture is synthetic and live submission remains disabled.")
    return 0


def _load_seed_profile(args: argparse.Namespace) -> ProfileUpdate:
    """Load a candidate profile only from an explicit file or environment value."""

    profile_path = getattr(args, "profile", None)
    configured_path = os.environ.get("ARGUS_PROFILE_PATH", "").strip()
    inline_json = os.environ.get("ARGUS_PROFILE_JSON", "").strip()
    if profile_path is not None and (configured_path or inline_json):
        raise ValueError(
            "seed-real accepts one profile source: --profile, ARGUS_PROFILE_PATH, "
            "or ARGUS_PROFILE_JSON"
        )
    if profile_path is not None:
        source = Path(profile_path).expanduser().resolve()
        try:
            payload: object = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("seed-real profile file could not be read") from exc
    elif configured_path:
        source = Path(configured_path).expanduser().resolve()
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("ARGUS_PROFILE_PATH could not be read") from exc
    elif inline_json:
        try:
            payload = json.loads(inline_json)
        except json.JSONDecodeError as exc:
            raise ValueError("ARGUS_PROFILE_JSON must contain a JSON object") from exc
    else:
        raise ValueError(
            "seed-real requires an explicit profile via --profile, "
            "ARGUS_PROFILE_PATH, or ARGUS_PROFILE_JSON"
        )

    if not isinstance(payload, Mapping):
        raise ValueError("seed-real profile must contain a JSON object")
    allowed = {field.name for field in fields(ProfileUpdate)}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError("seed-real profile contains unsupported fields")
    missing = [name for name in ("first_name", "last_name", "email") if not payload.get(name)]
    if missing:
        raise ValueError("seed-real profile requires first_name, last_name, and email")
    values: dict[str, Any] = dict(payload)
    locations = values.get("preferred_locations")
    if locations is not None:
        if not isinstance(locations, (list, tuple)) or not all(
            isinstance(item, str) for item in locations
        ):
            raise ValueError("preferred_locations must be a list of strings")
        values["preferred_locations"] = tuple(locations)
    return ProfileUpdate(**values)


def _seed_cv_root(args: argparse.Namespace) -> Path:
    configured = getattr(args, "cv_root", None)
    raw = str(configured) if configured is not None else os.environ.get("ARGUS_CV_ROOT", "")
    if not raw.strip():
        raise ValueError(
            "seed-real requires an explicit CV root via --cv-root or ARGUS_CV_ROOT"
        )
    return Path(raw).expanduser().resolve()


def _seed_real(args: argparse.Namespace) -> int:
    """Seed a supplied candidate profile and CV library; never embeds identity data."""

    try:
        profile_update = _load_seed_profile(args)
        cv_root = _seed_cv_root(args)
    except ValueError as exc:
        print(f"ARGUS seed-real refused: {exc}", file=sys.stderr)
        return 2
    settings, database, crypto = _runtime()
    summer_roots: tuple[Path, ...] = (
        cv_root / "Finance",
        cv_root / "_finance_summer_variants",
    )
    yii_trees: tuple[Path, ...] = (
        cv_root / "PlacementYearCVs",
        cv_root / "PlacementYearCVs2",
        cv_root / "PlacementYearCVs3",
    )
    candidate_slug = "_".join(
        part.strip().replace(" ", "_")
        for part in (profile_update.first_name or "", profile_update.last_name or "")
        if part and part.strip()
    )

    with database.session_scope() as session:
        profile_service = ProfileService(session, crypto)
        profile = profile_service.get_model()
        if not profile.first_name:
            profile_service.update(profile_update, actor="profile_seed")

        documents = DocumentService(session, settings.documents_dir)

        def store_tree(
            roots: tuple[Path, ...], variant: str, blanket_root: Path | None = None
        ) -> int:
            count = 0
            for root in roots:
                if not root.is_dir():
                    print("  configured CV tree missing; skipped")
                    continue
                for path in sorted(root.glob("**/*.docx")):
                    rel = path.relative_to(root)
                    parts = rel.parts  # (Firm, Division, file.docx)
                    firm_slug = (
                        parts[0].casefold().replace(" ", "-").replace(".", "")
                        if parts
                        else ""
                    )
                    division_slug = (
                        parts[1]
                        .casefold()
                        .replace(" ", "-")
                        .replace("&", "and")
                        .replace("/", "-")
                        if len(parts) > 1
                        else ""
                    )
                    tags = tuple(
                        t for t in (firm_slug, division_slug, f"{variant}-cv") if t
                    )
                    try:
                        documents.store_bytes(
                            filename=path.name,
                            content=path.read_bytes(),
                            kind="cv",
                            tags=tags,
                            approved=True,
                            actor=f"profile_seed:{variant}",
                        )
                        count += 1
                    except Exception:  # noqa: BLE001 - continue independent CV files
                        print("  CV file skipped")
            if blanket_root is not None and blanket_root.is_dir():
                for path in sorted(blanket_root.glob("*.docx")):
                    stem = path.stem
                    prefix = f"{candidate_slug}_Blanket_".casefold()
                    if prefix and stem.casefold().startswith(prefix):
                        stem = stem[len(prefix) :]
                    role_slug = stem.replace("_", " ").strip().casefold().replace(" ", "-")
                    tags = tuple(
                        t
                        for t in (
                            DocumentService.BLANKET_TAG,
                            role_slug,
                            f"{variant}-cv",
                        )
                        if t
                    )
                    try:
                        documents.store_bytes(
                            filename=path.name,
                            content=path.read_bytes(),
                            kind="cv",
                            tags=tags,
                            approved=True,
                            actor=f"profile_seed:{variant}:blanket",
                        )
                        count += 1
                    except Exception:  # noqa: BLE001 - continue independent CV files
                        print("  CV file skipped")
            return count

        summer_count = store_tree(
            summer_roots, "summer", cv_root / "_blanket_role_cvs_summer"
        )
        yii_count = store_tree(yii_trees, "yii", cv_root / "_blanket_role_cvs")

    print(f"ARGUS profile seed complete: Yii CVs={yii_count}, Summer CVs={summer_count}.")
    return 0


def _command_audit(args: argparse.Namespace) -> int:
    settings = Settings.load()
    database_path = settings.data_dir / "argus.db"
    if not database_path.is_file():
        print(f"ARGUS audit refused: database does not exist: {database_path}", file=sys.stderr)
        return 2

    def open_read_only():  # noqa: ANN202 - SQLAlchemy DB-API creator
        return sqlite3.connect(
            f"file:{database_path}?mode=ro",
            uri=True,
            check_same_thread=False,
            timeout=5,
        )

    engine = create_engine("sqlite://", creator=open_read_only, future=True)
    try:
        with Session(engine) as session:
            all_epochs = bool(getattr(args, "all_epochs", False))
            requested_epoch = getattr(args, "epoch", None)
            if requested_epoch is not None and requested_epoch < 0:
                print("ARGUS audit refused: --epoch must be zero or greater", file=sys.stderr)
                return 2
            selected_epoch: int | None = None
            if not all_epochs:
                if requested_epoch is not None:
                    selected_epoch = int(requested_epoch)
                else:
                    state = session.get(AuditChainState, 1)
                    if state is not None:
                        selected_epoch = int(state.epoch)
                    else:
                        selected_epoch = int(
                            session.scalar(select(func.max(AuditEvent.epoch))) or 0
                        )
                event_count = int(
                    session.scalar(
                        select(func.count())
                        .select_from(AuditEvent)
                        .where(AuditEvent.epoch == selected_epoch)
                    )
                    or 0
                )
                if event_count == 0:
                    print(
                        f"ARGUS audit refused: epoch {selected_epoch} has no events",
                        file=sys.stderr,
                    )
                    return 2
            verification = verify_audit_chain(session, epoch=selected_epoch)
            history = None
            if not all_epochs and requested_epoch is None:
                # Default path answers "is the current epoch valid" — but a
                # forked historical epoch would otherwise stay invisible
                # unless the operator passes --all-epochs. Report the
                # whole-history result alongside, without changing the
                # current-epoch verdict or exit code.
                try:
                    history = verify_audit_chain(session, epoch=None)
                except Exception:  # noqa: BLE001 - history advisory, never fatal
                    history = None
    finally:
        engine.dispose()
    if verification.valid:
        if all_epochs:
            print(
                f"AUDIT ALL EPOCHS VALID — {verification.checked_events} events verified"
            )
        elif requested_epoch is not None:
            print(
                "AUDIT EPOCH VALID — "
                f"epoch {selected_epoch}; {verification.checked_events} events verified"
            )
        else:
            print(
                "AUDIT CURRENT EPOCH VALID — "
                f"epoch {selected_epoch}; {verification.checked_events} events verified"
            )
            if history is None:
                print("HISTORICAL EPOCHS UNKNOWN — whole-history check did not complete")
            elif history.valid:
                print(
                    "HISTORICAL EPOCHS VALID — "
                    f"{history.checked_events} events verified (use --all-epochs for detail)"
                )
            else:
                print(
                    "HISTORICAL EPOCHS BROKEN — "
                    f"event {history.broken_event_id}; "
                    f"{history.checked_events} prior events verified "
                    "(use --all-epochs for detail)"
                )
        return 0
    if not all_epochs:
        print(
            "AUDIT EPOCH BROKEN — "
            f"epoch {selected_epoch}; event {verification.broken_event_id}; "
            f"{verification.checked_events} prior events verified"
        )
        return 2
    print(
        "AUDIT BROKEN — "
        f"event {verification.broken_event_id}; {verification.checked_events} prior events verified"
    )
    return 2


def _command_import_csv(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser().resolve()
    if not path.is_file():
        print(f"CSV not found: {path}", file=sys.stderr)
        return 1
    _, database, _ = _runtime()
    with database.session_scope() as session:
        report = OpportunityService(session).import_csv(path.read_bytes(), args.source)
    print(
        f"Imported {report.imported}; skipped {report.skipped_duplicates} duplicates; "
        f"{len(report.errors)} errors."
    )
    for error in report.errors:
        print(f"  row {error.row_number}: {error.message}")
    return 2 if report.errors else 0


def _print_ats_histogram(title: str, histogram: Counter[str]) -> None:
    print(f"{title} ats_type histogram:")
    if not histogram:
        print("  (empty)")
        return
    for ats_type, count in sorted(histogram.items()):
        print(f"  {ats_type}: {count}")


def _plan_ats_reclassification(
    opportunities: Sequence[Opportunity],
) -> tuple[Counter[str], Counter[str], list[tuple[Opportunity, str, str]]]:
    before = Counter(str(item.ats_type or "unknown") for item in opportunities)
    after = before.copy()
    changes: list[tuple[Opportunity, str, str]] = []
    for opportunity in opportunities:
        old_value = str(opportunity.ats_type or "unknown")
        new_value = _ats_from_url(opportunity.url)
        if new_value == "unknown" or new_value == old_value:
            continue
        changes.append((opportunity, old_value, new_value))
        after[old_value] -= 1
        if after[old_value] == 0:
            del after[old_value]
        after[new_value] += 1
    return before, after, changes


def _command_reclassify_ats(args: argparse.Namespace) -> int:
    settings = Settings.load()
    database_path = settings.data_dir / "argus.db"
    if not database_path.is_file():
        print(
            f"ARGUS reclassify-ats refused: database does not exist: {database_path}",
            file=sys.stderr,
        )
        return 2

    dry_run = bool(args.dry_run)
    if dry_run:

        def open_read_only():  # noqa: ANN202 - SQLAlchemy DB-API creator
            return sqlite3.connect(
                f"file:{database_path}?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=5,
            )

        engine = create_engine("sqlite://", creator=open_read_only, future=True)
        try:
            with Session(engine) as session:
                opportunities = list(
                    session.scalars(select(Opportunity).order_by(Opportunity.id)).all()
                )
                before, after, changes = _plan_ats_reclassification(opportunities)
        finally:
            engine.dispose()
    else:
        database = Database(settings)
        with database.session_scope() as session:
            opportunities = list(
                session.scalars(select(Opportunity).order_by(Opportunity.id)).all()
            )
            before, after, changes = _plan_ats_reclassification(opportunities)
            for opportunity, old_value, new_value in changes:
                opportunity.ats_type = new_value
                append_audit(
                    session,
                    AuditInput(
                        "maintenance",
                        "opportunity.ats_reclassified",
                        "opportunity",
                        opportunity.id,
                        {"old_ats_type": old_value, "new_ats_type": new_value},
                    ),
                )

    print("ATS URL reclassification")
    print(f"Database: {database_path}")
    print(
        "Mode: DRY RUN (no database changes written)"
        if dry_run
        else "Mode: APPLIED"
    )
    _print_ats_histogram("Before", before)
    _print_ats_histogram("After", after)
    print(f"Rows changed: {len(changes)}")
    return 0


def _print_programme_histogram(title: str, histogram: Counter[str]) -> None:
    print(f"{title} programme_group histogram:")
    if not histogram:
        print("  (empty)")
        return
    for programme_group, count in sorted(histogram.items()):
        print(f"  {programme_group}: {count}")


def _exact_trackr_tracker_slug(url: str) -> str | None:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc.casefold() != "app.the-trackr.com":
        return None
    path_parts = parsed.path.split("/")
    if len(path_parts) == 4 and path_parts[-1] == "":
        path_parts.pop()
    if len(path_parts) != 3 or path_parts[:2] != ["", "uk-finance"]:
        return None
    slug = path_parts[2]
    return slug if slug in SLUG_TO_TYPE else None


def _programme_from_retained_evidence(
    opportunity: Opportunity,
) -> tuple[str | None, bool]:
    source = str(opportunity.source or "")
    if source.startswith("trackr_live:"):
        slug = source.removeprefix("trackr_live:")
        if slug in SLUG_TO_TYPE:
            return ProgrammeType(SLUG_TO_TYPE[slug]).value, False

    tracker_slug = _exact_trackr_tracker_slug(opportunity.url)
    if tracker_slug is not None:
        return ProgrammeType(SLUG_TO_TYPE[tracker_slug]).value, False
    if source == "trackr_live":
        return None, True
    return classify_programme(opportunity.role_title, opportunity.employer).value, False


def _plan_programme_reclassification(
    opportunities: Sequence[Opportunity],
) -> tuple[
    Counter[str],
    Counter[str],
    list[tuple[Opportunity, str, str]],
    int,
]:
    before = Counter(str(item.programme_group or "(empty)") for item in opportunities)
    after = before.copy()
    changes: list[tuple[Opportunity, str, str]] = []
    unrecoverable = 0
    for opportunity in opportunities:
        old_value = str(opportunity.programme_group or "")
        new_value, provenance_unrecoverable = _programme_from_retained_evidence(
            opportunity
        )
        if provenance_unrecoverable:
            unrecoverable += 1
            continue
        if new_value is None or new_value == old_value:
            continue
        changes.append((opportunity, old_value, new_value))
        old_histogram_value = old_value or "(empty)"
        after[old_histogram_value] -= 1
        if after[old_histogram_value] == 0:
            del after[old_histogram_value]
        after[new_value] += 1
    return before, after, changes, unrecoverable


def _create_verified_programme_backup(
    database_path: Path,
    backup_directory: Path,
) -> Path:
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = backup_directory / (
        f"{database_path.name}.pre-programme-reclassification-{uuid4().hex}.bak"
    )
    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(descriptor)
        source_uri = database_path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            with closing(sqlite3.connect(backup_path)) as destination:
                source.backup(destination)
                destination.commit()
                integrity_rows = destination.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
                if not integrity_rows or any(
                    str(row[0]).casefold() != "ok" for row in integrity_rows
                ):
                    raise RuntimeError(
                        f"programme backup integrity check failed: {integrity_rows!r}"
                    )
                foreign_key_errors = destination.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if foreign_key_errors:
                    raise RuntimeError(
                        "programme backup foreign key check failed: "
                        f"{foreign_key_errors!r}"
                    )
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def _sqlite_file_fingerprint(database_path: Path) -> tuple[tuple[bool, int, str], ...]:
    fingerprints: list[tuple[bool, int, str]] = []
    for path in (database_path, Path(f"{database_path}-wal")):
        if not path.exists():
            fingerprints.append((False, 0, ""))
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        fingerprints.append((True, size, digest.hexdigest()))
    return tuple(fingerprints)


def _copy_stable_sqlite_snapshot(
    database_path: Path,
    snapshot_root: Path,
    *,
    attempts: int = 3,
) -> Path:
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        attempt_directory = snapshot_root / f"attempt-{attempt}"
        attempt_directory.mkdir()
        snapshot_path = attempt_directory / database_path.name
        try:
            before = _sqlite_file_fingerprint(database_path)
            if not before[0][0]:
                raise FileNotFoundError(database_path)
            shutil.copyfile(database_path, snapshot_path)
            source_wal_path = Path(f"{database_path}-wal")
            if before[1][0]:
                shutil.copyfile(
                    source_wal_path,
                    Path(f"{snapshot_path}-wal"),
                )
            after = _sqlite_file_fingerprint(database_path)
            snapshot = _sqlite_file_fingerprint(snapshot_path)
            if before == after == snapshot:
                return snapshot_path
        except (OSError, ValueError) as exc:
            last_error = exc
    message = (
        "dry-run refused: source database changed during stable snapshot capture"
    )
    if last_error is not None:
        raise RuntimeError(message) from last_error
    raise RuntimeError(message)


_NON_UK_ARCHIVE_REASON = "non_uk_location"
_OFF_CYCLE_ARCHIVE_REASON = "off_cycle_closed_window"
_OFF_CYCLE_PROGRAMME_REASON = "off_cycle_programme_tier"


@dataclass(frozen=True, slots=True)
class _OpportunityArchivePlanRow:
    opportunity_id: str
    employer: str
    role_title: str
    location: str
    reason: str


def _archive_counts_from_sqlite(connection: sqlite3.Connection) -> tuple[int, int, int, int]:
    opportunity_count = int(connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])
    application_count = int(connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0])
    archive_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_archives'"
    ).fetchone()
    if archive_table is None:
        return opportunity_count, application_count, 0, 0
    archive_count = int(
        connection.execute("SELECT COUNT(*) FROM opportunity_archives").fetchone()[0]
    )
    active_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM opportunity_archives WHERE archived_at IS NOT NULL"
        ).fetchone()[0]
    )
    return opportunity_count, application_count, archive_count, active_count


def _archive_plan_from_sqlite(
    connection: sqlite3.Connection,
    *,
    unarchive: bool,
) -> list[_OpportunityArchivePlanRow]:
    connection.row_factory = sqlite3.Row
    archive_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_archives'"
    ).fetchone()
    if unarchive:
        if archive_table is None:
            return []
        rows = connection.execute(
            "SELECT o.id, o.employer, o.role_title, o.location, a.archived_reason "
            "FROM opportunities AS o "
            "JOIN opportunity_archives AS a ON a.opportunity_id=o.id "
            "WHERE a.archived_at IS NOT NULL AND a.archived_reason=? "
            "ORDER BY o.id",
            (_NON_UK_ARCHIVE_REASON,),
        ).fetchall()
        return [
            _OpportunityArchivePlanRow(
                opportunity_id=str(row["id"]),
                employer=str(row["employer"] or ""),
                role_title=str(row["role_title"] or ""),
                location=str(row["location"] or ""),
                reason=str(row["archived_reason"] or ""),
            )
            for row in rows
        ]

    if archive_table is None:
        rows = connection.execute(
            "SELECT id, employer, role_title, location FROM opportunities ORDER BY id"
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT o.id, o.employer, o.role_title, o.location "
            "FROM opportunities AS o "
            "LEFT JOIN opportunity_archives AS a ON a.opportunity_id=o.id "
            "WHERE a.archived_at IS NULL ORDER BY o.id"
        ).fetchall()
    planned: list[_OpportunityArchivePlanRow] = []
    for row in rows:
        decision = classify_location_scope(str(row["location"] or ""))
        if not decision.should_archive:
            continue
        planned.append(
            _OpportunityArchivePlanRow(
                opportunity_id=str(row["id"]),
                employer=str(row["employer"] or ""),
                role_title=str(row["role_title"] or ""),
                location=str(row["location"] or ""),
                reason=decision.reason,
            )
        )
    return planned


def _off_cycle_plan_from_sqlite(
    connection: sqlite3.Connection,
    *,
    unarchive: bool,
    programme_off_cycle: bool = False,
) -> tuple[list[_OpportunityArchivePlanRow], int]:
    """Plan archiving (or unarchiving) of CLOSED-window or PROGRAMME off-cycle opportunities.

    Returns (plan, already_archived_skipped) where already_archived_skipped
    counts rows that matched the selector but were already archived — these
    are excluded from the plan so the count is not misleading.
    """
    connection.row_factory = sqlite3.Row
    if programme_off_cycle:
        reason = _OFF_CYCLE_PROGRAMME_REASON
    else:
        reason = _OFF_CYCLE_ARCHIVE_REASON

    if unarchive:
        rows = connection.execute(
            "SELECT o.id, o.employer, o.role_title, o.location, a.archived_reason "
            "FROM opportunities AS o "
            "JOIN opportunity_archives AS a ON a.opportunity_id=o.id "
            "WHERE a.archived_at IS NOT NULL AND a.archived_reason=? "
            "ORDER BY o.id",
            (reason,),
        ).fetchall()
        return [
            _OpportunityArchivePlanRow(
                opportunity_id=str(row["id"]),
                employer=str(row["employer"] or ""),
                role_title=str(row["role_title"] or ""),
                location=str(row["location"] or ""),
                reason=str(row["archived_reason"] or ""),
            )
            for row in rows
        ], 0

    if programme_off_cycle:
        where_clause = (
            "WHERE (a.archived_at IS NULL) "
            "AND (LOWER(o.role_title) LIKE '%off-cycle%' "
            "OR LOWER(o.role_title) LIKE '%off cycle%' "
            "OR LOWER(o.role_title) LIKE '%offcycle%')"
        )
    else:
        where_clause = (
            "WHERE o.application_window_status=? "
            "AND a.archived_at IS NULL"
        )

    # Count already-archived matches (skipped from plan)
    if programme_off_cycle:
        skipped_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM opportunities AS o "
                "JOIN opportunity_archives AS a ON a.opportunity_id=o.id "
                "WHERE a.archived_at IS NOT NULL "
                "AND (LOWER(o.role_title) LIKE '%off-cycle%' "
                "OR LOWER(o.role_title) LIKE '%off cycle%' "
                "OR LOWER(o.role_title) LIKE '%offcycle%')"
            ).fetchone()[0]
        )
    else:
        skipped_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM opportunities AS o "
                "LEFT JOIN opportunity_archives AS a ON a.opportunity_id=o.id "
                "WHERE o.application_window_status=? "
                "AND a.archived_at IS NOT NULL",
                (ApplicationWindowStatus.CLOSED.value,),
            ).fetchone()[0]
        )

    params = ()
    if not programme_off_cycle:
        params = (ApplicationWindowStatus.CLOSED.value,)

    rows = connection.execute(
        f"SELECT o.id, o.employer, o.role_title, o.location "
        f"FROM opportunities AS o "
        f"LEFT JOIN opportunity_archives AS a ON a.opportunity_id=o.id "
        f"{where_clause} "
        f"ORDER BY o.id",
        params,
    ).fetchall()
    plan = [
        _OpportunityArchivePlanRow(
            opportunity_id=str(row["id"]),
            employer=str(row["employer"] or ""),
            role_title=str(row["role_title"] or ""),
            location=str(row["location"] or ""),
            reason=reason,
        )
        for row in rows
    ]
    return plan, skipped_count


def _read_archive_snapshot(
    database_path: Path,
    *,
    unarchive: bool,
) -> tuple[list[_OpportunityArchivePlanRow], tuple[int, int, int, int]]:
    with TemporaryDirectory(prefix="argus-archive-dry-run-") as temporary:
        snapshot_path = _copy_stable_sqlite_snapshot(database_path, Path(temporary))
        with closing(
            sqlite3.connect(snapshot_path.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            plan = _archive_plan_from_sqlite(connection, unarchive=unarchive)
            counts = _archive_counts_from_sqlite(connection)
    return plan, counts


def _read_off_cycle_snapshot(
    database_path: Path,
    *,
    unarchive: bool,
    programme_off_cycle: bool = False,
) -> tuple[list[_OpportunityArchivePlanRow], tuple[int, int, int, int], int]:
    with TemporaryDirectory(prefix="argus-off-cycle-dry-run-") as temporary:
        snapshot_path = _copy_stable_sqlite_snapshot(database_path, Path(temporary))
        with closing(
            sqlite3.connect(snapshot_path.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            plan, skipped = _off_cycle_plan_from_sqlite(
                connection, unarchive=unarchive, programme_off_cycle=programme_off_cycle,
            )
            counts = _archive_counts_from_sqlite(connection)
    return plan, counts, skipped


def _archive_counts_from_session(session: Session) -> tuple[int, int, int, int]:
    return (
        int(session.scalar(select(func.count()).select_from(Opportunity)) or 0),
        int(session.scalar(select(func.count()).select_from(Application)) or 0),
        int(session.scalar(select(func.count()).select_from(OpportunityArchive)) or 0),
        int(
            session.scalar(
                select(func.count())
                .select_from(OpportunityArchive)
                .where(OpportunityArchive.archived_at.is_not(None))
            )
            or 0
        ),
    )


def _archive_plan_from_session(
    session: Session,
    *,
    unarchive: bool,
) -> list[_OpportunityArchivePlanRow]:
    if unarchive:
        rows = session.execute(
            select(Opportunity, OpportunityArchive)
            .join(
                OpportunityArchive,
                OpportunityArchive.opportunity_id == Opportunity.id,
            )
            .where(
                OpportunityArchive.archived_at.is_not(None),
                OpportunityArchive.archived_reason == _NON_UK_ARCHIVE_REASON,
            )
            .order_by(Opportunity.id)
        ).all()
        return [
            _OpportunityArchivePlanRow(
                opportunity_id=opportunity.id,
                employer=str(opportunity.employer or ""),
                role_title=str(opportunity.role_title or ""),
                location=str(opportunity.location or ""),
                reason=str(marker.archived_reason or ""),
            )
            for opportunity, marker in rows
        ]

    opportunities = session.scalars(
        select(Opportunity)
        .outerjoin(
            OpportunityArchive,
            OpportunityArchive.opportunity_id == Opportunity.id,
        )
        .where(OpportunityArchive.archived_at.is_(None))
        .order_by(Opportunity.id)
    ).all()
    planned: list[_OpportunityArchivePlanRow] = []
    for opportunity in opportunities:
        decision = classify_location_scope(str(opportunity.location or ""))
        if not decision.should_archive:
            continue
        planned.append(
            _OpportunityArchivePlanRow(
                opportunity_id=opportunity.id,
                employer=str(opportunity.employer or ""),
                role_title=str(opportunity.role_title or ""),
                location=str(opportunity.location or ""),
                reason=decision.reason,
            )
        )
    return planned


def _off_cycle_plan_from_session(
    session: Session,
    *,
    unarchive: bool,
    programme_off_cycle: bool = False,
) -> tuple[list[_OpportunityArchivePlanRow], int]:
    """Plan archiving (or unarchiving) of CLOSED-window or PROGRAMME off-cycle opportunities via ORM.

    Returns (plan, already_archived_skipped).
    """
    if programme_off_cycle:
        reason = _OFF_CYCLE_PROGRAMME_REASON
    else:
        reason = _OFF_CYCLE_ARCHIVE_REASON

    if unarchive:
        rows = session.execute(
            select(Opportunity, OpportunityArchive)
            .join(
                OpportunityArchive,
                OpportunityArchive.opportunity_id == Opportunity.id,
            )
            .where(
                OpportunityArchive.archived_at.is_not(None),
                OpportunityArchive.archived_reason == reason,
            )
            .order_by(Opportunity.id)
        ).all()
        return [
            _OpportunityArchivePlanRow(
                opportunity_id=opportunity.id,
                employer=str(opportunity.employer or ""),
                role_title=str(opportunity.role_title or ""),
                location=str(opportunity.location or ""),
                reason=str(marker.archived_reason or ""),
            )
            for opportunity, marker in rows
        ], 0

    if programme_off_cycle:
        # Select unarchived opportunities whose role title contains off-cycle variants
        import sqlalchemy as sa

        pattern = sa.or_(
            Opportunity.role_title.ilike("%off-cycle%"),
            Opportunity.role_title.ilike("%off cycle%"),
            Opportunity.role_title.ilike("%offcycle%"),
        )
        opportunities = session.scalars(
            select(Opportunity)
            .outerjoin(
                OpportunityArchive,
                OpportunityArchive.opportunity_id == Opportunity.id,
            )
            .where(
                OpportunityArchive.archived_at.is_(None),
                pattern,
            )
            .order_by(Opportunity.id)
        ).all()
    else:
        opportunities = session.scalars(
            select(Opportunity)
            .where(
                Opportunity.application_window_status == ApplicationWindowStatus.CLOSED.value,
            )
            .order_by(Opportunity.id)
        ).all()

    # Count already-archived matches that were skipped
    if programme_off_cycle:
        already_archived = session.scalar(
            select(func.count())
            .select_from(Opportunity)
            .join(
                OpportunityArchive,
                OpportunityArchive.opportunity_id == Opportunity.id,
            )
            .where(
                OpportunityArchive.archived_at.is_not(None),
                pattern,
            )
        ) or 0
    else:
        already_archived = session.scalar(
            select(func.count())
            .select_from(Opportunity)
            .outerjoin(
                OpportunityArchive,
                OpportunityArchive.opportunity_id == Opportunity.id,
            )
            .where(
                Opportunity.application_window_status == ApplicationWindowStatus.CLOSED.value,
                OpportunityArchive.archived_at.is_not(None),
            )
        ) or 0

    plan = [
        _OpportunityArchivePlanRow(
            opportunity_id=opportunity.id,
            employer=str(opportunity.employer or ""),
            role_title=str(opportunity.role_title or ""),
            location=str(opportunity.location or ""),
            reason=reason,
        )
        for opportunity in opportunities
    ]
    return plan, already_archived


def _create_verified_archive_backup(
    database_path: Path,
    backup_directory: Path,
) -> Path:
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = backup_directory / (
        f"{database_path.name}.pre-opportunity-archive-{uuid4().hex}.bak"
    )
    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(descriptor)
        with closing(
            sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
        ) as source:
            with closing(sqlite3.connect(backup_path)) as destination:
                source.backup(destination)
                destination.commit()
                integrity_rows = destination.execute("PRAGMA integrity_check").fetchall()
                if not integrity_rows or any(
                    str(row[0]).casefold() != "ok" for row in integrity_rows
                ):
                    raise RuntimeError(
                        f"archive backup integrity check failed: {integrity_rows!r}"
                    )
                foreign_key_errors = destination.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if foreign_key_errors:
                    raise RuntimeError(
                        "archive backup foreign key check failed: "
                        f"{foreign_key_errors!r}"
                    )
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def _print_archive_plan(
    database_path: Path,
    *,
    unarchive: bool,
    apply_changes: bool,
    plan: list[_OpportunityArchivePlanRow],
) -> None:
    print("Opportunity archive plan")
    print(f"Database: {database_path}")
    print("Action: UNARCHIVE NON-UK" if unarchive else "Action: ARCHIVE NON-UK")
    print("Mode: APPLIED" if apply_changes else "Mode: DRY RUN (no database changes written)")
    for row in plan:
        print(
            f"  {row.opportunity_id} | {row.employer} | {row.role_title} | "
            f"{row.location} | reason={row.reason}"
        )
    print(f"Rows selected: {len(plan)}")


def _print_off_cycle_plan(
    database_path: Path,
    *,
    unarchive: bool,
    apply_changes: bool,
    plan: list[_OpportunityArchivePlanRow],
    programme_off_cycle: bool = False,
    already_archived: int = 0,
) -> None:
    if programme_off_cycle:
        label = "PROGRAMME OFF-CYCLE"
        archive_action = "ARCHIVE OFF-CYCLE PROGRAMME"
        unarchive_action = "UNARCHIVE OFF-CYCLE PROGRAMME"
        empty_msg = "  (no off-cycle programme rows selected)"
    else:
        label = "CLOSED WINDOW"
        archive_action = "ARCHIVE CLOSED WINDOW"
        unarchive_action = "UNARCHIVE CLOSED WINDOW"
        empty_msg = "  (no CLOSED rows selected)"
    print(f"Off-cycle archive plan ({label})")
    print(f"Database: {database_path}")
    print(f"Action: {unarchive_action}" if unarchive else f"Action: {archive_action}")
    print("Mode: APPLIED" if apply_changes else "Mode: DRY RUN (no database changes written)")
    if not plan:
        print(empty_msg)
    for row in plan:
        print(
            f"  {row.opportunity_id} | {row.employer} | {row.role_title} | "
            f"{row.location} | reason={row.reason}"
        )
    print(f"Rows pending: {len(plan)}")
    if already_archived > 0:
        print(f"Rows already archived (skipped): {already_archived}")


def _print_archive_counts(
    before: tuple[int, int, int, int],
    after: tuple[int, int, int, int],
) -> None:
    print(f"Opportunity rows: {before[0]} -> {after[0]}")
    print(f"Application rows: {before[1]} -> {after[1]}")
    print(f"Archive metadata rows: {before[2]} -> {after[2]}")
    print(f"Active archives: {before[3]} -> {after[3]}")


def _command_archive_opportunities(args: argparse.Namespace) -> int:
    settings = Settings.load()
    database_path = settings.data_dir / "argus.db"
    if not database_path.is_file():
        print(
            "ARGUS archive-opportunities refused: database does not exist: "
            f"{database_path}",
            file=sys.stderr,
        )
        return 2

    unarchive = bool(args.unarchive)
    apply_changes = bool(args.apply)
    planned, snapshot_counts = _read_archive_snapshot(
        database_path,
        unarchive=unarchive,
    )
    _print_archive_plan(
        database_path,
        unarchive=unarchive,
        apply_changes=apply_changes,
        plan=planned,
    )
    if not apply_changes or not planned:
        _print_archive_counts(snapshot_counts, snapshot_counts)
        return 0

    # The exact plan is already visible. This is the first live-write step.
    backup_path = _create_verified_archive_backup(
        database_path,
        settings.data_dir / "backups",
    )
    print(f"Backup: {backup_path}", flush=True)

    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        # Pysqlite otherwise defers the physical write transaction until the
        # first DML statement. Hold a reserved lock from live validation
        # through marker and audit mutation so a location cannot change in the
        # validation-to-write interval.
        session.execute(text("BEGIN IMMEDIATE"))
        live_plan = _archive_plan_from_session(session, unarchive=unarchive)
        before = _archive_counts_from_session(session)
        if live_plan != planned or before != snapshot_counts:
            raise RuntimeError(
                "archive apply refused: live database changed after the printed plan"
            )
        archived_at = datetime.now(timezone.utc)
        for row in live_plan:
            opportunity = session.get(Opportunity, row.opportunity_id)
            if opportunity is None:
                raise RuntimeError(
                    f"archive apply refused: opportunity disappeared: {row.opportunity_id}"
                )
            marker = session.get(OpportunityArchive, row.opportunity_id)
            if unarchive:
                if marker is None or marker.archived_at is None:
                    raise RuntimeError(
                        "archive apply refused: active marker disappeared: "
                        f"{row.opportunity_id}"
                    )
                marker.archived_at = None
                marker.archived_reason = None
                event_type = "opportunity.unarchived"
            else:
                if marker is None:
                    marker = OpportunityArchive(opportunity_id=row.opportunity_id)
                    session.add(marker)
                marker.archived_at = archived_at
                marker.archived_reason = row.reason
                event_type = "opportunity.archived"
            append_audit(
                session,
                AuditInput(
                    "maintenance",
                    event_type,
                    "opportunity",
                    row.opportunity_id,
                    {
                        "location": row.location,
                        "reason": row.reason,
                    },
                ),
            )
        session.flush()
        after = _archive_counts_from_session(session)
        if after[0] != before[0] or after[1] != before[1]:
            raise RuntimeError(
                "archive apply rolled back: opportunity/application row count changed"
            )
        if after[2] < before[2]:
            raise RuntimeError("archive apply rolled back: archive metadata row was deleted")

    _print_archive_counts(before, after)
    return 0


def _command_archive_off_cycle(args: argparse.Namespace) -> int:
    # Fix Windows default-console encoding crash on zero-width spaces etc.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (ValueError, AttributeError):
        pass

    settings = Settings.load()
    database_path = settings.data_dir / "argus.db"
    if not database_path.is_file():
        print(
            "ARGUS archive-off-cycle refused: database does not exist: "
            f"{database_path}",
            file=sys.stderr,
        )
        return 2

    programme_off_cycle = bool(getattr(args, "programme_off_cycle", False))
    # Default to --closed when neither selector is specified (backward compat for bare --unarchive)
    if not programme_off_cycle and not args.closed_flag:
        args.closed_flag = True
    unarchive = bool(args.unarchive)
    apply_changes = bool(args.apply)
    planned, snapshot_counts, already_archived = _read_off_cycle_snapshot(
        database_path,
        unarchive=unarchive,
        programme_off_cycle=programme_off_cycle,
    )
    _print_off_cycle_plan(
        database_path,
        unarchive=unarchive,
        apply_changes=apply_changes,
        plan=planned,
        programme_off_cycle=programme_off_cycle,
        already_archived=already_archived,
    )
    if not apply_changes or not planned:
        _print_archive_counts(snapshot_counts, snapshot_counts)
        return 0

    # The exact plan is already visible. This is the first live-write step.
    backup_path = _create_verified_archive_backup(
        database_path,
        settings.data_dir / "backups",
    )
    print(f"Backup: {backup_path}", flush=True)

    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        live_plan, live_skipped = _off_cycle_plan_from_session(
            session, unarchive=unarchive, programme_off_cycle=programme_off_cycle,
        )
        before = _archive_counts_from_session(session)
        if live_plan != planned or before != snapshot_counts:
            raise RuntimeError(
                "archive-off-cycle apply refused: live database changed after the printed plan"
            )
        archived_at = datetime.now(timezone.utc)
        for row in live_plan:
            opportunity = session.get(Opportunity, row.opportunity_id)
            if opportunity is None:
                raise RuntimeError(
                    f"archive-off-cycle apply refused: opportunity disappeared: {row.opportunity_id}"
                )
            marker = session.get(OpportunityArchive, row.opportunity_id)
            if unarchive:
                if marker is None or marker.archived_at is None:
                    raise RuntimeError(
                        "archive-off-cycle apply refused: active marker disappeared: "
                        f"{row.opportunity_id}"
                    )
                marker.archived_at = None
                marker.archived_reason = None
                event_type = "opportunity.unarchived"
            else:
                if marker is None:
                    marker = OpportunityArchive(opportunity_id=row.opportunity_id)
                    session.add(marker)
                marker.archived_at = archived_at
                marker.archived_reason = row.reason
                event_type = "opportunity.archived"
            append_audit(
                session,
                AuditInput(
                    "maintenance",
                    event_type,
                    "opportunity",
                    row.opportunity_id,
                    {
                        "location": row.location,
                        "reason": row.reason,
                    },
                ),
            )
        session.flush()
        after = _archive_counts_from_session(session)
        if after[0] != before[0] or after[1] != before[1]:
            raise RuntimeError(
                "archive-off-cycle apply rolled back: opportunity/application row count changed"
            )
        if after[2] < before[2]:
            raise RuntimeError(
                "archive-off-cycle apply rolled back: archive metadata row was deleted"
            )

    _print_archive_counts(before, after)
    return 0


def _print_cv_tag_matrix(title: str, matrix: Counter[tuple[str, str]]) -> None:
    print(f"{title} content/tag table:")
    print("  variant       grad-2029  grad-2028  unparseable  total")
    for variant in (*CV_VARIANT_TAGS, "(none)"):
        counts = [matrix[(variant, column)] for column in MATRIX_CONTENT_COLUMNS]
        print(
            f"  {variant:<13} {counts[0]:>9}  {counts[1]:>9}  "
            f"{counts[2]:>11}  {sum(counts):>5}"
        )


def _command_repair_cv_tags(args: argparse.Namespace) -> int:
    settings = Settings.load()
    database_path = settings.data_dir / "argus.db"
    if not database_path.is_file():
        print(
            f"ARGUS repair-cv-tags refused: database does not exist: {database_path}",
            file=sys.stderr,
        )
        return 2

    apply_changes = bool(args.apply)
    backup_path: Path | None = None
    changed = 0
    if not apply_changes:
        with TemporaryDirectory(prefix="argus-cv-tags-dry-run-") as temporary:
            snapshot_path = _copy_stable_sqlite_snapshot(
                database_path,
                Path(temporary),
            )

            def open_read_only():  # noqa: ANN202 - SQLAlchemy DB-API creator
                return sqlite3.connect(
                    snapshot_path.as_uri() + "?mode=ro",
                    uri=True,
                    check_same_thread=False,
                    timeout=5,
                )

            engine = create_engine("sqlite://", creator=open_read_only, future=True)
            try:
                with Session(engine) as session:
                    plan = plan_cv_tag_repairs(session)
                    affected = contradictory_selected_application_ids(session)
            finally:
                engine.dispose()
        invariant_violations = planned_invariant_violations(plan)
        contradictions = 0 if invariant_violations == 0 else len(affected)
        reselection = CvReselectionReport(
            affected=len(affected),
            corrected=0,
            missing=0,
            state_fail_closed=0,
        )
    else:
        # This is intentionally the first operation that can write anywhere.
        # It writes only the new backup; the live DB is not opened read/write
        # until the verified backup has completed.
        backup_path = create_verified_cv_backup(
            database_path,
            settings.data_dir / "backups",
        )
        database = Database(settings)
        with database.session_scope() as session:
            plan = plan_cv_tag_repairs(session)
            invariant_violations = planned_invariant_violations(plan)
            if invariant_violations:
                raise RuntimeError(
                    "CV tag repair refused: projected invariant violations="
                    f"{invariant_violations}"
                )
            affected = contradictory_selected_application_ids(session)
            changed = apply_cv_tag_repairs(session, plan)
            invariant_violations = single_query_invariant_violations(session)
            if invariant_violations:
                raise RuntimeError(
                    "CV tag repair rolled back: single-query invariant violations="
                    f"{invariant_violations}"
                )
            reselection = reselect_applications(
                session,
                settings.documents_dir,
                affected,
            )
            contradictions = len(contradictory_selected_application_ids(session))
            if contradictions:
                raise RuntimeError(
                    "CV tag repair rolled back: contradictory selected CVs="
                    f"{contradictions}"
                )

    print("CV/graduation-year integrity repair")
    print(f"Database: {database_path}")
    print("Mode: APPLIED" if apply_changes else "Mode: DRY RUN (no database changes written)")
    if backup_path is not None:
        print(f"Backup: {backup_path}")
    _print_cv_tag_matrix("Before", plan.before)
    _print_cv_tag_matrix("After", plan.after)
    print(f"Rows scanned: {len(plan.observations)}")
    print(f"Rows changed: {changed if apply_changes else len(plan.changes)}")
    print(f"Unparseable CVs: {plan.unparseable}")
    print(f"Affected selected CVs: {reselection.affected}")
    print(f"Applications reselected: {reselection.corrected}")
    print(f"Applications missing compatible CV: {reselection.missing}")
    print(f"Applications moved to NEEDS_USER: {reselection.state_fail_closed}")
    print(f"Invariant violations: {invariant_violations}")
    print(f"Contradictory selected CVs: {contradictions}")
    return 1 if invariant_violations or contradictions else 0


def _command_reclassify_programmes(args: argparse.Namespace) -> int:
    settings = Settings.load()
    database_path = settings.data_dir / "argus.db"
    if not database_path.is_file():
        print(
            "ARGUS reclassify-programmes refused: database does not exist: "
            f"{database_path}",
            file=sys.stderr,
        )
        return 2

    dry_run = bool(args.dry_run)
    if dry_run:
        with TemporaryDirectory(prefix="argus-programme-dry-run-") as temporary:
            snapshot_path = _copy_stable_sqlite_snapshot(
                database_path,
                Path(temporary),
            )

            def open_read_only():  # noqa: ANN202 - SQLAlchemy DB-API creator
                return sqlite3.connect(
                    snapshot_path.as_uri() + "?mode=ro",
                    uri=True,
                    check_same_thread=False,
                    timeout=5,
                )

            engine = create_engine("sqlite://", creator=open_read_only, future=True)
            try:
                with Session(engine) as session:
                    opportunities = list(
                        session.scalars(
                            select(Opportunity).order_by(Opportunity.id)
                        ).all()
                    )
                    before, after, changes, unrecoverable = (
                        _plan_programme_reclassification(opportunities)
                    )
            finally:
                engine.dispose()
    else:
        backup_path = _create_verified_programme_backup(
            database_path,
            settings.data_dir / "backups",
        )
        print(f"Backup: {backup_path}", flush=True)
        database = Database(settings)
        with database.session_scope() as session:
            opportunities = list(
                session.scalars(select(Opportunity).order_by(Opportunity.id)).all()
            )
            before, after, changes, unrecoverable = (
                _plan_programme_reclassification(opportunities)
            )
            for opportunity, old_value, new_value in changes:
                opportunity.programme_group = new_value
                append_audit(
                    session,
                    AuditInput(
                        "maintenance",
                        "opportunity.programme_reclassified",
                        "opportunity",
                        opportunity.id,
                        {
                            "old_programme_group": old_value,
                            "new_programme_group": new_value,
                        },
                    ),
                )

    print("Programme group reclassification")
    print(f"Database: {database_path}")
    print(
        "Mode: DRY RUN (no database changes written)"
        if dry_run
        else "Mode: APPLIED"
    )
    _print_programme_histogram("Before", before)
    _print_programme_histogram("After", after)
    print(f"Rows changed: {len(changes)}")
    print(f"Authoritative provenance unrecoverable: {unrecoverable}")
    return 0


def _command_resolve_targets(args: argparse.Namespace) -> int:
    settings = Settings.load()
    database_path = settings.data_dir / "argus.db"
    if not database_path.is_file():
        print(
            f"ARGUS resolve-targets refused: database does not exist: {database_path}",
            file=sys.stderr,
        )
        return 2

    options = BatchResolveOptions(
        limit=args.limit,
        employer=args.employer,
        ats=args.ats,
        programme=getattr(args, "programme", "all"),
        only_unattempted=bool(args.only_unattempted),
        retry_failed=bool(args.retry_failed),
        dry_run=bool(args.dry_run),
        concurrency=args.concurrency,
        delay_seconds=args.delay_seconds,
        row_timeout_seconds=float(getattr(args, "row_timeout_seconds", 45.0)),
    )
    read_only_engine = None
    if options.dry_run:

        def open_read_only():  # noqa: ANN202 - SQLAlchemy DB-API creator
            return sqlite3.connect(
                f"file:{database_path}?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=5,
            )

        read_only_engine = create_engine(
            "sqlite://",
            creator=open_read_only,
            future=True,
        )

        class _ReadOnlyDatabase:
            @contextmanager
            def session_scope(self):  # noqa: ANN202 - driver-compatible scope
                with Session(read_only_engine) as session:
                    try:
                        yield session
                    finally:
                        session.rollback()

        database = _ReadOnlyDatabase()
    else:
        settings.ensure_directories()
        database = Database(settings)
        database.create_schema()

    def progress(row) -> None:  # noqa: ANN001 - stable CLI projection
        action = "would resolve" if options.dry_run else "resolved"
        reasons = ",".join(row.reason_codes) if row.reason_codes else "none"
        backfill_note = (
            f" | --backfill-employer: {row.established_employer}"
            if bool(getattr(row, "employer_backfilled", False))
            and str(getattr(row, "established_employer", "")).strip()
            else ""
        )
        print(
            f"[{row.index}] {row.opportunity_id} | {row.employer} | "
            f"{row.role_title} | {action} -> {row.target_status} | reasons={reasons}"
            f"{backfill_note}",
            flush=True,
        )

    try:
        report = BatchTargetResolutionDriver(
            database,  # type: ignore[arg-type]
            settings,
        ).run(options, progress=progress)
    finally:
        if read_only_engine is not None:
            read_only_engine.dispose()

    print("Target status histogram:")
    if report.target_histogram:
        for status, count in sorted(report.target_histogram.items()):
            print(f"  {status}: {count}")
    else:
        print("  (empty)")
    print("Failure reasons:")
    if report.failure_reasons:
        for reason, count in sorted(report.failure_reasons.items()):
            print(f"  {reason}: {count}")
    else:
        print("  (none)")
    if report.fatal_safety_violation:
        print(
            "FATAL SAFETY VIOLATION: "
            f"{report.fatal_safety_violation}"
        )
    print(
        f"Selected: {report.selected}; attempted: {report.attempted}; "
        f"interrupted: {'yes' if report.interrupted else 'no'}"
    )
    return 130 if report.interrupted else 0


def _command_apply(args: argparse.Namespace) -> int:
    mode = RunMode(args.mode)
    if mode is RunMode.SUBMIT and args.confirm_submit != args.application_id:
        print(
            "ARGUS refused the run: Retype the exact application ID with "
            "--confirm-submit immediately before submit",
            file=sys.stderr,
        )
        return 2
    settings, database, crypto = _runtime()
    with database.session_scope() as session:
        application = session.get(Application, args.application_id)
        if application is None:
            print(
                f"ARGUS refused the run: Application not found: {args.application_id}",
                file=sys.stderr,
            )
            return 2
        status_reason = user_status_exclusion_reason(
            application.opportunity.user_status
        )
        if status_reason is not None:
            print(f"ARGUS refused the run: {status_reason}", file=sys.stderr)
            return 2
    runner = AutomationRunner(database, settings, crypto)
    try:
        outcome = runner.run(
            args.application_id,
            mode,
            headed=args.headed,
        )
    except (KeyError, SubmissionBlocked, ValueError) as exc:
        print(f"ARGUS refused the run: {exc}", file=sys.stderr)
        return 2
    payload = asdict(outcome)
    print(json.dumps(payload, indent=2, default=str))
    return 0 if outcome.risk_level == 0 else 2


def _command_token(_: argparse.Namespace) -> int:
    settings, _, _ = _runtime()
    print(settings.api_token)
    return 0


def _command_serve(args: argparse.Namespace) -> int:
    settings, _, _ = _runtime()
    try:
        from app.version import __version__
    except ImportError:  # pragma: no cover - frozen builds always ship it
        __version__ = ""
    lock = RuntimeLock(
        runtime_lock_path(settings.data_dir),
        host=settings.host,
        port=settings.port,
        version=__version__,
    )
    try:
        lock.acquire()
    except RuntimeAlreadyRunning as exc:
        print(f"ARGUS refused to start: {exc}", file=sys.stderr)
        return 2
    try:
        if args.open:
            webbrowser.open(f"http://{settings.host}:{settings.port}")
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            reload=False,
            access_log=not args.quiet,
        )
    finally:
        lock.release()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="argus",
        description="Local-first internship application operating system",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create the private local runtime")
    init_parser.set_defaults(handler=_command_init)

    seed_parser = subparsers.add_parser("seed", help="Create idempotent synthetic ATS demo data")
    seed_parser.set_defaults(handler=_command_seed)

    real_parser = subparsers.add_parser(
        "seed-real", help="Seed an explicitly supplied candidate profile and CV library"
    )
    real_parser.add_argument(
        "--profile",
        type=Path,
        help="JSON profile file (or set ARGUS_PROFILE_PATH / ARGUS_PROFILE_JSON)",
    )
    real_parser.add_argument(
        "--cv-root",
        type=Path,
        help="CV library root (or set ARGUS_CV_ROOT)",
    )
    real_parser.set_defaults(handler=_seed_real)

    audit_parser = subparsers.add_parser("audit", help="Verify the hash-linked audit chain")
    audit_scope = audit_parser.add_mutually_exclusive_group()
    audit_scope.add_argument(
        "--epoch",
        type=int,
        help="Verify one explicit audit epoch (defaults to the current epoch)",
    )
    audit_scope.add_argument(
        "--all-epochs",
        action="store_true",
        help="Verify retained historical epochs as well as the current epoch",
    )
    audit_parser.set_defaults(handler=_command_audit)

    token_parser = subparsers.add_parser("token", help="Print the local capture token")
    token_parser.set_defaults(handler=_command_token)

    import_parser = subparsers.add_parser("import-csv", help="Import opportunities from a CSV")
    import_parser.add_argument("path")
    import_parser.add_argument("--source", default="cli_csv")
    import_parser.set_defaults(handler=_command_import_csv)

    reclassify_parser = subparsers.add_parser(
        "reclassify-ats",
        help="Recompute opportunity ATS labels from parsed URL hosts",
    )
    reclassify_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the proposed histogram without writing changes",
    )
    reclassify_parser.set_defaults(handler=_command_reclassify_ats)

    programme_parser = subparsers.add_parser(
        "reclassify-programmes",
        help="Recompute programme groups from retained source evidence",
    )
    programme_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the proposed histogram without writing changes",
    )
    programme_parser.set_defaults(handler=_command_reclassify_programmes)

    archive_parser = subparsers.add_parser(
        "archive-opportunities",
        help="Reversibly archive confidently non-UK opportunities",
    )
    archive_action = archive_parser.add_mutually_exclusive_group(required=True)
    archive_action.add_argument(
        "--non-uk",
        action="store_true",
        help="Select present locations with confident non-UK evidence",
    )
    archive_action.add_argument(
        "--unarchive",
        action="store_true",
        help="Reverse active non-UK archive markers",
    )
    archive_mode = archive_parser.add_mutually_exclusive_group()
    archive_mode.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="Print the exact plan without writing (the default)",
    )
    archive_mode.add_argument(
        "--apply",
        action="store_true",
        help="Print the plan, back up the database, then audit and apply it",
    )
    archive_parser.set_defaults(
        handler=_command_archive_opportunities,
        apply=False,
        non_uk=False,
        unarchive=False,
    )

    off_cycle_parser = subparsers.add_parser(
        "archive-off-cycle",
        help="Reversibly archive CLOSED-window or off-cycle-programme-tier opportunities",
    )
    off_cycle_selector = off_cycle_parser.add_mutually_exclusive_group()
    off_cycle_selector.add_argument(
        "--closed",
        action="store_true",
        dest="closed_flag",
        help="Select CLOSED-window opportunities for archiving",
    )
    off_cycle_selector.add_argument(
        "--programme-off-cycle",
        action="store_true",
        dest="programme_off_cycle",
        help="Select opportunities whose role title denotes an off-cycle programme tier",
    )
    off_cycle_parser.add_argument(
        "--unarchive",
        action="store_true",
        help="Reverse active archive markers for the selected mode",
    )
    off_cycle_mode = off_cycle_parser.add_mutually_exclusive_group()
    off_cycle_mode.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="Print the exact plan without writing (the default)",
    )
    off_cycle_mode.add_argument(
        "--apply",
        action="store_true",
        help="Print the plan, back up the database, then audit and apply it",
    )
    off_cycle_parser.set_defaults(
        handler=_command_archive_off_cycle,
        apply=False,
        closed_flag=False,
        programme_off_cycle=False,
        unarchive=False,
    )

    repair_cv_parser = subparsers.add_parser(
        "repair-cv-tags",
        help="Derive CV graduation tags, repair variants, and reselect unsafe CVs",
    )
    repair_mode = repair_cv_parser.add_mutually_exclusive_group()
    repair_mode.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="Inspect and print the repair without writing (the default)",
    )
    repair_mode.add_argument(
        "--apply",
        action="store_true",
        help="Back up the database, then apply and audit the repair",
    )
    repair_cv_parser.set_defaults(handler=_command_repair_cv_tags, apply=False)

    resolve_parser = subparsers.add_parser(
        "resolve-targets",
        help="Resolve stored opportunity sources without filling or submitting",
    )
    resolve_parser.add_argument("--limit", type=int)
    resolve_parser.add_argument(
        "--employer",
        default="",
        help="Case-insensitive employer-name filter",
    )
    resolve_parser.add_argument(
        "--ats",
        default="",
        help="ATS filter (for example greenhouse, lever, workday, or custom)",
    )
    resolve_parser.add_argument(
        "--programme",
        choices=[
            ProgrammeType.YEAR_IN_INDUSTRY.value,
            ProgrammeType.SPRING_WEEK.value,
            ProgrammeType.SUMMER.value,
            "all",
        ],
        default="all",
        help=(
            "Programme filter (default: all). With all, unattempted rows are "
            "prioritised year-in-industry, spring-week, summer, then other; "
            "earlier deadlines are attempted first within each tier."
        ),
    )
    attempt_scope = resolve_parser.add_mutually_exclusive_group()
    attempt_scope.add_argument(
        "--only-unattempted",
        action="store_true",
        help="Select only rows with no previous resolution attempt (the default)",
    )
    attempt_scope.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Replay attempted failures/JOB_DETAIL rows; with --limit, preserve "
            "the exact latest attempted cohort for before/after reporting"
        ),
    )
    resolve_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print selected rows using a read-only database connection",
    )
    resolve_parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Concurrent read-only navigators (default: 2; maximum: 8)",
    )
    resolve_parser.add_argument(
        "--delay-seconds",
        type=float,
        default=2.0,
        help="Minimum delay between navigation starts (default: 2.0)",
    )
    resolve_parser.add_argument(
        "--row-timeout-seconds",
        type=float,
        default=45.0,
        help=(
            "Hard wall-clock bound for each isolated navigation process "
            "(default: 45)"
        ),
    )
    resolve_parser.set_defaults(handler=_command_resolve_targets)

    apply_parser = subparsers.add_parser("apply", help="Run one prepared application")
    apply_parser.add_argument("application_id")
    apply_parser.add_argument(
        "--mode",
        choices=[mode.value for mode in RunMode],
        default=RunMode.DRY_RUN.value,
    )
    apply_parser.add_argument("--headed", action="store_true")
    apply_parser.add_argument(
        "--confirm-submit",
        metavar="APPLICATION_ID",
        help=(
            "For submit mode only, retype the exact application ID after the "
            "action-time employer and role review"
        ),
    )
    apply_parser.set_defaults(handler=_command_apply)

    serve_parser = subparsers.add_parser("serve", help="Start the local command centre")
    serve_parser.add_argument("--open", action="store_true", help="Open the dashboard in a browser")
    serve_parser.add_argument("--quiet", action="store_true", help="Disable access logs")
    serve_parser.set_defaults(handler=_command_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("ARGUS stopped.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ARGUS error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
