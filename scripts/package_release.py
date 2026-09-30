#!/usr/bin/env python3
"""Build and verify a deterministic, evidence-bound ARGUS source release.

The release command is intentionally source-first.  A release made from a
Git checkout must be accompanied by a fresh verification manifest whose
source binding and raw-log hashes still match the working tree.  The archive
is an allowlisted source distribution; runtime data, credentials, browser
state, and old build output are never release inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


_ALLOWED_DIRECTORIES = (
    "app",
    "extension",
    "packaging",
    "samples",
    "scripts",
    "tests",
    "docs",
)
_ALLOWED_FILES = (
    ".env.example",
    "pyproject.toml",
    "requirements.txt",
    "README.md",
    "SECURITY.md",
    "OPERATIONS.md",
    "VERIFICATION.md",
    "launcher.py",
    "launcher.spec",
)
_REQUIRED_FILES = (
    "app/main.py",
    "extension/manifest.json",
    "scripts/setup.sh",
    "pyproject.toml",
    "requirements.txt",
    "README.md",
    "SECURITY.md",
    "OPERATIONS.md",
    "VERIFICATION.md",
)

# A release archive must never accidentally become a backup or a data dump.
# These names are checked case-insensitively at every path component.
_EXCLUDED_PARTS = frozenset(
    part.casefold()
    for part in {
        ".git",
        ".worktrees",
        ".venv",
        ".hermes",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cache",
        "cache",
        "caches",
        "data",
        "local-data",
        "artifacts",
        "dist",
        "build",
        "build-candidate",
        "build-final",
        "build-ultimate",
        "release-artifacts",
        "release-candidate",
        "release-candidate-final",
        "release-candidate-ultimate",
        "release-artifacts-final-check",
        "release-output",
        "release-artifacts-repeat",
        "release-evidence",
        "test-evidence",
        "evidence",
        "test-results",
        "playwright-report",
        "backups",
        "backup",
        "browser-profile",
        "browser-profiles",
        "profiles",
        "cookies",
        "traces",
        "screenshots",
        "candidate-docs",
        "candidates",
        "cv",
        "cvs",
        "superpowers",
    }
)
_FORBIDDEN_NAMES = frozenset(
    name.casefold()
    for name in {
        ".env",
        ".env.local",
        "secret.key",
        "api_token.txt",
        "argus.db",
        "candidate.json",
        "profile.json",
        "cookies.json",
    }
)
_FORBIDDEN_SUFFIXES = frozenset(
    {
        ".pyc",
        ".pyo",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".key",
        ".pem",
        ".pfx",
        ".p12",
        ".jks",
        ".der",
        ".crt",
        ".cer",
        ".zip",
        ".trace",
        ".har",
    }
)
_PRIVATE_DOCUMENT_SUFFIXES = frozenset(
    {".csv", ".doc", ".docx", ".json", ".md", ".pdf", ".rtf", ".txt", ".yaml", ".yml"}
)
_PRIVATE_DOCUMENT_TOKENS = frozenset(
    {
        "candidate", "profile", "resume", "curriculum", "cv", "credential", "secret", "token",
        "backup", "evidence", "worklog", "forensic", "privacy_tooling", "engineering_log",
        "implementation_log", "max_pressure", "state_", "release_review",
        "change_log", "orchestration_log", "release_evidence", "upgrade", "forensic",
        "hardening_report", "provider_egress",
    }
)
_PRIVATE_PATH_TOKENS = frozenset(
    {
        "cookie", "cookies", "keylog", "sslkeylogfile", "live_submit",
        "candidate", "resume", "screenshot", "old-release",
        "old_release", "oldrelease", "old", "key", "keys", "backup", "backups",
    }
)
_FORBIDDEN_CONTENT_MARKERS = frozenset(
    {"trackr_cookie", "submission_token", "sslkeylogfile", "private_key", "secret_key"}
)
_EXCLUDED_ROOT_REASONS = {
    "release-evidence": "verification evidence",
    "test-evidence": "verification evidence",
    "evidence": "verification evidence",
    "dist": "old release artifact",
    "release-output": "old release artifact",
    "build": "build artifact",
    "backups": "backup",
    ".venv": "virtual environment",
    ".git": "Git internals",
    ".hermes": "local runtime state",
    "data": "local application data",
    "local-data": "local application data",
}
_EVIDENCE_SCHEMA_VERSION = 2
_SNAPSHOT_SCHEMA_VERSION = 1
_ARCHIVE_MAX_BYTES = 256 * 1024 * 1024
_ARCHIVE_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_ARCHIVE_MAX_MEMBERS = 4096
_VOLATILE_EXCLUDED_ROOTS = frozenset({
    "release-evidence", "test-evidence", ".venv", ".pytest_cache", "__pycache__", ".hermes",
    "dist", "build", "build-candidate", "build-final", "build-ultimate",
    "release-artifacts", "release-output",
})
_APPROVED_RELEASE_OUTPUT_ROOTS = frozenset({
    "dist", "release-artifacts", "release-output", "build", "build-candidate",
    "build-final", "build-ultimate",
})
_CORE_E2E_EVIDENCE = frozenset(
    {
        "tests/e2e/test_adversarial_flows.py",
        "tests/e2e/test_application_flows.py",
        "tests/e2e/test_application_navigator_journeys.py",
        "tests/e2e/test_apply_ui.py",
        "tests/e2e/test_dashboard.py",
        "tests/e2e/test_lab_adapter_variants.py",
        "tests/e2e/test_local_portal_end_to_end.py",
        "tests/e2e/test_static_legal_boundaries_campaign.py",
        "tests/e2e/test_submission_unknown.py",
    }
)
_OPTIONAL_E2E_EVIDENCE = frozenset(
    {"tests/e2e/test_default_source_resolution.py"}
)


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    archive: Path
    checksum_file: Path
    sha256: str
    file_count: int
    manifest_file: Path
    signature_file: Path | None = None
    archive_verified_twice: bool = False


class _ReleaseCapability:
    """Opaque, process-local proof issued by the verifier after a passed run.

    The random secret is intentionally held only in memory. It is never put
    into JSON evidence or the release manifest, so a copied/edited evidence
    file cannot authorize packaging in another process.
    """

    __slots__ = ("_secret", "_message", "_mac", "_root_identity")

    def __init__(self, *, secret: bytes, message: bytes, mac: bytes, root_identity: bytes) -> None:
        self._secret = bytes(secret)
        self._message = bytes(message)
        self._mac = bytes(mac)
        self._root_identity = bytes(root_identity)


def _canonical_root_identity(root: Path) -> dict[str, Any]:
    """Return a process-local identity for one canonical checkout root.

    The canonical path prevents replay into a byte-identical clone at another
    location.  The filesystem identity additionally makes replacement of the
    checkout at the same path fail closed when the platform exposes a stable
    device/file identifier.  This value is never written to evidence or a
    release manifest; it only participates in the in-memory capability MAC.
    """

    candidate = Path(root).expanduser()
    if _has_reparse_point(candidate):
        raise ValueError("release root must not be a link or reparse point")
    resolved = candidate.resolve(strict=True)
    if _has_reparse_point(resolved):
        raise ValueError("release root must not be a link or reparse point")
    info = resolved.stat()
    return {
        "canonical_path": os.path.normcase(os.path.realpath(os.fspath(resolved))),
        "st_dev": int(getattr(info, "st_dev", 0)),
        "st_ino": int(getattr(info, "st_ino", 0)),
    }


def _capability_message(
    evidence: Mapping[str, Any],
    *,
    root_identity: Mapping[str, Any],
) -> bytes:
    source = evidence.get("source")
    provenance = evidence.get("provenance")
    if not isinstance(source, Mapping) or not isinstance(provenance, Mapping):
        raise ValueError("verification evidence cannot issue a release capability")
    return _canonical_json(
        {
            "schema_version": 1,
            "evidence_sha256": _sha256_bytes(_canonical_json(dict(evidence))),
            "source_binding_sha256": source.get("binding_sha256"),
            "run_nonce": provenance.get("run_nonce"),
            "plan_sha256": provenance.get("plan_sha256"),
            "root_identity": dict(root_identity),
        }
    )


def _issue_release_capability(
    evidence: Mapping[str, Any],
    *,
    root: Path,
) -> _ReleaseCapability:
    """Issue the in-memory verifier-to-packager capability.

    The supported production path is ``scripts.verify --package-output``;
    focused tests use this private helper to model that same-process hand-off
    without serializing the secret.
    """

    if not isinstance(evidence, Mapping):
        raise ValueError("release capability requires an evidence mapping")
    identity = _canonical_root_identity(root)
    message = _capability_message(evidence, root_identity=identity)
    secret = secrets.token_bytes(32)
    return _ReleaseCapability(
        secret=secret,
        message=message,
        mac=hmac.new(secret, message, hashlib.sha256).digest(),
        root_identity=_canonical_json(identity),
    )


def _validate_release_capability(
    capability: object,
    evidence: Mapping[str, Any],
    *,
    root: Path,
) -> None:
    if not isinstance(capability, _ReleaseCapability):
        raise ValueError(
            "standalone test evidence cannot authorize packaging; "
            "run verifier and packaging in the same invocation"
        )
    try:
        identity = _canonical_root_identity(root)
        identity_bytes = _canonical_json(identity)
        message = _capability_message(evidence, root_identity=identity)
        expected = hmac.new(capability._secret, message, hashlib.sha256).digest()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("invalid in-memory release capability") from exc
    if (
        not capability._secret
        or not hmac.compare_digest(capability._root_identity, identity_bytes)
        or not hmac.compare_digest(capability._message, message)
        or not hmac.compare_digest(capability._mac, expected)
    ):
        raise ValueError("release capability does not match this verifier evidence")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *arguments: str, check: bool = False) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    if result.returncode != 0:
        if check:
            detail = (result.stderr or result.stdout or "git command failed").strip()
            raise ValueError(detail)
        return None
    return result.stdout


def _has_reparse_point(path: Path) -> bool:
    """Detect links and Windows junctions without following them."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _normalise_relative(relative: str | Path) -> str:
    """Return a conservative archive/Git relative path or reject it."""

    raw = str(relative).replace("\\", "/")
    normal = unicodedata.normalize("NFKC", raw)
    if normal != raw or not normal or "\x00" in normal:
        raise ValueError(f"non-canonical release path: {raw!r}")
    candidate = Path(normal)
    if candidate.is_absolute() or re.match(r"^[A-Za-z]:", normal) or normal.startswith("//"):
        raise ValueError(f"absolute release path: {raw!r}")
    parts = normal.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe release path: {raw!r}")
    # The release inventory is deliberately ASCII-only.  This closes
    # look-alike separators and confusable path components at the boundary.
    if any(ord(character) > 0x7F for character in normal):
        raise ValueError(f"non-ASCII/confusable release path: {raw!r}")
    return "/".join(parts)


