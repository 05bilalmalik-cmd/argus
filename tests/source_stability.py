"""Fail-closed source-tree snapshots for concurrent pytest containment."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping


_SOURCE_ROOTS = ("app", "scripts", "tests")


SourceFingerprint = tuple[str, int, int]


def _stable_fingerprint(path: Path) -> SourceFingerprint:
    """Hash one file only when size/mtime remain stable around the read."""

    for _attempt in range(3):
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
        if (
            before.st_size == after.st_size == len(content)
            and before.st_mtime_ns == after.st_mtime_ns
        ):
            return (
                hashlib.sha256(content).hexdigest(),
                after.st_size,
                after.st_mtime_ns,
            )
    raise RuntimeError(
        f"Python source changed while the test baseline was being captured: {path.name}"
    )


def snapshot_source_tree(root: Path) -> dict[str, SourceFingerprint]:
    """Return relative POSIX paths and hashes for all product/test Python files."""

    resolved = Path(root).resolve()
    snapshot: dict[str, SourceFingerprint] = {}
    for directory_name in _SOURCE_ROOTS:
        directory = resolved / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts or not path.is_file():
                continue
            relative = path.relative_to(resolved).as_posix()
            snapshot[relative] = _stable_fingerprint(path)
    return snapshot


def diff_source_snapshots(
    before: Mapping[str, SourceFingerprint],
    after: Mapping[str, SourceFingerprint],
) -> dict[str, tuple[str, ...]]:
    """Classify every added, deleted or byte-modified Python source path."""

    before_paths = set(before)
    after_paths = set(after)
    return {
        "added": tuple(sorted(after_paths - before_paths)),
        "deleted": tuple(sorted(before_paths - after_paths)),
        "modified": tuple(
            sorted(
                path
                for path in before_paths & after_paths
                if before[path] != after[path]
            )
        ),
    }


def format_source_drift(changes: Mapping[str, tuple[str, ...]]) -> str:
    """Produce one environment-neutral failure description."""

    details = [
        f"{kind}=" + ",".join(paths)
        for kind in ("added", "deleted", "modified")
        if (paths := tuple(changes.get(kind, ())))
    ]
    return "; ".join(details)
