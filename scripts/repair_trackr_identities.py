"""Backup-gated repair of stable live-Trackr programme identities.

The command is deliberately separate from the Phase 19 application-window
backfill.  Its default mode projects the complete repair into a temporary
SQLite copy.  ``--apply`` is the only mode that opens the source database for
schema or business writes, and it creates and verifies an online backup first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Sequence
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.domain.targets import TargetKind  # noqa: E402
from app.models import Opportunity  # noqa: E402
from app.runtime_lock import RuntimeLock, runtime_lock_path  # noqa: E402
from app.scouting.application_window import tracker_owned_host  # noqa: E402
from app.scouting.divisions import infer_division  # noqa: E402
from app.scouting.service import IngestibleOpportunity, ScoutService  # noqa: E402
from app.scouting.trackr_identity import (  # noqa: E402
    IdentityCandidate,
    material_identity_signature,
    normalize_trackr_id,
)
from app.scouting.trackr_live import (  # noqa: E402
    SLUG_TO_TYPE,
    TRACKER_URL,
    fetch_programmes,
)
from app.security.crypto import CryptoBox  # noqa: E402


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMMUTABLE_EVIDENCE_TABLES = (
    "documents",
    "answer_entries",
    "automation_runs",
    "question_records",
    "email_messages",
    "submission_intents",
    "submission_authorities",
    "submission_review_bindings",
    "lab_submissions",
)
_PERMITTED_NEW_ARCHIVE_REASONS = frozenset(
    {"closed_application_window", "ambiguous_trackr_identity"}
)
_AUDIT_TABLES = frozenset({"audit_events", "audit_outbox", "audit_chain_state"})
_ALLOWED_AUDIT_ACTIONS = frozenset(
    {
        "opportunity.created",
        "scout.trackr_identity_inserted",
        "scout.opportunity_ingested",
        "scout.trackr_identity_claimed",
        "scout.opportunity_refreshed",
        "opportunity.archived",
    }
)
_IDEMPOTENT_ZERO_STATS = (
    "imported",
    "updated",
    "applications",
    "application_urls_backfilled",
    "legacy_sources_migrated",
    "closed_archived",
    "closed_unarchived",
    "trackr_id_legacy_claimed",
    "trackr_id_inserted",
    "trackr_id_conflict_recovered",
    "trackr_ambiguous_legacy_archived",
    "trackr_ambiguous_legacy_preserved",
    "trackr_ambiguous_legacy_deferred",
    "ingestion_failure",
)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_scalar(value: object) -> object:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _readonly_connection(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _assert_integrity(connection: Any, *, label: str) -> dict[str, object]:
    integrity = list(connection.execute("PRAGMA integrity_check").fetchall())
    values = [str(row[0]) for row in integrity]
    if not values or any(value.casefold() != "ok" for value in values):
        raise RuntimeError(f"{label} integrity check failed: {values!r}")
    foreign_keys = list(connection.execute("PRAGMA foreign_key_check").fetchall())
    if foreign_keys:
        raise RuntimeError(f"{label} foreign-key check failed: {foreign_keys!r}")
    return {"integrity": "ok", "foreign_key_errors": 0}


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
            _assert_integrity(destination, label="copy")
    return destination_path


def create_verified_backup(database_path: Path, backup_dir: Path) -> dict[str, object]:
    """Create the unique pre-write backup and return verified metadata."""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir.resolve() / (
        f"{database_path.name}.pre-phase19-trackr-identity-"
        f"{stamp}-{uuid4().hex}.bak"
    )
    _online_copy(database_path, backup_path)
    with closing(_readonly_connection(backup_path)) as connection:
        checks = _assert_integrity(connection, label="backup")
    return {
        "path": str(backup_path),
        "sha256": _hash_file(backup_path),
        "size_bytes": backup_path.stat().st_size,
        **checks,
    }


def _table_names(connection: Any) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        if str(row[0]) != "sqlite_sequence"
    )


def _table_columns(connection: Any, table: str) -> tuple[str, ...]:
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError(f"unsafe SQLite identifier: {table!r}")
    return tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _table_rows(connection: Any, table: str) -> list[list[object]]:
    columns = _table_columns(connection, table)
    if not columns:
        return []
    quoted = ", ".join(f'"{column}"' for column in columns)
    rows = [
        [_json_scalar(value) for value in row]
        for row in connection.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
    ]
    rows.sort(key=lambda row: json.dumps(row, sort_keys=True, default=str))
    return rows


def _table_evidence(connection: Any, table: str) -> dict[str, object]:
    if table not in _table_names(connection):
        rows: list[list[object]] = []
    else:
        rows = _table_rows(connection, table)
    return {"count": len(rows), "sha256": _stable_hash(rows)}


def _prewrite_evidence(connection: Any) -> dict[str, object]:
    """Canonical full-row guard over every pre-existing SQLite table.

    Schema v8 adds only the nullable Trackr ID to the opportunity rows.  The
    guard represents that column as null even on v7, so the additive migration
    itself is the sole allowed pre-ingest difference.
    """

    table_names = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    )
    evidence: dict[str, object] = {}
    for table in table_names:
        if not _IDENTIFIER.fullmatch(table):
            raise RuntimeError(f"unsafe SQLite table in prewrite guard: {table!r}")
        physical_columns = set(_table_columns(connection, table))
        canonical_columns = set(physical_columns)
        if table == "opportunities":
            canonical_columns.add("trackr_programme_id")
        ordered_columns = tuple(sorted(canonical_columns))
        select_parts = [
            f'"{column}"' if column in physical_columns else f"NULL AS \"{column}\""
            for column in ordered_columns
        ]
        raw_rows = [
            [_json_scalar(value) for value in row]
            for row in connection.execute(
                f'SELECT {", ".join(select_parts)} FROM "{table}"'
            ).fetchall()
        ]
        row_hashes = sorted(_stable_hash(row) for row in raw_rows)
        evidence[table] = {
            "columns": list(ordered_columns),
            "row_count": len(row_hashes),
            "row_sha256": row_hashes,
        }
    return evidence


def _structured_rows(connection: Any, table: str) -> list[dict[str, object]]:
    if table not in _table_names(connection):
        return []
    columns = _table_columns(connection, table)
    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    rows = [
        {column: _json_scalar(value) for column, value in zip(columns, row)}
        for row in connection.execute(
            f'SELECT {quoted_columns} FROM "{table}"'
        ).fetchall()
    ]
    if table in {"audit_events", "audit_outbox"}:
        for row in rows:
            details = str(row.pop("details_json", "") or "")
            row["details_sha256"] = _stable_hash(details)
            if table == "audit_outbox":
                intent_fingerprint = str(
                    row.pop("intent_fingerprint", "") or ""
                )
                last_error = str(row.pop("last_error", "") or "")
                row["intent_fingerprint_sha256"] = _stable_hash(
                    intent_fingerprint
                )
                row["last_error_sha256"] = _stable_hash(last_error)
            if str(row.get("event_type") or "") == "opportunity.archived":
                try:
                    archive_reason = str(json.loads(details).get("reason") or "")
                except (TypeError, ValueError):
                    archive_reason = ""
                row["archive_reason_sha256"] = _stable_hash(archive_reason)
                row["archive_reason"] = (
                    archive_reason
                    if archive_reason in _PERMITTED_NEW_ARCHIVE_REASONS
                    else "<redacted>"
                )
    rows.sort(key=lambda row: (int(row.get("id") or 0), _stable_hash(row)))
    return rows


def _trackr_identity_index_is_exact(connection: Any) -> bool:
    name = "uq_opportunities_trackr_programme_id"
    named = connection.execute(
        "SELECT type, tbl_name, sql FROM sqlite_master WHERE name=?", (name,)
    ).fetchone()
    if named is None:
        return False
    index_row = next(
        (row for row in connection.execute("PRAGMA index_list('opportunities')") if str(row[1]) == name),
        None,
    )
    columns = tuple(
        str(row[2])
        for row in connection.execute(f'PRAGMA index_info("{name}")')
    )
    folded_sql = re.sub(r'["`\[\]()]', " ", str(named[2] or "").casefold())
    folded_sql = " ".join(folded_sql.rstrip(" ;").split())
    predicate = folded_sql.partition(" where ")[2]
    return bool(
        str(named[0]) == "index"
        and str(named[1]) == "opportunities"
        and index_row is not None
        and bool(index_row[2])
        and len(index_row) > 4
        and bool(index_row[4])
        and columns == ("trackr_programme_id",)
        and predicate == "trackr_programme_id is not null"
    )


def _column_set(connection: Any, table: str) -> set[str]:
    return set(_table_columns(connection, table)) if table in _table_names(connection) else set()


def _database_snapshot_connection(connection: Any) -> dict[str, object]:
    checks = _assert_integrity(connection, label="database")
    tables = set(_table_names(connection))
    opportunity_columns = _column_set(connection, "opportunities")
    application_columns = _column_set(connection, "applications")

    def count(table: str) -> int:
        if table not in tables:
            return 0
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])

    opportunity_ids = (
        sorted(str(row[0]) for row in connection.execute("SELECT id FROM opportunities"))
        if "opportunities" in tables
        else []
    )
    application_bindings = (
        sorted(
            (str(row[0]), str(row[1]))
            for row in connection.execute("SELECT id, opportunity_id FROM applications")
        )
        if "applications" in tables and {"id", "opportunity_id"} <= application_columns
        else []
    )
    trackr_bindings = []
    if "trackr_programme_id" in opportunity_columns:
        trackr_bindings = sorted(
            (str(row[0]), None if row[1] is None else str(row[1]))
            for row in connection.execute(
                "SELECT id, trackr_programme_id FROM opportunities"
            )
        )
    else:
        trackr_bindings = [(opportunity_id, None) for opportunity_id in opportunity_ids]
    non_null_bindings = {
        opportunity_id: raw_id
        for opportunity_id, raw_id in trackr_bindings
        if raw_id is not None
    }

    verified_targets: dict[str, list[object]] = {}
    target_columns = {
        "application_url",
        "target_status",
        "resolved_ats_type",
        "resolution_evidence_json",
        "resolved_at",
        "resolution_attempted_at",
    }
    if target_columns <= opportunity_columns:
        for row in connection.execute(
            "SELECT id, application_url, target_status, resolved_ats_type, "
            "resolution_evidence_json, resolved_at, resolution_attempted_at "
            "FROM opportunities WHERE resolved_at IS NOT NULL "
            "AND application_url IS NOT NULL AND application_url <> '' "
            "AND target_status IN (?, ?)",
            (TargetKind.APPLICATION_ENTRY.value, TargetKind.APPLICATION_FORM.value),
        ):
            verified_targets[str(row[0])] = [
                _stable_hash([_json_scalar(value) for value in row[1:]])
            ]

    archives: dict[str, list[object]] = {}
    if "opportunity_archives" in tables:
        for row in connection.execute(
            "SELECT opportunity_id, archived_at, archived_reason "
            "FROM opportunity_archives ORDER BY opportunity_id"
        ):
            archives[str(row[0])] = [_json_scalar(row[1]), _json_scalar(row[2])]

    state_expression = (
        "COALESCE(NULLIF(application_window_status, ''), 'UNKNOWN')"
        if "application_window_status" in opportunity_columns
        else "'UNKNOWN'"
    )
    window_states: dict[str, int] = {}
    by_programme: dict[str, dict[str, int]] = defaultdict(dict)
    if "opportunities" in tables:
        window_states = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                f"SELECT {state_expression}, COUNT(*) FROM opportunities "
                f"GROUP BY {state_expression}"
            )
        }
        for programme, state, total in connection.execute(
            f"SELECT programme_group, {state_expression}, COUNT(*) "
            f"FROM opportunities GROUP BY programme_group, {state_expression}"
        ):
            by_programme[str(programme or "")][str(state)] = int(total)

    real_links = 0
    captured_links = 0
    if "opportunities" in tables:
        has_application_url = "application_url" in opportunity_columns
        query = (
            "SELECT url, application_url FROM opportunities"
            if has_application_url
            else "SELECT url, NULL FROM opportunities"
        )
        for source_url, application_url in connection.execute(query):
            source = str(source_url or "")
            candidate = str(application_url or "")
            if candidate and not tracker_owned_host(candidate):
                captured_links += 1
                real_links += 1
            elif source and not tracker_owned_host(source):
                real_links += 1

    archive_reasons = Counter()
    for archived_at, reason in archives.values():
        if archived_at is not None:
            archive_reasons[str(reason or "")] += 1

    immutable_tables = set(_IMMUTABLE_EVIDENCE_TABLES)
    immutable_tables.update(
        table
        for table in tables
        if "form" in table.casefold() or "submission" in table.casefold()
    )
    immutable_evidence = {
        table: _table_evidence(connection, table)
        for table in sorted(immutable_tables)
    }

    identity_rows = [raw_id for _opportunity_id, raw_id in trackr_bindings if raw_id]
    identity_duplicates: dict[str, int] = {}
    for raw_id, total in Counter(identity_rows).items():
        if total > 1:
            identity_duplicates[raw_id] = total
    retained_unbound_legacy_ids: list[str] = []
    if {"source", "trackr_programme_id"} <= opportunity_columns:
        retained_unbound_legacy_ids = sorted(
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM opportunities WHERE trackr_programme_id IS NULL "
                "AND source LIKE 'trackr_live%'"
            )
        )

    index_valid = bool(
        "trackr_programme_id" in opportunity_columns
        and _trackr_identity_index_is_exact(connection)
    )
    opportunity_windows = {}
    if "application_window_status" in opportunity_columns:
        opportunity_windows = {
            str(row[0]): str(row[1] or "UNKNOWN")
            for row in connection.execute(
                "SELECT id, application_window_status FROM opportunities"
            )
        }
    prewrite_evidence = _prewrite_evidence(connection)
    audit_events_rows = _structured_rows(connection, "audit_events")
    audit_outbox_rows = _structured_rows(connection, "audit_outbox")

    deadline_count = 0
    rolling_true = 0
    rolling_false = 0
    if "opportunities" in tables:
        if "deadline" in opportunity_columns:
            deadline_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM opportunities WHERE deadline IS NOT NULL"
                ).fetchone()[0]
            )
        if "rolling" in opportunity_columns:
            rolling_true = int(
                connection.execute(
                    "SELECT COUNT(*) FROM opportunities WHERE rolling = 1"
                ).fetchone()[0]
            )
            rolling_false = int(
                connection.execute(
                    "SELECT COUNT(*) FROM opportunities WHERE rolling = 0"
                ).fetchone()[0]
            )

    table_counts = {table: count(table) for table in sorted(tables)}
    return {
        **checks,
        "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        "opportunities": count("opportunities"),
        "applications": count("applications"),
        "archives": count("opportunity_archives"),
        "audit_events": count("audit_events"),
        "audit_outbox": count("audit_outbox"),
        "table_counts": table_counts,
        "opportunity_ids": opportunity_ids,
        "opportunity_ids_sha256": _stable_hash(opportunity_ids),
        "application_bindings": application_bindings,
        "application_bindings_sha256": _stable_hash(application_bindings),
        "trackr_bindings": trackr_bindings,
        "trackr_bindings_sha256": _stable_hash(trackr_bindings),
        "non_null_trackr_bindings": non_null_bindings,
        "distinct_trackr_ids": len(set(identity_rows)),
        "trackr_identity_rows": len(identity_rows),
        "trackr_identity_duplicates": identity_duplicates,
        "trackr_identity_index_valid": index_valid,
        "retained_unbound_trackr_legacy": len(retained_unbound_legacy_ids),
        "retained_unbound_trackr_legacy_ids": retained_unbound_legacy_ids,
        "opportunity_windows_by_id": opportunity_windows,
        "verified_targets": verified_targets,
        "verified_targets_sha256": _stable_hash(verified_targets),
        "archives_by_opportunity": archives,
        "archives_sha256": _stable_hash(archives),
        "active_archive_reasons": dict(sorted(archive_reasons.items())),
        "immutable_evidence": immutable_evidence,
        "immutable_evidence_sha256": _stable_hash(immutable_evidence),
        "audit_events_rows": audit_events_rows,
        "audit_outbox_rows": audit_outbox_rows,
        "prewrite_evidence": prewrite_evidence,
        "prewrite_guard_sha256": _stable_hash(prewrite_evidence),
        "window_states": dict(sorted(window_states.items())),
        "by_programme": {
            programme: dict(sorted(states.items()))
            for programme, states in sorted(by_programme.items())
        },
        "real_employer_links": real_links,
        "captured_employer_application_urls": captured_links,
        "deadlines": deadline_count,
        "rolling_true": rolling_true,
        "rolling_false": rolling_false,
    }


def database_snapshot(database_path: Path) -> dict[str, object]:
    with closing(_readonly_connection(database_path)) as connection:
        return _database_snapshot_connection(connection)


def _validate_capture(
    rows: Iterable[IngestibleOpportunity],
) -> tuple[tuple[IngestibleOpportunity, ...], dict[str, object]]:
    captured = tuple(rows)
    if not captured:
        raise ValueError("Live Trackr capture was empty; refusing identity repair")
    signatures: dict[str, tuple[object, ...]] = {}
    canonical: list[dict[str, object]] = []
    for position, row in enumerate(captured):
        source = str(getattr(row, "source", "") or "")
        prefix, separator, slug = source.partition(":")
        if prefix != "trackr_live" or separator != ":" or slug not in SLUG_TO_TYPE:
            raise ValueError(
                f"capture row {position} has an empty or unsupported Trackr source slug"
            )
        tracker_url = str(
            getattr(row, "tracker_url", getattr(row, "source_url", "")) or ""
        )
        expected_tracker_url = TRACKER_URL.format(slug=slug)
        if tracker_url != expected_tracker_url:
            raise ValueError(
                f"capture row {position} does not use the slug's exact canonical tracker URL"
            )
        programme_type = str(getattr(row, "programme_type", "") or "")
        if programme_type != SLUG_TO_TYPE[slug]:
            raise ValueError(
                f"capture row {position} programme type does not match source slug"
            )
        raw_id = normalize_trackr_id(getattr(row, "source_record_id", None))
        if not raw_id:
            raise ValueError(
                f"capture row {position} has a missing or invalid Trackr programme ID"
            )
        signature = material_identity_signature(row)
        previous = signatures.get(raw_id)
        if previous is not None:
            if previous != signature:
                raise ValueError(
                    f"conflicting duplicate Trackr programme ID in capture: {raw_id!r}"
                )
            raise ValueError(f"duplicate Trackr programme ID in capture: {raw_id!r}")
        signatures[raw_id] = signature
        canonical.append(
            {
                "trackr_programme_id": raw_id,
                "material_signature": [_json_scalar(value) for value in signature],
            }
        )
    canonical.sort(key=lambda item: str(item["trackr_programme_id"]))
    return captured, {
        "captured_at": _utc_timestamp(),
        "payload_sha256": _stable_hash(canonical),
        "raw_row_count": len(captured),
        "accepted_id_count": len(canonical),
        "unique_id_count": len(signatures),
        "ids_sha256": _stable_hash(sorted(signatures)),
    }


def _safe_settings(database_path: Path) -> Settings:
    database_path = database_path.resolve()
    if database_path.name.casefold() != "argus.db":
        raise ValueError("Identity repair database must be named argus.db")
    return Settings.load(
        {
            "ARGUS_DATA_DIR": str(database_path.parent),
            "ARGUS_AUTOMATION_MODE": "OFF",
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_ENABLE_TRACKR_LIVE": "false",
            "ARGUS_SWEEP_INTERVAL_HOURS": "0",
            "ARGUS_ENABLE_APPLY_CLICK": "false",
            "ARGUS_ENABLE_NOTIFICATIONS": "false",
        }
    )


def _identity_counts(stats: dict[str, int]) -> dict[str, int]:
    return {
        "existing_bound": int(stats.get("trackr_id_matched", 0)),
        "claim_legacy": int(stats.get("trackr_id_legacy_claimed", 0)),
        "insert_new": int(stats.get("trackr_id_inserted", 0)),
        "ambiguous_raw_ids": int(stats.get("trackr_id_legacy_ambiguous", 0)),
        "ambiguous_legacy_archived": int(
            stats.get("trackr_ambiguous_legacy_archived", 0)
        ),
        "ambiguous_legacy_preserved": int(
            stats.get("trackr_ambiguous_legacy_preserved", 0)
        ),
    }


def _identity_candidate(row: IngestibleOpportunity) -> IdentityCandidate:
    source = str(getattr(row, "source", "") or "")
    raw_id = normalize_trackr_id(getattr(row, "source_record_id", ""))
    division = (
        str(getattr(row, "division", "") or "").strip()
        or infer_division(row.role_title, row.employer)
    )
    return IdentityCandidate(
        raw_id=raw_id,
        employer=row.employer,
        role_title=row.role_title,
        cycle="2026-27",
        source=source,
        tracker_url=str(getattr(row, "tracker_url", getattr(row, "source_url", ""))),
        programme_group=str(getattr(row, "programme_type", "") or ""),
        location=row.location,
        division=division,
    )


def _build_identity_plan_report(
    session: Any,
    service: ScoutService,
    rows: Sequence[IngestibleOpportunity],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Serialize the exact mutation plan before the first row is changed."""

    _items, core_plan = service._plan_trackr_identity_batch(
        tuple(rows), default_cycle="2026-27"
    )
    row_by_id = {
        normalize_trackr_id(getattr(row, "source_record_id", "")): row for row in rows
    }
    candidates = {raw_id: _identity_candidate(row) for raw_id, row in row_by_id.items()}
    plan = []
    for item in sorted(core_plan.items, key=lambda value: value.raw_id):
        candidate = candidates[item.raw_id]
        plan.append(
            {
                "trackr_programme_id": item.raw_id,
                "source_slug": candidate.source.partition(":")[2],
                "strict_claim_key": list(candidate.claim_key),
                "disposition": item.disposition.value,
                "planned_opportunity_id": item.opportunity_id,
                "actual_opportunity_id": None,
                "application_window_status": str(
                    getattr(row_by_id[item.raw_id], "application_window_status").value
                ),
            }
        )

    raw_ambiguous = set(core_plan.ambiguous_raw_ids)
    raw_by_key: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for raw_id in sorted(raw_ambiguous):
        raw_by_key[tuple(candidates[raw_id].claim_key)].append(raw_id)
    legacy_by_key: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for opportunity_id in core_plan.ambiguous_legacy_opportunity_ids:
        record = session.get(Opportunity, opportunity_id)
        if record is None:
            raise RuntimeError(
                f"planned ambiguous legacy opportunity disappeared: {opportunity_id}"
            )
        candidate = IdentityCandidate(
            raw_id="",
            employer=record.employer,
            role_title=record.role_title,
            cycle=record.cycle,
            source=record.source,
            tracker_url=record.url,
            programme_group=record.programme_group,
            location=record.location,
            division=record.division,
            opportunity_id=record.id,
        )
        legacy_by_key[tuple(candidate.claim_key)].append(record.id)
    cohorts = [
        {
            "strict_claim_key": list(key),
            "raw_ids": sorted(raw_ids),
            "retained_legacy_opportunity_ids": sorted(legacy_by_key.get(key, [])),
        }
        for key, raw_ids in sorted(raw_by_key.items())
    ]
    return plan, cohorts


