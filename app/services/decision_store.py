"""Agent07-owned durable store for typed human decision answers.

Durability boundary (deliberately narrow):
- Primary truth for *uniqueness* is a sidecar SQLite table under
  ``settings.data_dir/decisions/decision_answers.sqlite3`` with
  ``PRIMARY KEY (decision_id)``. Cross-process duplicates collapse to
  exactly one winner via the constraint; SQLite serialises the writers.
- An append-only JSONL mirror (same directory) preserves the historical
  audit shape and stays human-inspectable.
- The legacy global file ``%LOCALAPPDATA%/ARGUS/decisions_answers.jsonl``
  is a READ-ONLY compatibility source. It is never written, never
  deleted, never migrated by guessing. Records lacking the newer
  ``context_revision`` field are accepted with revision ``""`` and can
  therefore never collide with a current context-bound ID.

Fail-closed contract: any malformed line, schema violation, or database
error raises :class:`DecisionStoreCorruptError`. Callers must refuse the
operation (HTTP 503 on the decisions API) rather than invent or drop data.

Answer records are sensitive acknowledgement data (who chose which action
label, when). They are NOT approved field values, document paths, CAPTCHA
proof, or submission evidence, and this module offers no path into the
runner, the answer bank, or submission authority.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Mapping

STORE_DIRNAME = "decisions"
JSONL_NAME = "decisions_answers.jsonl"
SQLITE_NAME = "decision_answers.sqlite3"
LEGACY_ENV_VAR = "LOCALAPPDATA"
LEGACY_SUFFIX = Path("ARGUS") / JSONL_NAME

REQUIRED_RECORD_FIELDS = (
    "decision_id",
    "chosen_option",
    "decided_by",
    "decided_at",
    "application_id",
    "canonical_key",
    "sensitivity",
    "context_revision",
)

_STORE_LOCK = threading.Lock()


class DecisionStoreCorruptError(ValueError):
    """The decision answer store cannot be trusted; fail closed."""


def store_dir(settings) -> Path:
    """Directory holding this settings-root's decision store."""
    return Path(settings.data_dir) / STORE_DIRNAME


def jsonl_path(settings) -> Path:
    """Primary append-only mirror for this settings-root."""
    return store_dir(settings) / JSONL_NAME


def sqlite_path(settings) -> Path:
    """Sidecar uniqueness authority for this settings-root."""
    return store_dir(settings) / SQLITE_NAME


def legacy_jsonl_path() -> Path | None:
    """Legacy global compatibility file, if the environment names one."""
    root = os.environ.get(LEGACY_ENV_VAR, "").strip()
    if not root:
        return None
    return Path(root) / LEGACY_SUFFIX


def init_store(settings) -> tuple[Path, Path]:
    """Create the store directory and sidecar database. Idempotent."""
    directory = store_dir(settings)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    database_path = sqlite_path(settings)
    connection = sqlite3.connect(str(database_path), timeout=10)
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS decision_answers ("
            "decision_id TEXT PRIMARY KEY, "
            "record_json TEXT NOT NULL, "
            "recorded_at REAL NOT NULL)"
        )
        connection.commit()
    finally:
        connection.close()
    if os.name != "nt" and database_path.exists():
        os.chmod(database_path, 0o600)
    return jsonl_path(settings), database_path


def validate_record(record: Mapping[str, object]) -> dict[str, object]:
    """Strictly validate one answer record; raise on anything unexpected."""
    if not isinstance(record, Mapping):
        raise DecisionStoreCorruptError("answer record is not an object")
    missing = [
        key
        for key in REQUIRED_RECORD_FIELDS
        if key not in record or record[key] is None
    ]
    if missing:
        raise DecisionStoreCorruptError(
            f"answer record missing fields: {', '.join(sorted(missing))}"
        )
    for key in REQUIRED_RECORD_FIELDS:
        value = record[key]
        if not isinstance(value, str):
            raise DecisionStoreCorruptError(f"answer record field {key!r} is invalid")
        # Legacy compatibility records carry context_revision "" (see
        # _read_jsonl_file). Every other required field must be non-empty.
        if key != "context_revision" and not value:
            raise DecisionStoreCorruptError(f"answer record field {key!r} is invalid")
    try:
        decided_at = datetime.fromisoformat(str(record["decided_at"]))
    except (TypeError, ValueError) as exc:
        raise DecisionStoreCorruptError("answer record decided_at is invalid") from exc
    if decided_at.tzinfo is None:
        raise DecisionStoreCorruptError("answer record decided_at lacks timezone")
    options = record.get("permitted_options")
    if options is not None and (
        not isinstance(options, list) or not all(isinstance(item, str) for item in options)
    ):
        raise DecisionStoreCorruptError("answer record permitted_options is invalid")
    return dict(record)