def _assert_contained(path: Path, root: Path) -> str:
    """Check lexical and canonical containment and every ancestor."""

    root = root.resolve(strict=True)
    if _has_reparse_point(root):
        raise ValueError("release root must not be a link or reparse point")
    if path.resolve(strict=False) == root:
        return ""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path is outside release root: {path}") from exc
    normal = _normalise_relative(relative)
    current = root
    for component in Path(normal).parts:
        current = current / component
        if _has_reparse_point(current):
            raise ValueError(f"release path contains link/reparse point: {normal}")
    try:
        canonical = path.resolve(strict=False)
        canonical.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"release path escapes canonical root: {normal}") from exc
    return normal


def _validate_release_output_dir(path: Path, root: Path) -> Path:
    """Return an approved output directory under the checkout.

    Release output is deliberately restricted to a named, ignored release
    root (normally ``dist``).  This prevents an absolute path, a junction, or
    a symlink from turning packaging into an arbitrary file writer.
    """

    raw_root = root.expanduser()
    if _has_reparse_point(raw_root):
        raise ValueError("release root must not be a link or reparse point")
    root = raw_root.resolve(strict=True)
    if _has_reparse_point(root):
        raise ValueError("release root must not be a link or reparse point")
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    # Do not resolve before the lexical check: a symlink must be rejected,
    # rather than silently accepted after resolving to an in-root target.
    lexical = Path(os.path.abspath(os.path.normpath(str(candidate))))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValueError("release output_dir must be contained by the approved release root") from exc
    if not relative.parts:
        raise ValueError("release output_dir must be a child release directory")
    if relative.parts[0].casefold() not in _APPROVED_RELEASE_OUTPUT_ROOTS:
        raise ValueError(
            "release output_dir must be under an approved release root "
            f"({', '.join(sorted(_APPROVED_RELEASE_OUTPUT_ROOTS))})"
        )
    current = root
    for component in relative.parts:
        current = current / component
        if current.exists() and _has_reparse_point(current):
            raise ValueError("release output_dir contains a link or reparse point")
    resolved = lexical.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("release output_dir escapes the approved release root") from exc
    return lexical


def _write_frozen_file(target: Path, payload: bytes, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        temporary.write_bytes(payload)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _freeze_external_input(source: Path, *, stage_root: Path, label: str) -> Path:
    """Copy one non-checkout release input into the private immutable stage.

    Executables and detached-signature inputs are deliberately staged before
    their metadata is inspected.  The source is read once, checked again
    before the staged copy is accepted, and all later validation uses only the
    staged bytes.  A caller may therefore replace the original file after the
    freeze without changing the bytes described by the release manifest.
    """

    source = source.expanduser()
    if _has_reparse_point(source) or not source.is_file():
        raise ValueError(f"{label} is not a regular file")
    before_stat = source.stat()
    payload = source.read_bytes()
    digest = _sha256_bytes(payload)
    after_stat = source.stat()
    if before_stat.st_size != after_stat.st_size or _sha256_file(source) != digest:
        raise ValueError(f"{label} changed while creating immutable release stage")
    target = stage_root / "__inputs__" / label / source.name
    _write_frozen_file(target, payload, stat.S_IMODE(before_stat.st_mode))
    try:
        os.chmod(target, stat.S_IMODE(before_stat.st_mode) & ~0o222)
    except OSError:
        pass
    if _sha256_file(target) != digest or target.stat().st_size != len(payload):
        raise ValueError(f"{label} immutable staged bytes failed verification")
    return target


@contextmanager
def _frozen_release_stage(
    *,
    root: Path,
    files: Sequence[Path],
    expected_source: Mapping[str, Any],
) -> Iterable[tuple[Path, tuple[Path, ...]]]:
    """Capture release bytes once and use that immutable stage for all work.

    Each source file is read into memory, written atomically to a private
    temporary tree, and checked against the source again. A source snapshot is
    then re-read before yielding. Any mutation during capture aborts the
    release; mutations after this point cannot change the archive because all
    verification/rebuild operations use the staged bytes.
    """

    with tempfile.TemporaryDirectory(prefix="argus-release-stage-") as temporary:
        stage_root = Path(temporary)
        staged: list[Path] = []
        for source in files:
            relative = _normalise_relative(source.relative_to(root))
            if _has_reparse_point(source) or not source.is_file():
                raise ValueError(f"release source changed or is not a regular file: {relative}")
            payload = source.read_bytes()
            digest = _sha256_bytes(payload)
            before_stat = source.stat()
            target = stage_root / relative
            _write_frozen_file(target, payload, stat.S_IMODE(before_stat.st_mode))
            after_stat = source.stat()
            if (
                before_stat.st_size != after_stat.st_size
                or _sha256_file(source) != digest
            ):
                raise ValueError(f"source changed while creating immutable release stage: {relative}")
            # Read-only permissions are a useful accidental-mutation guard;
            # archive verification still hashes every staged member.
            try:
                os.chmod(target, stat.S_IMODE(before_stat.st_mode) & ~0o222)
            except OSError:
                pass
            staged.append(target)
        staged = sorted(staged, key=lambda item: item.relative_to(stage_root).as_posix())
        staged_inventory = _file_inventory(staged, stage_root)
        expected_inventory = list(expected_source.get("inventory", []))
        if staged_inventory != expected_inventory:
            raise ValueError("immutable release stage does not match the verified source inventory")
        current_source = source_snapshot(root)
        if dict(current_source) != dict(expected_source):
            raise ValueError("source changed before immutable release stage was sealed")
        yield stage_root, tuple(staged)


def _excluded_reason(relative: str) -> str | None:
    parts = relative.casefold().split("/")
    for part in parts:
        if part in _EXCLUDED_ROOT_REASONS:
            return _EXCLUDED_ROOT_REASONS[part]
        if part in _EXCLUDED_PARTS:
            return "explicitly excluded local/artifact root"
    return None


def _is_safe_release_file(path: Path, root: Path) -> bool:
    relative_name = _assert_contained(path, root)
    if not path.exists() or not path.is_file():
        return False
    if _excluded_reason(relative_name):
        return False
    filename = path.name.casefold()
    if filename in _FORBIDDEN_NAMES or path.suffix.casefold() in _FORBIDDEN_SUFFIXES:
        return False
    if filename == "verification.md":
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").casefold()
        except OSError:
            return False
        if any(marker in text for marker in ("c:\\users\\", "/home/", "secret.key", "screenshot", "receipt ", "backup")):
            return False
    if path.suffix.casefold() in _PRIVATE_DOCUMENT_SUFFIXES and any(
        token in filename for token in _PRIVATE_DOCUMENT_TOKENS
    ):
        return False
    if filename.startswith(".env") and relative_name.casefold() != ".env.example":
        return False
    path_parts = relative_name.casefold().split("/")
    if any(part in {"key", "keys", "old", "old-release", "old_release", "oldrelease", "backup", "backups"} for part in path_parts):
        return False
    if any(token in filename for token in _PRIVATE_PATH_TOKENS):
        return False
    if filename in {
        "test_privacy_scan.py", "test_release_tooling_task8b.py", "test_allowlist_matching.py",
        "test_mode_and_egress.py", "test_cli.py", "test_mail_service.py", "test_repositories.py",
    }:
        return False
    return True


def _iter_release_files(root: Path) -> Iterable[Path]:
    root = root.resolve(strict=True)
    _assert_contained(root, root)
    yielded: set[Path] = set()
    for filename in _ALLOWED_FILES:
        path = root / filename
        if _is_safe_release_file(path, root):
            yielded.add(path)
            yield path
    for directory in _ALLOWED_DIRECTORIES:
        base = root / directory
        if not base.exists():
            continue
        if _has_reparse_point(base):
            raise ValueError(f"release directory is a link/reparse point: {directory}")
        pending = [base]
        while pending:
            current = pending.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda item: unicodedata.normalize("NFKC", item.name).casefold())
            except OSError as exc:
                raise ValueError(f"unable to inspect release directory: {current.name}") from exc
            for entry in entries:
                path = Path(entry.path)
                _assert_contained(path, root)
                if _has_reparse_point(path):
                    raise ValueError(f"release tree contains link/reparse point: {path.name}")
                if entry.is_dir(follow_symlinks=False):
                    if _excluded_reason(_assert_contained(path, root)):
                        continue
                    pending.append(path)
                elif _is_safe_release_file(path, root) and path not in yielded:
                    yielded.add(path)
                    yield path