def _attach_actual_opportunity_ids(
    plan: Sequence[dict[str, object]],
    snapshot: dict[str, object],
) -> list[dict[str, object]]:
    by_raw_id = {
        str(raw_id): str(opportunity_id)
        for opportunity_id, raw_id in snapshot["trackr_bindings"]
        if raw_id is not None
    }
    attached = deepcopy(list(plan))
    for entry in attached:
        entry["actual_opportunity_id"] = by_raw_id.get(
            str(entry["trackr_programme_id"])
        )
    return attached


def _normalized_identity_plan_for_comparison(
    plan: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    normalized = deepcopy(list(plan))
    for entry in normalized:
        if entry.get("disposition") == "insert_new":
            entry["actual_opportunity_id"] = "<new>"
    return normalized


def _assert_same_identity_plan(
    projected_plan: Sequence[dict[str, object]],
    projected_ambiguity: Sequence[dict[str, object]],
    live_plan: Sequence[dict[str, object]],
    live_ambiguity: Sequence[dict[str, object]],
) -> None:
    if _normalized_identity_plan_for_comparison(
        projected_plan
    ) != _normalized_identity_plan_for_comparison(live_plan):
        raise RuntimeError("live repair exact identity plan differed from projection")
    if list(projected_ambiguity) != list(live_ambiguity):
        raise RuntimeError("live repair exact identity plan ambiguity mapping differed")


def _rows_by_id(rows: Sequence[dict[str, object]]) -> dict[int, dict[str, object]]:
    return {int(row["id"]): dict(row) for row in rows}


def _validate_audit_appends(
    before: dict[str, object],
    after: dict[str, object],
    stats: dict[str, int],
    identity_plan: Sequence[dict[str, object]],
    expected_archives: dict[str, str],
) -> dict[str, int]:
    before_events = _rows_by_id(before["audit_events_rows"])
    after_events = _rows_by_id(after["audit_events_rows"])
    before_outbox = _rows_by_id(before["audit_outbox_rows"])
    after_outbox = _rows_by_id(after["audit_outbox_rows"])
    if any(after_events.get(key) != row for key, row in before_events.items()):
        raise RuntimeError(
            "Trackr identity repair invariant failed: prior audit event changed"
        )
    if any(after_outbox.get(key) != row for key, row in before_outbox.items()):
        raise RuntimeError(
            "Trackr identity repair invariant failed: prior audit outbox row changed"
        )
    new_events = [after_events[key] for key in sorted(set(after_events) - set(before_events))]
    new_outbox = [after_outbox[key] for key in sorted(set(after_outbox) - set(before_outbox))]

    inserts = int(stats.get("trackr_id_inserted", 0))
    claims = int(stats.get("trackr_id_legacy_claimed", 0))
    refreshed = int(stats.get("updated", 0))
    expected_actions = Counter(
        {
            "opportunity.created": inserts,
            "scout.trackr_identity_inserted": inserts,
            "scout.opportunity_ingested": inserts,
            "scout.trackr_identity_claimed": claims,
            "scout.opportunity_refreshed": refreshed,
            "opportunity.archived": len(expected_archives),
        }
    )
    expected_actions += Counter()
    expected_actions = Counter(
        {action: total for action, total in expected_actions.items() if total}
    )
    inserted_subjects = {
        str(entry["actual_opportunity_id"])
        for entry in identity_plan
        if entry["disposition"] == "insert_new"
    }
    claimed_subjects = {
        str(entry["actual_opportunity_id"])
        for entry in identity_plan
        if entry["disposition"] == "claim_legacy"
    }
    touched_subjects = {
        str(entry["actual_opportunity_id"])
        for entry in identity_plan
        if entry.get("actual_opportunity_id")
    }

    def validate(
        rows: Sequence[dict[str, object]], *, label: str
    ) -> tuple[Counter[str], Counter[tuple[str, str]]]:
        actions: Counter[str] = Counter()
        action_subjects: Counter[tuple[str, str]] = Counter()
        for row in rows:
            action = str(row.get("event_type") or "")
            subject = str(row.get("entity_id") or "")
            if action not in _ALLOWED_AUDIT_ACTIONS:
                raise RuntimeError(
                    "Trackr identity repair invariant failed: unexpected audit action "
                    f"{action!r} in {label}"
                )
            if str(row.get("actor") or "") != "scout":
                raise RuntimeError(
                    "Trackr identity repair invariant failed: unexpected audit actor"
                )
            if str(row.get("entity_type") or "") != "opportunity":
                raise RuntimeError(
                    "Trackr identity repair invariant failed: unexpected audit entity type"
                )
            expected_subjects = touched_subjects
            if action in {
                "opportunity.created",
                "scout.trackr_identity_inserted",
                "scout.opportunity_ingested",
            }:
                expected_subjects = inserted_subjects
            elif action == "scout.trackr_identity_claimed":
                expected_subjects = claimed_subjects
            elif action == "opportunity.archived":
                expected_subjects = set(expected_archives)
                reason = str(row.get("archive_reason") or "")
                if not reason:
                    raise RuntimeError(
                        "Trackr identity repair invariant failed: malformed archive audit"
                    )
                if expected_archives.get(subject) != reason:
                    raise RuntimeError(
                        "Trackr identity repair invariant failed: archive audit subject/reason mismatch"
                    )
            if subject not in expected_subjects:
                raise RuntimeError(
                    "Trackr identity repair invariant failed: unexpected audit subject "
                    f"{subject!r} for {action!r}"
                )
            actions[action] += 1
            action_subjects[(action, subject)] += 1
        exact_subjects = {
            "opportunity.created": inserted_subjects,
            "scout.trackr_identity_inserted": inserted_subjects,
            "scout.opportunity_ingested": inserted_subjects,
            "scout.trackr_identity_claimed": claimed_subjects,
            "opportunity.archived": set(expected_archives),
        }
        for action, subjects in exact_subjects.items():
            actual = Counter(
                {
                    subject: total
                    for (row_action, subject), total in action_subjects.items()
                    if row_action == action
                }
            )
            expected = Counter({subject: 1 for subject in subjects})
            if actual != expected:
                raise RuntimeError(
                    "Trackr identity repair invariant failed: exact audit subjects "
                    f"differed for {action!r}"
                )
        refreshed_subjects = Counter(
            {
                subject: total
                for (action, subject), total in action_subjects.items()
                if action == "scout.opportunity_refreshed"
            }
        )
        if any(total != 1 for total in refreshed_subjects.values()):
            raise RuntimeError(
                "Trackr identity repair invariant failed: duplicate refreshed audit subject"
            )
        return actions, action_subjects

    outbox_actions, _outbox_subjects = validate(new_outbox, label="outbox")
    if outbox_actions != expected_actions:
        raise RuntimeError(
            "Trackr identity repair invariant failed: audit outbox exact delta mismatch "
            f"({dict(outbox_actions)} != {dict(expected_actions)})"
        )
    if new_events:
        event_actions, _event_subjects = validate(new_events, label="events")
    else:
        event_actions = Counter()
    if new_events and event_actions != expected_actions:
        raise RuntimeError(
            "Trackr identity repair invariant failed: audit event exact delta mismatch "
            f"({dict(event_actions)} != {dict(expected_actions)})"
        )
    if new_events:
        if len(new_events) != len(new_outbox):
            raise RuntimeError(
                "Trackr identity repair invariant failed: audit event/outbox delta mismatch"
            )
        for outbox in new_outbox:
            emitted_id = outbox.get("emitted_event_id")
            event = after_events.get(int(emitted_id)) if emitted_id is not None else None
            if event is None or any(
                event.get(column) != outbox.get(column)
                for column in (
                    "created_at",
                    "actor",
                    "event_type",
                    "entity_type",
                    "entity_id",
                    "details_sha256",
                    "archive_reason",
                )
            ):
                raise RuntimeError(
                    "Trackr identity repair invariant failed: audit event/outbox relationship mismatch"
                )
    return {"outbox_delta": len(new_outbox), "event_delta": len(new_events)}


def _assert_repair_invariants(
    before: dict[str, object],
    after: dict[str, object],
    stats: dict[str, int],
    accepted_ids: Sequence[str],
    *,
    identity_plan: Sequence[dict[str, object]],
    ambiguity_cohorts: Sequence[dict[str, object]],
) -> dict[str, object]:
    def refuse(message: str) -> None:
        raise RuntimeError(f"Trackr identity repair invariant failed: {message}")

    if after["integrity"] != "ok" or int(after["foreign_key_errors"]) != 0:
        refuse("post-write SQLite integrity or foreign-key check failed")
    if int(stats.get("ingestion_failure", 0)) != 0:
        refuse(f"ingestion failures were reported: {stats['ingestion_failure']}")
    if int(stats.get("excluded", 0)) != 0:
        refuse(f"accepted capture IDs were excluded: {stats['excluded']}")

    for entry in identity_plan:
        disposition = str(entry.get("disposition") or "")
        planned_opportunity_id = entry.get("planned_opportunity_id")
        actual_opportunity_id = entry.get("actual_opportunity_id")
        if disposition in {"existing_bound", "claim_legacy"}:
            if (
                not planned_opportunity_id
                or actual_opportunity_id != planned_opportunity_id
            ):
                refuse(
                    "stable identity actual opportunity did not equal its "
                    "planned opportunity"
                )
        elif disposition == "insert_new":
            if planned_opportunity_id is not None:
                refuse("insert identity unexpectedly had a planned opportunity")
            if not actual_opportunity_id:
                refuse("insert identity did not resolve to a new actual opportunity")
        else:
            refuse(f"unexpected identity disposition in exact plan: {disposition!r}")

    before_opportunities = set(before["opportunity_ids"])
    after_opportunities = set(after["opportunity_ids"])
    before_applications = {
        str(application_id): str(opportunity_id)
        for application_id, opportunity_id in before["application_bindings"]
    }
    after_applications = {
        str(application_id): str(opportunity_id)
        for application_id, opportunity_id in after["application_bindings"]
    }
    if not before_opportunities <= after_opportunities:
        refuse("one or more pre-existing opportunity IDs disappeared")
    if not set(before_applications) <= set(after_applications):
        refuse("one or more pre-existing application IDs disappeared")
    if any(
        after_applications[application_id] != opportunity_id
        for application_id, opportunity_id in before_applications.items()
    ):
        refuse("a pre-existing application changed opportunity binding")

    inserted = int(stats.get("trackr_id_inserted", 0))
    claimed = int(stats.get("trackr_id_legacy_claimed", 0))
    opportunity_delta = int(after["opportunities"]) - int(before["opportunities"])
    application_delta = int(after["applications"]) - int(before["applications"])
    if opportunity_delta != inserted:
        refuse(
            f"opportunity delta {opportunity_delta} did not equal inserted {inserted}"
        )
    if application_delta != inserted or application_delta != int(
        stats.get("applications", 0)
    ):
        refuse(
            "application additions were not exactly one per newly inserted opportunity"
        )
    new_opportunity_ids = after_opportunities - before_opportunities
    planned_new_opportunity_ids = {
        str(entry["actual_opportunity_id"])
        for entry in identity_plan
        if entry["disposition"] == "insert_new"
    }
    if new_opportunity_ids != planned_new_opportunity_ids:
        refuse(
            "new opportunity IDs did not exactly equal identified insert dispositions"
        )
    new_application_bindings = {
        application_id: opportunity_id
        for application_id, opportunity_id in after_applications.items()
        if application_id not in before_applications
    }
    if (
        len(new_application_bindings) != len(new_opportunity_ids)
        or set(new_application_bindings.values()) != new_opportunity_ids
        or len(set(new_application_bindings.values())) != len(new_opportunity_ids)
    ):
        refuse("new application bindings were not exactly one per new opportunity")

    before_non_null = dict(before["non_null_trackr_bindings"])
    after_non_null = dict(after["non_null_trackr_bindings"])
    if any(after_non_null.get(key) != value for key, value in before_non_null.items()):
        refuse("a prior non-null Trackr ID binding changed")
    raw_id_counts = Counter(
        raw_id for _opportunity_id, raw_id in after["trackr_bindings"] if raw_id
    )
    missing_or_duplicate = {
        raw_id: raw_id_counts.get(raw_id, 0)
        for raw_id in accepted_ids
        if raw_id_counts.get(raw_id, 0) != 1
    }
    if missing_or_duplicate:
        refuse(f"accepted IDs were not represented exactly once: {missing_or_duplicate}")
    distinct_delta = int(after["distinct_trackr_ids"]) - int(
        before["distinct_trackr_ids"]
    )
    if distinct_delta != claimed + inserted:
        refuse(
            f"distinct Trackr ID delta {distinct_delta} != claims+inserts {claimed + inserted}"
        )
    if after["trackr_identity_duplicates"]:
        refuse(f"duplicate persisted Trackr IDs: {after['trackr_identity_duplicates']}")
    if not bool(after["trackr_identity_index_valid"]):
        refuse("schema v8 Trackr identity partial unique index is absent or malformed")

    if before["verified_targets"] != after["verified_targets"]:
        refuse("pre-existing identity-verified target evidence changed")
    before_archives = dict(before["archives_by_opportunity"])
    after_archives = dict(after["archives_by_opportunity"])
    if any(after_archives.get(key) != value for key, value in before_archives.items()):
        refuse("a prior archive sidecar/reason/active state changed or disappeared")
    new_archive_reasons = {
        str(values[1])
        for key, values in after_archives.items()
        if key not in before_archives
    }
    if not new_archive_reasons <= _PERMITTED_NEW_ARCHIVE_REASONS:
        refuse(f"unexpected new archive reasons: {sorted(new_archive_reasons)}")
    if int(after["archives"]) < int(before["archives"]):
        refuse("archive count decreased")

    touched_opportunity_ids = {
        str(entry["actual_opportunity_id"])
        for entry in identity_plan
        if entry.get("actual_opportunity_id")
    }
    expected_new_archives: dict[str, str] = {}
    for entry in identity_plan:
        opportunity_id = str(entry["actual_opportunity_id"])
        expected_window = str(entry["application_window_status"])
        if after["opportunity_windows_by_id"].get(opportunity_id) != expected_window:
            refuse(
                "captured application-window state did not match its actual opportunity"
            )
        if (
            opportunity_id not in before_archives
            and expected_window == "CLOSED"
        ):
            expected_new_archives[opportunity_id] = "closed_application_window"
    for cohort in ambiguity_cohorts:
        for opportunity_id in cohort["retained_legacy_opportunity_ids"]:
            opportunity_id = str(opportunity_id)
            if opportunity_id not in before_archives:
                expected_new_archives[opportunity_id] = "ambiguous_trackr_identity"
    actual_new_archives = {
        key: str(values[1])
        for key, values in after_archives.items()
        if key not in before_archives
    }
    if actual_new_archives != expected_new_archives:
        refuse(
            "exact new archive subject/reason set differed from closed and ambiguity plan"
        )
    if int(stats.get("closed_archived", 0)) != sum(
        reason == "closed_application_window"
        for reason in expected_new_archives.values()
    ):
        refuse("closed archive statistics did not match exact subjects")
    if int(stats.get("trackr_ambiguous_legacy_archived", 0)) != sum(
        reason == "ambiguous_trackr_identity"
        for reason in expected_new_archives.values()
    ):
        refuse("ambiguous archive statistics did not match exact subjects")

    if before["immutable_evidence"] != after["immutable_evidence"]:
        refuse("immutable automation/form/submission/document evidence changed")
    audit_delta = _validate_audit_appends(
        before, after, stats, identity_plan, expected_new_archives
    )

    return {
        "passed": True,
        "opportunities_delta": opportunity_delta,
        "applications_delta": application_delta,
        "distinct_trackr_ids_delta": distinct_delta,
        "rows_deleted": len(before_opportunities - after_opportunities),
        "applications_deleted": len(set(before_applications) - set(after_applications)),
        "verified_targets_unchanged": True,
        "prior_archives_unchanged": True,
        "immutable_evidence_unchanged": True,
        "new_archive_reasons": sorted(new_archive_reasons),
        "new_opportunity_ids": sorted(new_opportunity_ids),
        "new_application_bindings": dict(sorted(new_application_bindings.items())),
        "new_archives": dict(sorted(expected_new_archives.items())),
        "audit_validation": audit_delta,
    }


def _raw_session_connection(session: Any) -> sqlite3.Connection:
    proxied = session.connection().connection
    return getattr(proxied, "driver_connection", proxied)


def _apply_rows(
    database_path: Path,
    rows: Sequence[IngestibleOpportunity],
    *,
    before: dict[str, object],
    accepted_ids: Sequence[str],
    expected_prewrite_guard_sha256: str,
) -> tuple[
    dict[str, int],
    dict[str, object],
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    settings = _safe_settings(database_path)
    settings.ensure_directories()
    database = Database(settings)
    try:
        database.create_schema()
        crypto = CryptoBox.from_path(settings.secret_key_path)
        session = database.SessionLocal()
        try:
            connection = session.connection()
            connection.exec_driver_sql("PRAGMA busy_timeout=0")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            final_prewrite = _database_snapshot_connection(
                _raw_session_connection(session)
            )
            if (
                final_prewrite["prewrite_guard_sha256"]
                != expected_prewrite_guard_sha256
            ):
                raise RuntimeError(
                    "Trackr identity repair prewrite guard changed after projection/backup"
                )
            if any(
                str(row.get("status") or "") == "PENDING"
                for row in final_prewrite["audit_outbox_rows"]
            ):
                raise RuntimeError(
                    "Trackr identity repair prewrite guard contains pending audit outbox evidence"
                )
            service = ScoutService(session, settings, crypto)
            identity_plan, ambiguity_cohorts = _build_identity_plan_report(
                session, service, rows
            )
            stats = service.ingest(
                rows,
                allow_trackr_identity_mutation=True,
            )
            session.flush()
            transactional_after = _database_snapshot_connection(
                _raw_session_connection(session)
            )
            identity_plan = _attach_actual_opportunity_ids(
                identity_plan, transactional_after
            )
            invariant = _assert_repair_invariants(
                before,
                transactional_after,
                stats,
                accepted_ids,
                identity_plan=identity_plan,
                ambiguity_cohorts=ambiguity_cohorts,
            )
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()
    finally:
        database.engine.dispose()
    committed_after = database_snapshot(database_path)
    _assert_repair_invariants(
        before,
        committed_after,
        stats,
        accepted_ids,
        identity_plan=identity_plan,
        ambiguity_cohorts=ambiguity_cohorts,
    )
    return stats, committed_after, invariant, identity_plan, ambiguity_cohorts


def _project_once(
    source_path: Path,
    rows: Sequence[IngestibleOpportunity],
    accepted_ids: Sequence[str],
) -> tuple[
    dict[str, object],
    dict[str, int],
    dict[str, object],
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    before = database_snapshot(source_path)
    with TemporaryDirectory(prefix="argus-phase19-trackr-identity-") as temporary:
        copy_path = Path(temporary) / "argus.db"
        _online_copy(source_path, copy_path)
        stats, after, invariant, identity_plan, ambiguity_cohorts = _apply_rows(
            copy_path,
            rows,
            before=before,
            accepted_ids=accepted_ids,
            expected_prewrite_guard_sha256=str(before["prewrite_guard_sha256"]),
        )
    return before, stats, after, invariant, identity_plan, ambiguity_cohorts


def _business_projection(snapshot: dict[str, object]) -> dict[str, object]:
    projection = {
        key: snapshot[key]
        for key in (
            "schema_version",
            "opportunities",
            "applications",
            "archives",
            "opportunity_ids_sha256",
            "application_bindings_sha256",
            "trackr_bindings_sha256",
            "distinct_trackr_ids",
            "trackr_identity_rows",
            "trackr_identity_duplicates",
            "trackr_identity_index_valid",
            "retained_unbound_trackr_legacy",
            "verified_targets_sha256",
            "archives_sha256",
            "immutable_evidence_sha256",
            "window_states",
            "by_programme",
            "real_employer_links",
            "captured_employer_application_urls",
            "deadlines",
            "rolling_true",
            "rolling_false",
            "active_archive_reasons",
        )
    }
    projection["non_audit_table_counts"] = {
        table: count
        for table, count in dict(snapshot["table_counts"]).items()
        if table not in {"audit_events", "audit_outbox", "audit_chain_state"}
    }
    return projection


def _assert_idempotent(
    before: dict[str, object],
    after: dict[str, object],
    stats: dict[str, int],
    *,
    identity_plan: Sequence[dict[str, object]] | None = None,
    ambiguity_cohorts: Sequence[dict[str, object]] | None = None,
    accepted_ids: Sequence[str] | None = None,
) -> dict[str, object]:
    nonzero = {
        key: int(stats.get(key, 0))
        for key in _IDEMPOTENT_ZERO_STATS
        if int(stats.get(key, 0)) != 0
    }
    if nonzero:
        raise RuntimeError(
            f"Trackr identity repair second pass was not a no-op: {nonzero}"
        )
    if (
        before["prewrite_guard_sha256"] != after["prewrite_guard_sha256"]
        or before["prewrite_evidence"] != after["prewrite_evidence"]
    ):
        raise RuntimeError(
            "Trackr identity repair idempotency prewrite guard changed"
        )
    if _business_projection(before) != _business_projection(after):
        raise RuntimeError("Trackr identity repair second pass changed business state")
    audit_delta = int(after["audit_events"]) - int(before["audit_events"])
    outbox_delta = int(after["audit_outbox"]) - int(before["audit_outbox"])
    if audit_delta or outbox_delta:
        raise RuntimeError(
            "Trackr identity repair second pass emitted audit evidence "
            f"(events={audit_delta}, outbox={outbox_delta})"
        )
    if identity_plan is not None:
        expected_ids = sorted(str(raw_id) for raw_id in (accepted_ids or ()))
        actual_ids = sorted(
            str(entry.get("trackr_programme_id") or "")
            for entry in identity_plan
        )
        invalid_entries = [
            str(entry.get("trackr_programme_id") or "")
            for entry in identity_plan
            if entry.get("disposition") != "existing_bound"
            or not entry.get("planned_opportunity_id")
            or entry.get("actual_opportunity_id")
            != entry.get("planned_opportunity_id")
        ]
        if actual_ids != expected_ids or invalid_entries:
            raise RuntimeError(
                "Trackr identity repair second-pass exact identity plan was not a no-op"
            )
        if list(ambiguity_cohorts or ()):
            raise RuntimeError(
                "Trackr identity repair second-pass ambiguity plan was not empty"
            )
    return {
        "passed": True,
        "stats": stats,
        "audit_events_delta": audit_delta,
        "audit_outbox_delta": outbox_delta,
        "business_counts_identical": True,
    }


def run_identity_repair(
    database_path: Path,
    rows: Sequence[IngestibleOpportunity],
    *,
    apply: bool = False,
    backup_dir: Path | None = None,
) -> dict[str, object]:
    """Project or apply one already-captured, fully identified Trackr batch."""

    captured, capture = _validate_capture(rows)
    database_path = database_path.resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    accepted_ids = tuple(
        normalize_trackr_id(getattr(row, "source_record_id", "")) for row in captured
    )
    if apply:
        lock = RuntimeLock(
            runtime_lock_path(database_path.parent),
            port=0,
            metadata={"operation": "phase19_trackr_identity_repair"},
        )
        with lock:
            return _run_validated_repair(
                database_path,
                captured,
                capture,
                accepted_ids,
                apply=True,
                backup_dir=backup_dir,
            )
    return _run_validated_repair(
        database_path,
        captured,
        capture,
        accepted_ids,
        apply=False,
        backup_dir=backup_dir,
    )


def _run_validated_repair(
    database_path: Path,
    captured: Sequence[IngestibleOpportunity],
    capture: dict[str, object],
    accepted_ids: Sequence[str],
    *,
    apply: bool,
    backup_dir: Path | None,
) -> dict[str, object]:
    (
        source_before,
        projection_stats,
        projected,
        projection_invariant,
        projected_plan,
        projected_ambiguity,
    ) = _project_once(database_path, captured, accepted_ids)
    projection_dispositions = _identity_counts(projection_stats)
    backup_metadata: dict[str, object] | None = None
    after = source_before
    applied_stats: dict[str, int] | None = None
    applied_invariant: dict[str, object] | None = None
    applied_plan: list[dict[str, object]] | None = None
    applied_ambiguity: list[dict[str, object]] | None = None
    idempotency: dict[str, object] | None = None

    if apply:
        resolved_backup_dir = (
            backup_dir.resolve()
            if backup_dir is not None
            else database_path.parent / "backups"
        )
        # This must remain before Database construction/create_schema on the
        # live path.  Projection above touched only a temporary online copy.
        backup_metadata = create_verified_backup(database_path, resolved_backup_dir)
        backup_snapshot = database_snapshot(Path(str(backup_metadata["path"])))
        if (
            backup_snapshot["prewrite_guard_sha256"]
            != source_before["prewrite_guard_sha256"]
        ):
            raise RuntimeError("verified backup does not represent the pre-write database")
        (
            applied_stats,
            after,
            applied_invariant,
            applied_plan,
            applied_ambiguity,
        ) = _apply_rows(
            database_path,
            captured,
            before=source_before,
            accepted_ids=accepted_ids,
            expected_prewrite_guard_sha256=str(
                source_before["prewrite_guard_sha256"]
            ),
        )
        if _identity_counts(applied_stats) != projection_dispositions:
            raise RuntimeError(
                "live repair dispositions differed from its fresh-copy projection"
            )
        _assert_same_identity_plan(
            projected_plan,
            projected_ambiguity,
            applied_plan,
            applied_ambiguity,
        )

        (
            second_before,
            second_stats,
            second_after,
            _second_invariant,
            _second_plan,
            _second_ambiguity,
        ) = _project_once(database_path, captured, accepted_ids)
        idempotency = _assert_idempotent(
            second_before,
            second_after,
            second_stats,
            identity_plan=_second_plan,
            ambiguity_cohorts=_second_ambiguity,
            accepted_ids=accepted_ids,
        )

    effective_stats = applied_stats if applied_stats is not None else projection_stats
    effective_invariant = (
        applied_invariant if applied_invariant is not None else projection_invariant
    )
    effective_plan = applied_plan if applied_plan is not None else projected_plan
    effective_ambiguity = (
        applied_ambiguity if applied_ambiguity is not None else projected_ambiguity
    )
    rows_deleted = int(effective_invariant["rows_deleted"])
    applications_deleted = int(effective_invariant["applications_deleted"])
    if rows_deleted or applications_deleted:
        raise RuntimeError("Trackr identity repair deletion invariant failed")

    return {
        "applied": bool(apply),
        "capture": capture,
        "backup": backup_metadata,
        "before": source_before,
        "projected": projected,
        "after": after,
        "projection_ingest": projection_stats,
        "ingest": effective_stats,
        "projected_identity_plan": projected_plan,
        "identity_plan": effective_plan,
        "ambiguity_cohorts": effective_ambiguity,
        "identity_dispositions": _identity_counts(effective_stats),
        "retained_unbound_legacy": int(
            (after if apply else projected)["retained_unbound_trackr_legacy"]
        ),
        "retained_unbound_rows": list(
            (after if apply else projected)["retained_unbound_trackr_legacy_ids"]
        ),
        "ambiguity": {
            "raw_ids": int(effective_stats.get("trackr_id_legacy_ambiguous", 0)),
            "legacy_archived": int(
                effective_stats.get("trackr_ambiguous_legacy_archived", 0)
            ),
            "legacy_preserved": int(
                effective_stats.get("trackr_ambiguous_legacy_preserved", 0)
            ),
            "legacy_deferred": int(
                effective_stats.get("trackr_ambiguous_legacy_deferred", 0)
            ),
        },
        "invariants": effective_invariant,
        "immutable_evidence_delta": {
            "unchanged": bool(effective_invariant["immutable_evidence_unchanged"]),
            "before_sha256": source_before["immutable_evidence_sha256"],
            "after_sha256": (after if apply else projected)[
                "immutable_evidence_sha256"
            ],
        },
        "audit_delta": {
            "events": int((after if apply else projected)["audit_events"])
            - int(source_before["audit_events"]),
            "outbox": int((after if apply else projected)["audit_outbox"])
            - int(source_before["audit_outbox"]),
        },
        "idempotency": idempotency,
        "rows_deleted": rows_deleted,
        "applications_deleted": applications_deleted,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or backup-gated apply of stable live-Trackr programme IDs; "
            "never runs target resolution, automation, forms, or submission."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="mutate only after a fresh projection and verified SQLite backup",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="explicit SQLite path (default: %LOCALAPPDATA%/ARGUS/argus.db)",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="explicit backup directory (apply mode only)",
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
    # Fetch before inspecting, backing up, migrating, or writing the database.
    rows = fetch_programmes()
    try:
        _validate_complete_cli_capture(rows)
        _validate_capture(rows)
    except ValueError as exc:
        parser.error(str(exc))
    database_path = (
        args.database.resolve()
        if args.database is not None
        else (Settings.load().data_dir / "argus.db").resolve()
    )
    report = run_identity_repair(
        database_path,
        rows,
        apply=bool(args.apply),
        backup_dir=args.backup_dir,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