def _read_jsonl_file(path: Path, *, source: str) -> dict[str, dict[str, object]]:
    """Strictly parse one JSONL file. Legacy records without a revision
    keep revision "" (compatibility only; they can never match a current
    context-bound ID)."""
    records: dict[str, dict[str, object]] = {}
    if not path.exists():
        return records
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DecisionStoreCorruptError(f"{source} store is unreadable") from exc
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DecisionStoreCorruptError(
                f"{source} store line {lineno} is not valid JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise DecisionStoreCorruptError(
                f"{source} store line {lineno} is not an object"
            )
        if source == "legacy" and not parsed.get("context_revision"):
            parsed = {**parsed, "context_revision": ""}
        try:
            validated = validate_record(parsed)
        except DecisionStoreCorruptError as exc:
            raise DecisionStoreCorruptError(
                f"{source} store line {lineno} is invalid: {exc}"
            ) from exc
        decision_id = str(validated["decision_id"])
        records.setdefault(decision_id, validated)
    return records


def _read_sqlite(settings) -> dict[str, dict[str, object]]:
    init_store(settings)
    try:
        connection = sqlite3.connect(str(sqlite_path(settings)), timeout=10)
    except sqlite3.Error as exc:
        raise DecisionStoreCorruptError("answer database is unreadable") from exc
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            rows = connection.execute(
                "SELECT decision_id, record_json FROM decision_answers"
            ).fetchall()
        except sqlite3.Error as exc:
            raise DecisionStoreCorruptError("answer database is corrupt") from exc
    finally:
        connection.close()
    records: dict[str, dict[str, object]] = {}
    for decision_id, record_json in rows:
        try:
            parsed = json.loads(str(record_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DecisionStoreCorruptError(
                f"answer database record {decision_id!r} is corrupt"
            ) from exc
        try:
            validated = validate_record(parsed)
        except DecisionStoreCorruptError as exc:
            raise DecisionStoreCorruptError(
                f"answer database record {decision_id!r} is invalid: {exc}"
            ) from exc
        records[str(validated["decision_id"])] = validated
    return records


def _same_file(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return False


def read_records(settings) -> dict[str, dict[str, object]]:
    """Read committed answers plus legacy evidence belonging to this root.

    The primary JSONL file is an audit mirror, not commit authority: an
    append can survive a rolled-back SQLite transaction. Still validate
    its format so corruption remains an explicit fail-closed error.
    """
    merged = _read_sqlite(settings)
    primary_jsonl = jsonl_path(settings)
    _read_jsonl_file(primary_jsonl, source="primary")
    legacy = legacy_jsonl_path()
    if (
        legacy is not None
        and _same_file(Path(settings.data_dir), legacy.parent)
        and not _same_file(legacy, primary_jsonl)
    ):
        for decision_id, record in _read_jsonl_file(legacy, source="legacy").items():
            merged.setdefault(decision_id, record)
    return merged


def has_answer(settings, decision_id: str) -> bool:
    """Return whether an answer is recorded. Corrupt stores raise."""
    return str(decision_id) in read_records(settings)


def try_record(settings, record: Mapping[str, object]) -> bool:
    """Persist one validated answer; False when the ID is already taken.

    Cross-process safe: the sidecar PRIMARY KEY decides the winner. The
    JSONL mirror is appended only after the row wins. Raises
    :class:`DecisionStoreCorruptError` on invalid records or I/O failure.
    """
    try:
        validated = validate_record(record)
    except DecisionStoreCorruptError as exc:
        raise DecisionStoreCorruptError(f"refusing to store invalid answer: {exc}") from exc
    init_store(settings)
    line = json.dumps(validated, separators=(",", ":"), sort_keys=True) + "\n"
    with _STORE_LOCK:
        connection = sqlite3.connect(str(sqlite_path(settings)), timeout=10)
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO decision_answers "
                    "(decision_id, record_json, recorded_at) VALUES (?, ?, "
                    "strftime('%s','now'))",
                    (str(validated["decision_id"]), line.strip()),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
            try:
                path = jsonl_path(settings)
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
                descriptor = os.open(path, flags, 0o600)
                try:
                    os.write(descriptor, line.encode("utf-8"))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if os.name != "nt":
                    os.chmod(path, 0o600)
            except OSError as exc:
                connection.rollback()
                raise DecisionStoreCorruptError(
                    "answer mirror append failed; row rolled back"
                ) from exc
            connection.commit()
        finally:
            connection.close()
        return True