def _validate_required_files(root: Path) -> None:
    missing = [relative for relative in _REQUIRED_FILES if not (root / relative).is_file()]
    if missing:
        raise ValueError("missing required release files: " + ", ".join(missing))


def _file_inventory(files: Sequence[Path], root: Path) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    for source in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = source.relative_to(root).as_posix()
        members.append(
            {
                "path": relative,
                "sha256": _sha256_file(source),
                "size": source.stat().st_size,
            }
        )
    return members


def _git_base_inventory(root: Path, files: Sequence[Path]) -> list[dict[str, Any]]:
    """Hash the same safe source inventory at the checked-out base commit."""

    inventory: list[dict[str, Any]] = []
    for source in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = source.relative_to(root).as_posix()
        object_name = _git(root, "rev-parse", f"HEAD:{relative}")
        if object_name is None:
            inventory.append({"path": relative, "sha256": None, "size": None})
            continue
        try:
            base_bytes = subprocess.run(
                ["git", "-C", str(root), "show", f"HEAD:{relative}"],
                check=True,
                capture_output=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError(f"unable to read Git base source: {relative}") from exc
        inventory.append(
            {
                "path": relative,
                "sha256": _sha256_bytes(base_bytes),
                "size": len(base_bytes),
            }
        )
    return inventory


def _git_working_paths(root: Path) -> list[str]:
    raw = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if raw is None:
        raise ValueError("release root is not a usable Git repository")
    # Git emits NUL-delimited UTF-8 paths.  Reject malformed or non-canonical
    # names rather than allowing a display/normalisation mismatch.
    values = raw.split("\x00")
    paths: list[str] = []
    for value in values:
        if not value:
            continue
        normal = _normalise_relative(value)
        if normal != value.replace("\\", "/"):
            raise ValueError(f"Git path is not canonical: {value!r}")
        paths.append(normal)
    # Git deliberately hides ignored files from the normal listing.  Walk
    # only the source-shaped top-level roots to include ignored local source
    # overrides without traversing virtual environments or old artifacts.
    roots = set(paths)
    excluded_top = {name.casefold() for name in _EXCLUDED_PARTS} | {name.casefold() for name in _EXCLUDED_ROOT_REASONS}
    for entry in os.scandir(root):
        if entry.name.casefold() in excluded_top or entry.name.casefold() == ".git":
            continue
        path = Path(entry.path)
        if entry.is_file(follow_symlinks=False):
            roots.add(_normalise_relative(path.relative_to(root)))
        elif entry.is_dir(follow_symlinks=False):
            for current, directories, filenames in os.walk(path, topdown=True, followlinks=False):
                directories[:] = [name for name in directories if name.casefold() not in excluded_top]
                for filename in filenames:
                    candidate = Path(current) / filename
                    if _has_reparse_point(candidate):
                        raise ValueError(f"Git working-tree path is a link/reparse point: {candidate.name}")
                    roots.add(_normalise_relative(candidate.relative_to(root)))
    return sorted(roots, key=str.casefold)


def _dirty_tree_inventory(root: Path, paths: Sequence[str]) -> tuple[list[dict[str, Any]], list[str]]:
    inventory: list[dict[str, Any]] = []
    # Report the complete explicit exclusion policy deterministically.  The
    # evidence directory is intentionally generated after source capture; its
    # contents must not make a previously captured binding stale.
    excluded_roots: set[str] = set(_EXCLUDED_ROOT_REASONS)
    excluded_inventory: list[dict[str, Any]] = []
    for relative in paths:
        path = root / Path(relative)
        _assert_contained(path, root)
        reason = _excluded_reason(relative)
        if reason:
            excluded_roots.add(relative.split("/", 1)[0])
            top = relative.split("/", 1)[0]
            if top.casefold() not in _VOLATILE_EXCLUDED_ROOTS:
                excluded_inventory.append({"path": relative, "sha256": _sha256_file(path) if path.is_file() else None, "size": path.stat().st_size if path.is_file() else None})
            continue
        if not path.exists() or not path.is_file():
            raise ValueError(f"Git working-tree path is missing or not a regular file: {relative}")
        inventory.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
                "release_member": _is_safe_release_file(path, root),
            }
        )
    # Keep an explicit, deterministic binding for stable excluded/ignored
    # files.  Volatile execution roots are named policy exclusions because
    # pytest and the verifier write there while evidence is being produced.
    inventory.extend(excluded_inventory)
    inventory.sort(key=lambda item: str(item["path"]).casefold())
    return inventory, sorted(excluded_roots, key=str.casefold)


def _git_metadata(root: Path, files: Sequence[Path]) -> dict[str, Any]:
    commit = (_git(root, "rev-parse", "--verify", "HEAD") or "").strip()
    if not commit:
        return {"repository": False, "base_commit": None, "base_inventory": []}
    base_inventory = _git_base_inventory(root, files)
    tree = _git(root, "ls-tree", "-r", "--full-tree", "--format=%(objectname) %(path)") or ""
    diff = _git(root, "diff", "--binary", "HEAD", "--") or ""
    working_paths = _git_working_paths(root)
    dirty_inventory, excluded_roots = _dirty_tree_inventory(root, working_paths)
    dirty_hash = _sha256_bytes(_canonical_json(dirty_inventory))
    return {
        "repository": True,
        "base_commit": commit,
        "base_inventory": base_inventory,
        "base_inventory_sha256": _sha256_bytes(_canonical_json(base_inventory)),
        "base_tree_sha256": _sha256_bytes(tree.encode("utf-8")),
        "working_tree_diff_sha256": _sha256_bytes(diff.encode("utf-8")),
        "dirty_tree_inventory": dirty_inventory,
        "dirty_tree_inventory_sha256": dirty_hash,
        "excluded_roots": excluded_roots,
    }


