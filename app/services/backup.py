"""Local hot-backup for ARGUS candidate data.

Creates a timestamped, self-describing copy of the load-bearing state:
the SQLite database (via the online backup API, so no shutdown is needed),
the encryption key, the capture token, and the approved-document vault.
Caches, logs, traces, locks, and notification state are excluded and named
in the manifest. Restore stays a deliberate manual file operation verified
by ``argus audit`` (see OPERATIONS.md); this module never overwrites live
state and never deletes anything outside its own ``backups/`` directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BACKUP_DIRNAME = "backups"
BACKUP_PREFIX = "argus-backup-"
RETAIN_COUNT = 5
MAX_DOCUMENTS_BYTES = 512 * 1024 * 1024
EXCLUDED = (
    "trackr_saves (re-fetchable discovery cache)",
    "notification_delivery_state.sqlite3 (transient outbox)",
    "logs, browser traces, screenshots (evidence, kept with the live dir)",
    "*.lock (process locks)",
)


class BackupError(ValueError):
    """A backup was refused or failed without touching live state."""


@dataclass(frozen=True, slots=True)
class BackupFile:
    name: str
    bytes: int
    sha256: str


def backup_root(data_dir: Path) -> Path:
    return Path(data_dir) / BACKUP_DIRNAME


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _copy_file_strict(source: Path, target: Path) -> BackupFile:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if os.name != "nt":
        os.chmod(target, 0o600)
    size, digest = _sha256_file(target)
    return BackupFile(name=target.name, bytes=size, sha256=digest)


def _sqlite_backup(source_db: Path, target_db: Path) -> BackupFile:
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(str(target_db), timeout=30)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    if os.name != "nt":
        os.chmod(target_db, 0o600)
    size, digest = _sha256_file(target_db)
    return BackupFile(name=target_db.name, bytes=size, sha256=digest)


def _documents_size(documents_dir: Path) -> int:
    total = 0
    if not documents_dir.is_dir():
        return 0
    for root, _dirs, files in os.walk(documents_dir):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def list_backups(data_dir: Path) -> list[dict[str, object]]:
    """Return backup summaries, oldest first; corrupt entries are skipped."""

    root = backup_root(data_dir)
    if not root.is_dir():
        return []
    summaries: list[dict[str, object]] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if not child.is_dir() or not child.name.startswith(BACKUP_PREFIX):
            continue
        manifest_path = child / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict) or manifest.get("backup_id") != child.name:
            continue
        summaries.append(
            {
                "backup_id": child.name,
                "created_at": str(manifest.get("created_at", "")),
                "files": len(manifest.get("files", [])),
                "bytes": int(manifest.get("total_bytes", 0) or 0),
                "db_sha256": str(manifest.get("db_sha256", "")),
            }
        )
    return summaries


def latest_backup(data_dir: Path) -> dict[str, object] | None:
    summaries = list_backups(data_dir)
    return summaries[-1] if summaries else None


def create_backup(
    data_dir: Path,
    *,
    database_path: Path,
    documents_dir: Path,
    secret_key_path: Path,
    api_token_path: Path,
    app_version: str = "",
    retain: int = RETAIN_COUNT,
) -> dict[str, object]:
    """Snapshot load-bearing state into a new timestamped directory."""

    data_dir = Path(data_dir)
    if not Path(database_path).is_file():
        raise BackupError("no database to back up")
    if _documents_size(Path(documents_dir)) > MAX_DOCUMENTS_BYTES:
        raise BackupError("document vault exceeds the backup size bound")
    stamp = _utc_stamp()
    target = backup_root(data_dir) / f"{BACKUP_PREFIX}{stamp}"
    if target.exists():
        raise BackupError("backup slot already exists; retry within a new second")
    target.mkdir(parents=True)

    files: list[BackupFile] = []
    try:
        files.append(_sqlite_backup(Path(database_path), target / "argus.db"))
        for source in (secret_key_path, api_token_path):
            source = Path(source)
            if source.is_file():
                files.append(_copy_file_strict(source, target / source.name))
        documents = Path(documents_dir)
        if documents.is_dir():
            shutil.copytree(documents, target / "documents", symlinks=False)
        manifest = {
            "backup_id": target.name,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "app_version": str(app_version or ""),
            "files": [
                {"name": item.name, "bytes": item.bytes, "sha256": item.sha256}
                for item in files
            ],
            "documents_bytes": _documents_size(target / "documents"),
            "total_bytes": sum(item.bytes for item in files)
            + _documents_size(target / "documents"),
            "db_sha256": next(
                (item.sha256 for item in files if item.name == "argus.db"), ""
            ),
            "excluded": list(EXCLUDED),
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    try:
        keep = max(1, int(retain))
    except (TypeError, ValueError):
        keep = RETAIN_COUNT
    for old in list_backups(data_dir)[: -keep]:
        shutil.rmtree(backup_root(data_dir) / str(old["backup_id"]), ignore_errors=True)

    return {
        "backup_id": target.name,
        "created_at": manifest["created_at"],
        "files": len(files),
        "bytes": manifest["total_bytes"],
        "db_sha256": manifest["db_sha256"],
    }
