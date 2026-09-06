"""Cross-platform process lock for the local ARGUS runtime.

The launcher must fail closed when another ARGUS process already owns the
runtime.  A lock file is deliberately retained after release so that its
metadata is useful for diagnostics; the operating-system file lock, rather
than file existence, is the authority.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_LOCAL_GUARD = threading.Lock()
_HELD_LOCK_PATHS: set[Path] = set()


class RuntimeLockError(RuntimeError):
    """Base error for runtime ownership failures."""


class RuntimeAlreadyRunning(RuntimeLockError):
    """Raised when another process currently owns the ARGUS runtime lock."""

    def __init__(self, path: Path, metadata: Mapping[str, Any] | None = None) -> None:
        self.path = path
        self.metadata = dict(metadata or {})
        owner = self.metadata.get("pid")
        port = self.metadata.get("port")
        description = f" (pid {owner}, port {port})" if owner or port else ""
        super().__init__(f"ARGUS is already running{description}; lock: {path}")


def runtime_lock_path(data_dir: Path) -> Path:
    """Return the stable lock path for one ARGUS data directory."""

    return Path(data_dir) / "argus.runtime.lock"


def _lock_file(handle) -> None:  # noqa: ANN001 - platform file object
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:  # noqa: ANN001 - platform file object
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            # The operating system releases a Windows lock on close.  A
            # failed explicit unlock must not hide the original shutdown.
            pass
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_metadata(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class RuntimeLock:
    """Hold an OS-level exclusive lock for the lifetime of the process."""

    def __init__(
        self,
        path: Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8787,
        version: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.host = host
        self.port = int(port)
        self.version = version
        self.metadata = dict(metadata or {})
        self._handle = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> "RuntimeLock":
        if self.acquired:
            return self
        # Windows byte-range locks are per-handle: two handles in one process
        # can both "acquire".  A process-local registry closes that gap the
        # same way SweepLock does for sweep locks.
        with _LOCAL_GUARD:
            if self.path in _HELD_LOCK_PATHS:
                raise RuntimeAlreadyRunning(self.path, _read_metadata(self.path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Keep at least one byte in the file: msvcrt.locking requires a
        # non-empty region, while fcntl works with either empty or non-empty.
        handle = self.path.open("a+", encoding="utf-8")
        try:
            if self.path.stat().st_size == 0:
                handle.write(" ")
                handle.flush()
                os.fsync(handle.fileno())
            _lock_file(handle)
        except OSError as exc:
            handle.close()
            raise RuntimeAlreadyRunning(self.path, _read_metadata(self.path)) from exc

        payload: dict[str, Any] = {
            "pid": os.getpid(),
            "host": self.host,
            "port": self.port,
            "version": self.version,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        payload.update(self.metadata)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        handle.flush()
        os.fsync(handle.fileno())
        with _LOCAL_GUARD:
            _HELD_LOCK_PATHS.add(self.path)
        self._handle = handle
        return self

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            _unlock_file(handle)
        finally:
            with _LOCAL_GUARD:
                _HELD_LOCK_PATHS.discard(self.path)
            handle.close()

    def __enter__(self) -> "RuntimeLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:  # noqa: ANN001
        self.release()


__all__ = [
    "RuntimeAlreadyRunning",
    "RuntimeLock",
    "RuntimeLockError",
    "runtime_lock_path",
]
