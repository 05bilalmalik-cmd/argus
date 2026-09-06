"""A small cross-process lock for the scheduled scouting sweep."""
from __future__ import annotations

import os
import threading
from pathlib import Path

_LOCAL_GUARD = threading.Lock()
_HELD_PATHS: set[Path] = set()


class SweepLock:
    """Acquire an advisory lock backed by a file descriptor.

    The descriptor stays open for the entire sweep, which makes the lock
    release automatic when a worker process exits.  A process-local set fills
    the small Windows gap where two handles owned by the same process can
    otherwise both acquire the CRT byte-range lock.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None
        self._resolved: Path | None = None

    def acquire(self) -> bool:
        resolved = self.path.resolve()
        with _LOCAL_GUARD:
            if resolved in _HELD_PATHS:
                return False
        resolved.parent.mkdir(parents=True, exist_ok=True)
        handle = None
        try:
            handle = resolved.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError):
            try:
                if handle is not None:
                    handle.close()
            except (AttributeError, OSError):
                pass
            return False
        with _LOCAL_GUARD:
            _HELD_PATHS.add(resolved)
        self._handle = handle
        self._resolved = resolved
        return True

    def release(self) -> None:
        handle = self._handle
        resolved = self._resolved
        if handle is None or resolved is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        finally:
            try:
                handle.close()
            except OSError:
                pass
            with _LOCAL_GUARD:
                _HELD_PATHS.discard(resolved)
            self._handle = None
            self._resolved = None

    def __enter__(self) -> "SweepLock":
        if not self.acquire():
            raise RuntimeError(f"Sweep lock is already held: {self.path}")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self.release()