def source_snapshot(root: Path) -> dict[str, Any]:
    """Return the privacy-safe source binding used by verification and release.

    Only relative paths, sizes, hashes, and Git object metadata are returned;
    no absolute user paths or source contents are embedded in the binding.
    """

    root = Path(root).expanduser().resolve(strict=True)
    files = tuple(_iter_release_files(root))
    inventory = _file_inventory(files, root)
    git_metadata = _git_metadata(root, files)
    inventory_paths = {entry["path"] for entry in inventory}
    excluded_required = [relative for relative in _REQUIRED_FILES if relative not in inventory_paths]
    snapshot: dict[str, Any] = {
        "schema_version": _SNAPSHOT_SCHEMA_VERSION,
        "inventory": inventory,
        "inventory_sha256": _sha256_bytes(_canonical_json(inventory)),
        "excluded_required_files": excluded_required,
        "git": git_metadata,
    }
    dirty_hash = snapshot["git"].get("dirty_tree_inventory_sha256") or snapshot["inventory_sha256"]
    snapshot["working_tree_inventory_sha256"] = dirty_hash
    snapshot["git"]["working_tree_inventory_sha256"] = dirty_hash
    binding_payload = {
        "schema_version": snapshot["schema_version"],
        "inventory": snapshot["inventory"],
        "inventory_sha256": snapshot["inventory_sha256"],
        "excluded_required_files": snapshot["excluded_required_files"],
        "git": snapshot["git"],
    }
    snapshot["binding_sha256"] = _sha256_bytes(_canonical_json(binding_payload))
    return snapshot


def _assert_source_binding(root: Path, expected: Mapping[str, Any], *, phase: str) -> None:
    """Fail closed when the checkout changes during any final release phase."""

    current = source_snapshot(root)
    if dict(current) != dict(expected):
        raise ValueError(f"source changed during {phase}; release is not immutable")


def _write_member(archive: zipfile.ZipFile, source: Path, member_name: str) -> None:
    data = source.read_bytes()
    info = zipfile.ZipInfo(member_name, date_time=(2026, 8, 22, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = stat.S_IMODE(source.stat().st_mode)
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.create_system = 3
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def _normalise_test_evidence(
    evidence: Mapping[str, Any] | Path | None,
) -> Mapping[str, Any] | None:
    if evidence is None:
        return None
    if isinstance(evidence, Path):
        try:
            loaded = json.loads(evidence.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"test evidence is not valid JSON: {evidence}") from exc
        if not isinstance(loaded, dict):
            raise ValueError("test evidence JSON must contain an object")
        return loaded
    return dict(evidence)


def _evidence_logs(evidence: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = evidence.get("logs", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("test evidence logs must be a list")
    logs: list[Mapping[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ValueError("test evidence log entries must be objects")
        logs.append(entry)
    return logs


def _parse_evidence_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"test evidence is missing {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"test evidence {field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"test evidence {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _required_evidence_steps(root: Path) -> tuple[str, ...]:
    e2e = sorted(
        _normalise_relative(path.relative_to(root))
        for path in (root / "tests" / "e2e").glob("test_*.py")
        if path.is_file() and not _has_reparse_point(path)
    )
    e2e_set = frozenset(e2e)
    missing_e2e = sorted(_CORE_E2E_EVIDENCE - e2e_set)
    unexpected_e2e = sorted(
        e2e_set - _CORE_E2E_EVIDENCE - _OPTIONAL_E2E_EVIDENCE
    )
    if missing_e2e or unexpected_e2e:
        raise ValueError(
            "verification evidence E2E inventory mismatch; "
            f"missing={missing_e2e}, unexpected={unexpected_e2e}"
        )
    return (
        "unit", "integration", *e2e, "compile", "cli_help", "cli_safe_smoke",
        "audit_fresh_temp", "migration_fresh_temp", "privacy_source",
    )


def _validate_evidence_python(root: Path, value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("test evidence must identify a Python interpreter")
    raw_path = value.get("path")
    expected_hash = value.get("sha256")
    version = value.get("version")
    if (
        not isinstance(raw_path, str)
        or not raw_path.strip()
        or not isinstance(expected_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
    ):
        raise ValueError("test evidence Python interpreter metadata is incomplete")
    interpreter = Path(raw_path).expanduser()
    if not interpreter.is_absolute():
        interpreter = root / interpreter
    interpreter = interpreter.resolve(strict=False)
    if _has_reparse_point(interpreter) or not interpreter.is_file() or _sha256_file(interpreter) != expected_hash:
        raise ValueError("test evidence Python interpreter is not a real matching file")
    if interpreter.suffix.casefold() in {".cmd", ".bat", ".com", ".ps1", ".py", ".sh"}:
        raise ValueError("test evidence Python interpreter is a script shim")
    magic = interpreter.read_bytes()[:4]
    native = magic[:2] == b"MZ" if os.name == "nt" else magic.startswith(b"\x7fELF") or magic[:4] in {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}
    if not native:
        raise ValueError("test evidence Python interpreter is not a native executable")
    if not isinstance(version, str) or not version.strip() or value.get("real") is not True or value.get("implementation") != "cpython":
        raise ValueError("test evidence Python interpreter was not validated")
    executable = value.get("executable")
    if not isinstance(executable, str) or Path(executable).expanduser().resolve(strict=False) != interpreter:
        raise ValueError("test evidence Python interpreter identity does not match its path")
    probe = subprocess.run(
        [str(interpreter), "-c", "import sys; print(sys.implementation.name); print(__import__('pathlib').Path(sys.executable).resolve())"],
        cwd=root,
        env={key: value for key, value in os.environ.items() if key.upper() in {"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.splitlines()[:1] != ["cpython"] or Path(probe.stdout.splitlines()[-1]).resolve(strict=False) != interpreter:
        raise ValueError("test evidence Python interpreter failed its CPython identity probe")


def _validate_test_evidence(root: Path, evidence: Mapping[str, Any] | None, *, captured_source: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    snapshot = dict(captured_source) if captured_source is not None else source_snapshot(root)
    if evidence is None:
        raise ValueError("test evidence is required for every release")
    if not snapshot["git"].get("repository"):
        raise ValueError("test evidence requires a Git repository")
    if evidence.get("status") != "passed":
        raise ValueError("test evidence must have status=passed")
    if evidence.get("schema_version") != _EVIDENCE_SCHEMA_VERSION:
        raise ValueError(
            f"test evidence must use schema_version={_EVIDENCE_SCHEMA_VERSION}"
        )
    supplied_source = evidence.get("source")
    if not isinstance(supplied_source, Mapping) or dict(supplied_source) != snapshot:
        raise ValueError("test evidence is stale relative to the source working tree")
    if evidence.get("live_network") is not False:
        raise ValueError("test evidence must explicitly prove live_network=false")
    started = _parse_evidence_time(evidence.get("started_at"), field="started_at")
    finished = _parse_evidence_time(evidence.get("finished_at"), field="finished_at")
    now = datetime.now(timezone.utc)
    if finished <= started or (now - finished).total_seconds() > 24 * 60 * 60 or (finished - now).total_seconds() > 300:
        raise ValueError("test evidence timestamps are stale or invalid")
    _validate_evidence_python(root, evidence.get("python"))
    provenance = evidence.get("provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("schema_version") != 1
        or provenance.get("executed") is not True
        or not isinstance(provenance.get("run_nonce"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", provenance.get("run_nonce", ""))
        or not isinstance(provenance.get("plan_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", provenance.get("plan_sha256", ""))
    ):
        raise ValueError("test evidence is missing verifier provenance nonce/plan binding")
    # Reconstruct the immutable command plan from the current verifier and
    # the separately attested interpreter path.  A log cannot choose its own
    # command hash or plan digest.
    from scripts.verify import _command_hash, _plan_digest, _step_plan
    interpreter_path = str(Path(str(evidence["python"]["path"])).expanduser().resolve(strict=False))
    raw_steps_for_plan = evidence.get("steps") if isinstance(evidence.get("steps"), list) else []
    packaged_path = None
    for candidate_step in raw_steps_for_plan:
        if isinstance(candidate_step, Mapping) and candidate_step.get("name") == "packaged_smoke" and isinstance(candidate_step.get("packaged_path"), str):
            packaged_path = Path(candidate_step["packaged_path"])
            break
    expected_plan = _step_plan(root, interpreter_path, packaged_path)
    expected_plan_sha256 = _plan_digest(expected_plan)
    if provenance["plan_sha256"] != expected_plan_sha256:
        raise ValueError("test evidence provenance plan does not match the verifier command plan")
    expected_commands = {
        str(item["name"]): [_command_hash(command) for command in (item.get("commands") or [item.get("command")])]
        for item in expected_plan
    }
    required_names = _required_evidence_steps(root)
    raw_steps = evidence.get("steps")
    if not isinstance(raw_steps, list) or len(raw_steps) not in {len(required_names), len(required_names) + 1}:
        raise ValueError(f"test evidence must contain exactly {len(required_names)} required steps (plus optional packaged_smoke)")
    steps_by_name: dict[str, Mapping[str, Any]] = {}
    for step in raw_steps:
        if not isinstance(step, Mapping) or not isinstance(step.get("name"), str):
            raise ValueError("test evidence contains an invalid step")
        name = step["name"]
        if name in steps_by_name or (name not in required_names and name != "packaged_smoke"):
            raise ValueError("test evidence step set does not match the required verification plan")
        if step.get("status") != "passed" or step.get("exit_code") != 0:
            raise ValueError(f"test evidence step did not pass: {name}")
        steps_by_name[name] = step
    step_names = set(steps_by_name)
    if step_names != set(required_names) and step_names != set(required_names) | {"packaged_smoke"}:
        raise ValueError("test evidence step set does not match the required verification plan")
    logs = _evidence_logs(evidence)
    if len(logs) != len(steps_by_name):
        raise ValueError("test evidence must contain one raw log per required step")
    evidence_root = (root / "release-evidence").resolve()
    if not evidence_root.is_dir():
        raise ValueError("test evidence release-evidence directory is missing")
    log_by_path: dict[str, Mapping[str, Any]] = {}
    log_text_by_path: dict[str, str] = {}
    for entry in logs:
        raw_path = entry.get("path")
        expected_hash = entry.get("sha256")
        expected_size = entry.get("size")
        if not isinstance(raw_path, str) or not raw_path.strip() or not isinstance(expected_size, int) or expected_size < 0:
            raise ValueError("test evidence log is missing a path")
        normal = _normalise_relative(raw_path)
        log_path = (root / normal).resolve(strict=False)
        try:
            log_path.relative_to(evidence_root)
        except ValueError as exc:
            raise ValueError("test evidence log is outside release-evidence") from exc
        _assert_contained(log_path, root)
        if _has_reparse_point(log_path) or not log_path.is_file():
            raise ValueError(f"test evidence log is missing: {raw_path}")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or _sha256_file(log_path) != expected_hash or log_path.stat().st_size != expected_size:
            raise ValueError(f"test evidence log hash is stale: {raw_path}")
        if normal in log_by_path:
            raise ValueError("test evidence contains duplicate raw logs")
        log_by_path[normal] = entry
        try:
            log_text_by_path[normal] = log_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("test evidence raw logs must be UTF-8 provenance logs") from exc
    for name, step in steps_by_name.items():
        step_log = step.get("log")
        if not isinstance(step_log, Mapping) or step_log.get("path") not in log_by_path:
            raise ValueError(f"test evidence step is missing its contained raw log: {name}")
        if dict(step_log) != dict(log_by_path[step_log["path"]]):
            raise ValueError(f"test evidence step log does not match logs list: {name}")
        if step.get("provenance_nonce") != provenance["run_nonce"] or step.get("plan_sha256") != provenance["plan_sha256"]:
            raise ValueError(f"test evidence step provenance does not match the run: {name}")
        command_hashes = step.get("command_hashes")
        if not isinstance(command_hashes, list) or not command_hashes or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in command_hashes):
            raise ValueError(f"test evidence step is missing command provenance: {name}")
        if name in expected_commands and command_hashes != expected_commands[name]:
            raise ValueError(f"test evidence step command provenance is not the required command: {name}")
        text = log_text_by_path[step_log["path"]]
        required_lines = {
            f"provenance_nonce={provenance['run_nonce']}",
            f"plan_sha256={provenance['plan_sha256']}",
            f"step={name}",
            "exit_code=0",
            f"command_count={len(command_hashes)}",
        }
        if not required_lines.issubset(set(text.splitlines())):
            raise ValueError(f"test evidence raw log lacks verifier provenance: {name}")
        actual_hashes = [line.split("=", 1)[1] for line in text.splitlines() if line.startswith("command_sha256=")]
        if actual_hashes != command_hashes:
            raise ValueError(f"test evidence raw log command provenance mismatch: {name}")
    return evidence


def _safe_evidence_for_manifest(value: Any, *, key: str = "") -> Any:
    """Copy only the fixed evidence contract; arbitrary values never ship."""

    if not isinstance(value, Mapping):
        return None
    allowed_top = {"schema_version", "status", "started_at", "finished_at", "live_network", "python", "source", "steps", "logs", "provenance"}
    result: dict[str, Any] = {}
    for item_key in allowed_top:
        item = value.get(item_key)
        if item_key in {"schema_version", "status", "started_at", "finished_at", "live_network"}:
            if isinstance(item, (str, int, bool)):
                result[item_key] = item
        elif item_key == "python" and isinstance(item, Mapping):
            result[item_key] = {
                "path": Path(str(item.get("path", ""))).name,
                "sha256": item.get("sha256"),
                "version": item.get("version"),
                "implementation": item.get("implementation"),
                "executable": Path(str(item.get("executable", ""))).name,
                "real": item.get("real") is True,
            }
        elif item_key == "provenance" and isinstance(item, Mapping):
            result[item_key] = {
                "schema_version": item.get("schema_version"),
                "run_nonce": item.get("run_nonce"),
                "plan_sha256": item.get("plan_sha256"),
                "executed": item.get("executed") is True,
            }
        elif item_key == "source" and isinstance(item, Mapping):
            # Source paths are relative and hashes are safe; retain only the
            # binding and inventory hashes, never arbitrary source metadata.
            result[item_key] = {
                "schema_version": item.get("schema_version"),
                "inventory_sha256": item.get("inventory_sha256"),
                "binding_sha256": item.get("binding_sha256"),
                "working_tree_inventory_sha256": item.get("working_tree_inventory_sha256"),
                "excluded_required_files": item.get("excluded_required_files", []),
                "git": {
                    "repository": item.get("git", {}).get("repository") if isinstance(item.get("git"), Mapping) else False,
                    "base_commit": item.get("git", {}).get("base_commit") if isinstance(item.get("git"), Mapping) else None,
                    "dirty_tree_inventory_sha256": item.get("git", {}).get("dirty_tree_inventory_sha256") if isinstance(item.get("git"), Mapping) else None,
                    "excluded_roots": item.get("git", {}).get("excluded_roots", []) if isinstance(item.get("git"), Mapping) else [],
                },
            }
        elif item_key in {"steps", "logs"} and isinstance(item, list):
            cleaned: list[dict[str, Any]] = []
            for entry in item:
                if not isinstance(entry, Mapping):
                    continue
                safe: dict[str, Any] = {}
                for field in ("name", "status", "exit_code", "path", "sha256", "size", "provenance_nonce", "plan_sha256", "command_hashes"):
                    field_value = entry.get(field)
                    if field == "path" and isinstance(field_value, str):
                        safe[field] = Path(field_value).name
                    elif isinstance(field_value, (str, int, list)) and field_value is not None:
                        safe[field] = field_value
                cleaned.append(safe)
            result[item_key] = cleaned
    return result


def _executable_metadata(
    executable: Path | None,
    *,
    version: str | None,
    signature: Path | None,
    signature_public_key: Path | None = None,
    allow_unsigned: bool = False,
) -> dict[str, Any] | None:
    if executable is None:
        if version is not None or signature is not None or signature_public_key is not None:
            raise ValueError("executable version/signature requires --executable")
        return None
    executable = executable.expanduser().resolve()
    if not executable.is_file():
        raise ValueError(f"executable does not exist: {executable}")
    if version is not None and not version.strip():
        raise ValueError("executable version cannot be blank")
    actual_version = _inspect_pe_version(executable)
    if executable.suffix.casefold() == ".exe" and actual_version is None:
        raise ValueError("executable is not a readable PE with version resources")
    if version is not None and actual_version != version:
        raise ValueError("supplied executable version does not match its PE version resource")
    metadata: dict[str, Any] = {
        "path": executable.name,
        "sha256": _sha256_file(executable),
        "size": executable.stat().st_size,
        "version": actual_version,
        "version_source": "PE_FIXEDFILEINFO" if actual_version else None,
        "signature": None,
    }
    if signature is not None:
        signature = signature.expanduser().resolve()
        if not signature.is_file():
            raise ValueError(f"executable signature does not exist: {signature}")
        if executable.suffix.casefold() == ".exe" and signature.suffix.casefold() != ".sig":
            _verify_authenticode(executable)
        else:
            if signature_public_key is None:
                raise ValueError("detached executable signatures require a public key")
            _verify_detached_signature(executable, signature, signature_public_key)
        metadata["signature"] = {
            "path": signature.name,
            "sha256": _sha256_file(signature),
            "size": signature.stat().st_size,
            "verified": True,
        }
    elif signature_public_key is not None:
        raise ValueError("detached signature public key requires an executable signature")
    elif executable.suffix.casefold() == ".exe":
        if not allow_unsigned:
            raise ValueError("executable is unsigned; pass --allow-unsigned-executable to acknowledge it explicitly")
        metadata["signature"] = {
            "state": "unsigned",
            "verified": False,
            "acknowledged": True,
        }
    return metadata


def _inspect_pe_version(executable: Path) -> str | None:
    """Read the actual Windows PE ProductVersion resource."""

    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version", use_last_error=True)
    size = version.GetFileVersionInfoSizeW(wintypes.LPCWSTR(str(executable)), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(wintypes.LPCWSTR(str(executable)), 0, size, buffer):
        return None
    class FixedInfo(ctypes.Structure):
        _fields_ = [
            ("signature", wintypes.DWORD), ("struct_version", wintypes.DWORD),
            ("file_version_ms", wintypes.DWORD), ("file_version_ls", wintypes.DWORD),
            ("product_version_ms", wintypes.DWORD), ("product_version_ls", wintypes.DWORD),
        ]
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(buffer, wintypes.LPCWSTR("\\"), ctypes.byref(pointer), ctypes.byref(length)) or not pointer.value:
        return None
    fixed = ctypes.cast(pointer, ctypes.POINTER(FixedInfo)).contents
    if fixed.signature != 0xFEEF04BD:
        return None
    ms, ls = fixed.product_version_ms, fixed.product_version_ls
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _verify_authenticode(executable: Path) -> None:
    try:
        result = subprocess.run(["signtool", "verify", "/pa", "/all", str(executable)], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise ValueError("cannot verify Authenticode: signtool is unavailable") from exc
    if result.returncode != 0:
        raise ValueError("Authenticode signature verification failed")


def _verify_detached_signature(executable: Path, signature: Path, public_key: Path) -> None:
    if not public_key.is_file() or _has_reparse_point(public_key):
        raise ValueError("detached signature public key is missing or unsafe")
    try:
        result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public_key), "-signature", str(signature), str(executable)], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise ValueError("cannot verify detached signature: openssl is unavailable") from exc
    if result.returncode != 0 or "verified ok" not in (result.stdout or "").casefold():
        raise ValueError("detached executable signature verification failed")


def _validate_version_alignment(root: Path, version: str) -> None:
    """Reject an archive whose checked-in version metadata disagrees."""

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        import tomllib

        try:
            with pyproject.open("rb") as handle:
                configured = tomllib.load(handle).get("project", {}).get("version")
        except tomllib.TOMLDecodeError:
            # Tiny non-Git fixture repositories used by the compatibility API
            # tests often contain a placeholder file.  A real checkout is
            # still fail-closed below because its evidence gate runs first.
            if (root / ".git").exists():
                raise ValueError("pyproject.toml is not valid TOML")
            configured = None
        if isinstance(configured, str) and configured.strip() and configured != version:
            raise ValueError(
                f"version mismatch: pyproject.toml declares {configured}, requested {version}"
            )
    runtime_version = root / "app" / "version.py"
    if runtime_version.is_file():
        text = runtime_version.read_text(encoding="utf-8")
        match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", text)
        if match and match.group(1) != version:
            raise ValueError(
                f"version mismatch: app/version.py declares {match.group(1)}, requested {version}"
            )
    resource = root / "packaging" / "version_info.txt"
    if resource.is_file():
        text = resource.read_text(encoding="utf-8")
        match = re.search(r'StringStruct\("ProductVersion",\s*"([^"]+)"\)', text)
        if match and match.group(1) != version:
            raise ValueError(
                "version mismatch: packaging/version_info.txt declares "
                f"{match.group(1)}, requested {version}"
            )


def _release_manifest(
    *,
    archive: Path,
    archive_sha256: str,
    version: str,
    files: Sequence[Path],
    root: Path,
    source: Mapping[str, Any],
    test_evidence: Mapping[str, Any],
    executable: Mapping[str, Any] | None,
    signing_requested: bool,
    signing_certificate: Path | None,
    archive_verified_twice: bool,
    archive_reproducible: bool,
) -> dict[str, Any]:
    members = _file_inventory(files, root)
    certificate_sha256 = None
    if signing_requested and signing_certificate is not None:
        certificate_sha256 = _sha256_file(signing_certificate)
    return {
        "schema_version": 2,
        "name": "argus-applications",
        "version": version,
        "archive": archive.name,
        "archive_sha256": archive_sha256,
        "archive_verified_twice": archive_verified_twice,
        "archive_reproducible": archive_reproducible,
        "files": members,
        "source": _safe_source_for_manifest(source),
        "source_binding_sha256": source["binding_sha256"],
        "test_evidence": _safe_evidence_for_manifest(dict(test_evidence)),
        "executable": executable,
        "signing": {
            "requested": signing_requested,
            "certificate_sha256": certificate_sha256,
        },
    }


def _safe_source_for_manifest(source: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve source hashes while redacting sensitive dirty-tree names."""

    safe = {
        "schema_version": source.get("schema_version"),
        "inventory_sha256": source.get("inventory_sha256"),
        "working_tree_inventory_sha256": source.get("working_tree_inventory_sha256"),
        "binding_sha256": source.get("binding_sha256"),
        "excluded_required_files": list(source.get("excluded_required_files", [])),
        "inventory": [],
        "git": dict(source.get("git", {})) if isinstance(source.get("git"), Mapping) else {},
    }
    for entry in source.get("inventory", []):
        if not isinstance(entry, Mapping):
            continue
        path = str(entry.get("path", ""))
        safe_path = path if not any(token in path.casefold() for token in ("candidate", "resume", "profile", "cookie", "backup", "secret", "token")) else f"<redacted:{_sha256_bytes(path.encode('utf-8'))[:16]}>"
        safe["inventory"].append({"path": safe_path, "sha256": entry.get("sha256"), "size": entry.get("size")})
    git = safe["git"]
    if isinstance(git, dict) and isinstance(git.get("dirty_tree_inventory"), list):
        cleaned: list[dict[str, Any]] = []
        for entry in git["dirty_tree_inventory"]:
            if not isinstance(entry, Mapping):
                continue
            path = str(entry.get("path", ""))
            clean_path = path if not any(token in path.casefold() for token in ("candidate", "resume", "profile", "cookie", "backup", "secret", "token")) else f"<redacted:{_sha256_bytes(path.encode('utf-8'))[:16]}>"
            cleaned.append({"path": clean_path, "sha256": entry.get("sha256"), "size": entry.get("size"), "release_member": entry.get("release_member")})
        git["dirty_tree_inventory"] = cleaned
    return safe


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    temporary = Path(temporary_handle.name)
    try:
        with temporary_handle:
            temporary_handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            temporary_handle.flush()
            os.fsync(temporary_handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_archive_members(archive_path: Path) -> None:
    if archive_path.stat().st_size > _ARCHIVE_MAX_BYTES:
        raise ValueError("release archive exceeds the compressed size cap")
    seen: set[str] = set()
    file_keys: set[str] = set()
    total = 0
    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = archive.infolist()
        if len(infos) > _ARCHIVE_MAX_MEMBERS:
            raise ValueError("release archive contains too many members")
        for info in infos:
            raw = info.filename.replace("\\", "/")
            normal = unicodedata.normalize("NFKC", raw)
            parts = normal.rstrip("/").split("/")
            if (not normal or normal != raw or any(ord(ch) > 0x7F for ch in normal)
                    or normal.startswith("/") or re.match(r"^[A-Za-z]:", normal)
                    or any(part in {"", ".", ".."} for part in parts)):
                raise ValueError("release archive contains an unsafe member path")
            key = normal.casefold().rstrip("/")
            if key in seen:
                raise ValueError("release archive contains duplicate members")
            seen.add(key)
            if not normal.endswith("/"):
                if any(key.startswith(parent + "/") for parent in file_keys):
                    raise ValueError("release archive contains a file/directory member collision")
                file_keys.add(key)
            elif any(existing.startswith(key + "/") for existing in file_keys):
                raise ValueError("release archive contains a file/directory member collision")
            if info.file_size > _ARCHIVE_MAX_MEMBER_BYTES:
                raise ValueError("release archive member exceeds the uncompressed size cap")
            total += info.file_size
            if total > _ARCHIVE_MAX_BYTES:
                raise ValueError("release archive exceeds the uncompressed size cap")


def _verify_archive(
    archive_path: Path,
    *,
    files: Sequence[Path],
    root: Path,
    prefix: str,
) -> str:
    _validate_archive_members(archive_path)
    expected: dict[str, tuple[str, int]] = {}
    for source in files:
        relative = source.relative_to(root).as_posix()
        _normalise_relative(relative)
        expected[f"{prefix}/{relative}"] = (_sha256_file(source), source.stat().st_size)
    with zipfile.ZipFile(archive_path, "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("release archive contains duplicate members")
        if set(names) != set(expected):
            missing = sorted(set(expected) - set(names))
            unexpected = sorted(set(names) - set(expected))
            raise ValueError(
                "release archive contents changed: "
                f"missing={missing[:3]!r}, unexpected={unexpected[:3]!r}"
            )
        for name in names:
            info = archive.getinfo(name)
            if name.endswith("/") or info.is_dir():
                raise ValueError(f"release archive contains a directory member: {name}")
            _normalise_relative(name)
            payload = archive.read(name)
            expected_hash, expected_size = expected[name]
            if len(payload) != expected_size or _sha256_bytes(payload) != expected_hash:
                raise ValueError(f"release archive member hash mismatch: {name}")
    return _sha256_file(archive_path)


def _decode_archive_text(raw: bytes) -> str:
    encodings: tuple[str, ...]
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        encodings = ("utf-16", "utf-8-sig")
    elif len(raw) >= 8 and (raw[1::2].count(0) >= len(raw[1::2]) // 2 or raw[0::2].count(0) >= len(raw[0::2]) // 2):
        encodings = ("utf-16-le", "utf-16-be", "utf-8-sig")
    else:
        encodings = ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Binary members are scanned only through printable ASCII/UTF-16 runs;
    # arbitrary bytes are never rendered into findings or logs.
    chunks: list[str] = []
    for match in re.finditer(rb"[\x20-\x7e]{4,}", raw):
        chunks.append(match.group(0).decode("ascii"))
    for match in re.finditer(rb"(?:[\x20-\x7e]\x00){4,}", raw):
        chunks.append(match.group(0).decode("utf-16-le", errors="ignore"))
    return "\n".join(chunks)


def _scan_archive_all_members(root: Path, archive: Path) -> None:
    config_path = root / "packaging" / "privacy_scan_config.json"
    scanner_path = root / "scripts" / "privacy_scan.py"
    if not config_path.is_file() or not scanner_path.is_file():
        raise ValueError("privacy scanner and policy config are required")
    from scripts.privacy_scan import _is_excluded, _scan_text, load_config

    _validate_archive_members(archive)
    config = load_config(config_path)
    findings: list[Any] = []
    with zipfile.ZipFile(archive, "r") as handle:
        for name in sorted(handle.namelist(), key=str.casefold):
            _normalise_relative(name)
            lowered = name.casefold()
            relative_name = name.split("/", 1)[-1]
            if _is_excluded(relative_name, config):
                continue
            basename = Path(name).name.casefold()
            private_suffix = Path(name).suffix.casefold() in {".json", ".yaml", ".yml", ".csv", ".pdf", ".doc", ".docx", ".txt"}
            if any(token in lowered for token in ("cookie", "keylog", "sslkeylog", "candidate", "resume", "screenshot", "old-release", "release-candidate", "backup")) or ("profile" in basename and private_suffix):
                raise ValueError(f"privacy scan rejected archive member name: {Path(name).name}")
            if name.endswith("/"):
                raise ValueError("release archive contains directory member")
            text = _decode_archive_text(handle.read(name))
            if Path(name).suffix.casefold() in {".md", ".json", ".txt", ".yaml", ".yml", ".ini", ".cfg", ".csv", ".env"} and relative_name.casefold() not in {"scripts/package_release.py", "scripts/verify.py", "scripts/privacy_scan.py", "packaging/privacy_scan_config.json"}:
                lowered_text = text.casefold()
                if any(re.search(rf"{re.escape(marker)}\s*[:=]", lowered_text) for marker in _FORBIDDEN_CONTENT_MARKERS):
                    raise ValueError(f"privacy scan rejected forbidden content in archive member: {basename}")
            if text:
                findings.extend(_scan_text(text, label=name, config=config))
    if findings:
        labels = ", ".join(f"{finding.path}:{finding.category}" for finding in findings[:8])
        raise ValueError(f"privacy scan found {len(findings)} release finding(s): {labels}")


def _scan_privacy(root: Path, archive: Path) -> None:
    _scan_archive_all_members(root, archive)


def _create_archive(path: Path, files: Sequence[Path], *, root: Path, prefix: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for source_file in files:
                relative = _normalise_relative(source_file.relative_to(root))
                _write_member(archive, source_file, f"{prefix}/{relative}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sign_manifest(
    *,
    manifest: Path,
    output_dir: Path,
    certificate: Path | None,
    key: Path | None,
) -> Path:
    if certificate is None:
        raise ValueError("signing requested but no signing certificate was provided")
    if key is None:
        raise ValueError("signing requested but no signing key was provided")
    if not certificate.is_file():
        raise ValueError(f"signing certificate does not exist: {certificate}")
    if not key.is_file():
        raise ValueError(f"signing key does not exist: {key}")
    signature = output_dir / f"{manifest.stem}.sig"
    try:
        result = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-sign",
                str(key),
                "-out",
                str(signature),
                str(manifest),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError("signing requested but openssl is unavailable") from exc
    if result.returncode != 0 or not signature.is_file():
        detail = (result.stderr or result.stdout or "openssl signing failed").strip()
        signature.unlink(missing_ok=True)
        raise RuntimeError(f"signing requested but signing failed: {detail}")
    return signature


def build_release(
    *,
    root: Path,
    output_dir: Path,
    version: str,
    test_evidence: Mapping[str, Any] | Path | None = None,
    verification_capability: object | None = None,
    signing_requested: bool = False,
    signing_certificate: Path | None = None,
    signing_key: Path | None = None,
    executable: Path | None = None,
    executable_version: str | None = None,
    executable_signature: Path | None = None,
    executable_signature_public_key: Path | None = None,
    allow_unsigned_executable: bool = False,
) -> ReleaseResult:
    raw_root = root.expanduser()
    if _has_reparse_point(raw_root):
        raise ValueError("release root must not be a link or reparse point")
    root = raw_root.resolve(strict=True)
    if not version.strip() or any(character in version for character in "/\\\0"):
        raise ValueError("version must be a non-empty filename-safe value")
    if signing_certificate is not None or signing_key is not None:
        signing_requested = True
    if signing_requested and signing_certificate is None:
        raise ValueError("signing requested but no signing certificate was provided")
    if signing_requested and signing_key is None:
        raise ValueError("signing requested but no signing key was provided")
    if signing_requested and signing_certificate is not None and not signing_certificate.is_file():
        raise ValueError(f"signing certificate does not exist: {signing_certificate}")
    if signing_requested and signing_key is not None and not signing_key.is_file():
        raise ValueError(f"signing key does not exist: {signing_key}")
    _validate_required_files(root)

    output_dir = _validate_release_output_dir(output_dir, root)

    files = tuple(
        sorted(
            dict.fromkeys(_iter_release_files(root)),
            key=lambda item: item.relative_to(root).as_posix(),
        )
    )
    if not files:
        raise ValueError("release contains no files")
    captured_source = source_snapshot(root)
    evidence = _normalise_test_evidence(test_evidence)
    if evidence is None:
        raise ValueError("test evidence is required for every release")
    _validate_release_capability(verification_capability, evidence, root=root)
    evidence = _validate_test_evidence(root, evidence, captured_source=captured_source)
    source = source_snapshot(root)
    if source != captured_source:
        raise ValueError("source changed while validating test evidence; release is not immutable")
    _validate_release_capability(verification_capability, evidence, root=root)
    if source.get("excluded_required_files"):
        raise ValueError("required release files were excluded by privacy policy: " + ", ".join(source["excluded_required_files"]))
    _validate_version_alignment(root, version)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Re-check after directory creation to close the symlink/junction race at
    # the output boundary.
    output_dir = _validate_release_output_dir(output_dir, root)
    archive_path = output_dir / f"argus-{version}.zip"
    checksum_path = output_dir / f"argus-{version}.zip.sha256"
    manifest_path = output_dir / f"argus-{version}.manifest.json"
    prefix = f"ARGUS-{version}"
    signature_path: Path | None = None
    try:
        with _frozen_release_stage(root=root, files=files, expected_source=source) as (stage_root, staged_files):
            # Executable metadata is derived only from immutable staged bytes.
            # This closes the source-file mutation window between metadata
            # inspection and manifest publication.
            staged_executable = (
                _freeze_external_input(executable, stage_root=stage_root, label="executable")
                if executable is not None
                else None
            )
            staged_signature = (
                _freeze_external_input(executable_signature, stage_root=stage_root, label="executable-signature")
                if executable_signature is not None
                else None
            )
            staged_public_key = (
                _freeze_external_input(executable_signature_public_key, stage_root=stage_root, label="executable-public-key")
                if executable_signature_public_key is not None
                else None
            )
            executable_metadata = _executable_metadata(
                staged_executable,
                version=executable_version,
                signature=staged_signature,
                signature_public_key=staged_public_key,
                allow_unsigned=allow_unsigned_executable,
            )
            _create_archive(archive_path, staged_files, root=stage_root, prefix=prefix)
            first_digest = _verify_archive(archive_path, files=staged_files, root=stage_root, prefix=prefix)
            with tempfile.TemporaryDirectory(prefix="argus-independent-rebuild-") as rebuild_dir:
                rebuild_path = Path(rebuild_dir) / archive_path.name
                # Both archives are deliberately built from the same sealed
                # stage; no second read of the mutable checkout is involved.
                _create_archive(rebuild_path, staged_files, root=stage_root, prefix=prefix)
                second_digest = _verify_archive(rebuild_path, files=staged_files, root=stage_root, prefix=prefix)
                if archive_path.read_bytes() != rebuild_path.read_bytes() or first_digest != second_digest:
                    raise ValueError("independent release rebuild is not byte-for-byte reproducible")
            digest = _verify_archive(
                archive_path,
                files=staged_files,
                root=stage_root,
                prefix=prefix,
            )
            # The checkout may be edited by another process while the ZIP is
            # being written. The archive is still sourced from the sealed
            # stage, but fail closed rather than publishing a manifest whose
            # source binding no longer describes the checkout at completion.
            _assert_source_binding(root, source, phase="release archive creation")
            checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
            _assert_source_binding(root, source, phase="release checksum publication")
            # The privacy policy and scanner themselves are staged inputs too;
            # do not let a checkout mutation alter what the archive gate saw.
            _scan_privacy(stage_root, archive_path)
            _assert_source_binding(root, source, phase="release privacy scan")
            _write_manifest(
                manifest_path,
                _release_manifest(
                    archive=archive_path,
                    archive_sha256=digest,
                    version=version,
                    files=staged_files,
                    root=stage_root,
                    source=source,
                    test_evidence=evidence,
                    executable=executable_metadata,
                    signing_requested=signing_requested,
                    signing_certificate=signing_certificate,
                    archive_verified_twice=True,
                    archive_reproducible=True,
                ),
            )
            if signing_requested:
                signature_path = _sign_manifest(
                    manifest=manifest_path,
                    output_dir=output_dir,
                    certificate=signing_certificate,
                    key=signing_key,
                )
                _assert_source_binding(root, source, phase="release signature publication")
            _assert_source_binding(root, source, phase="release manifest publication")
            return ReleaseResult(
                archive=archive_path,
                checksum_file=checksum_path,
                sha256=digest,
                file_count=len(staged_files),
                manifest_file=manifest_path,
                signature_file=signature_path,
                archive_verified_twice=True,
            )
    except Exception:
        # A failed privacy or evidence gate must not leave a misleading partial
        # release beside a previous artifact.
        for path in (archive_path, checksum_path, manifest_path, signature_path):
            if path is not None:
                path.unlink(missing_ok=True)
        raise


def _read_version(root: Path) -> str:
    import tomllib

    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle).get("project", {})
    version = project.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("pyproject.toml does not contain project.version")
    return version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a verified, secret-free ARGUS release archive"
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--version")
    parser.add_argument(
        "--test-evidence",
        type=Path,
        required=False,
        help="Fresh verification JSON; required for a Git working tree",
    )
    parser.add_argument("--executable", "--exe", dest="executable", type=Path)
    parser.add_argument("--executable-version", dest="executable_version")
    parser.add_argument(
        "--executable-signature",
        "--signature",
        dest="executable_signature",
        type=Path,
    )
    parser.add_argument("--executable-signature-public-key", type=Path)
    parser.add_argument(
        "--allow-unsigned-executable",
        action="store_true",
        help="Explicitly acknowledge that a supplied PE executable is unsigned (not trusted)",
    )
    parser.add_argument(
        "--sign",
        action="store_true",
        help="Create a detached manifest signature (requires certificate and key)",
    )
    parser.add_argument("--signing-certificate", type=Path)
    parser.add_argument("--signing-key", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    version = args.version or _read_version(root)
    output = args.output
    if not output.is_absolute():
        output = root / output
    evidence = args.test_evidence
    if evidence is not None and not evidence.is_absolute():
        evidence = root / evidence
    executable = args.executable
    if executable is not None and not executable.is_absolute():
        executable = root / executable
    executable_signature = args.executable_signature
    if executable_signature is not None and not executable_signature.is_absolute():
        executable_signature = root / executable_signature
    executable_signature_public_key = args.executable_signature_public_key
    if executable_signature_public_key is not None and not executable_signature_public_key.is_absolute():
        executable_signature_public_key = root / executable_signature_public_key
    result = build_release(
        root=root,
        output_dir=output,
        version=version,
        test_evidence=evidence,
        signing_requested=args.sign,
        signing_certificate=args.signing_certificate,
        signing_key=args.signing_key,
        executable=executable,
        executable_version=args.executable_version,
        executable_signature=executable_signature,
        executable_signature_public_key=executable_signature_public_key,
        allow_unsigned_executable=args.allow_unsigned_executable,
    )
    print(f"Built {result.archive} ({result.file_count} files)")
    print(f"SHA256 {result.sha256}")
    print(f"Manifest {result.manifest_file}")
    if result.signature_file:
        print(f"Signature {result.signature_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
