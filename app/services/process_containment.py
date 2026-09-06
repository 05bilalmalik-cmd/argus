"""Exact, row-owned subprocess containment for live browser resolution.

The parent process never searches for browsers by name.  It owns one explicit
root PID and, on timeout, terminates only that PID's operating-system process
tree.  This is the boundary that a Python thread cannot provide for a native
Playwright call that never returns.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ProcessReapResult:
    forced: bool
    verified_empty: bool
    returncode: int | None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            WAIT_TIMEOUT = 0x00000102
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class OwnedProcessTree:
    """One exact child root plus every descendant created beneath it."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._lines: queue.Queue[str | BaseException | None] = queue.Queue()
        self._tracked_pids: set[int] = {process.pid}
        self._stdout_reader = threading.Thread(
            target=self._read_stdout,
            name=f"argus-contained-stdout-{process.pid}",
            daemon=True,
        )
        self._stdout_reader.start()
        self._stderr_tail: list[str] = []
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            name=f"argus-contained-stderr-{process.pid}",
            daemon=True,
        )
        self._stderr_reader.start()

    @classmethod
    def spawn(
        cls,
        command: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        env: Mapping[str, str],
    ) -> "OwnedProcessTree":
        creationflags = 0
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(
            [str(item) for item in command],
            cwd=str(Path(cwd).resolve()),
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            **kwargs,
        )
        return cls(process)

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr_tail)[-4000:]

    def _read_stdout(self) -> None:
        try:
            assert self.process.stdout is not None
            while True:
                line = self.process.stdout.readline()
                if not line:
                    self._lines.put(None)
                    return
                self._lines.put(line)
        except BaseException as exc:  # noqa: BLE001 - relayed to owner
            self._lines.put(exc)

    def _read_stderr(self) -> None:
        try:
            assert self.process.stderr is not None
            for line in self.process.stderr:
                self._stderr_tail.append(line)
                if len(self._stderr_tail) > 80:
                    del self._stderr_tail[:20]
        except Exception:  # noqa: BLE001 - diagnostics are advisory
            return

    def send_line(self, value: str, *, max_bytes: int = 1_048_576) -> None:
        encoded = value.encode("utf-8")
        if len(encoded) > max_bytes or "\n" in value or "\r" in value:
            raise ValueError("Contained-process request is not one bounded line")
        if self.process.stdin is None:
            raise RuntimeError("Contained-process stdin is unavailable")
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()

    def receive_line(self, *, timeout_seconds: float, max_bytes: int) -> str:
        if timeout_seconds <= 0 or max_bytes < 1:
            raise ValueError("Contained-process receive bounds must be positive")
        try:
            item = self._lines.get(timeout=float(timeout_seconds))
        except queue.Empty as exc:
            raise TimeoutError("Contained process response timed out") from exc
        if item is None:
            raise RuntimeError(
                "Contained process exited before responding"
                + (f": {self.stderr_tail}" if self.stderr_tail else "")
            )
        if isinstance(item, BaseException):
            raise RuntimeError("Contained process response failed") from item
        if len(item.encode("utf-8")) > max_bytes:
            raise ValueError("Contained process response exceeded its byte limit")
        line = item.rstrip("\r\n")
        # Test and diagnostic children may report exact descendant PIDs.  They
        # are used only to strengthen the post-kill emptiness proof.
        try:
            decoded = json.loads(line)
            if isinstance(decoded, dict):
                for key, value in decoded.items():
                    if str(key).casefold().endswith("_pid") and isinstance(value, int):
                        self._tracked_pids.add(value)
        except (TypeError, ValueError):
            pass
        return line

    def wait_and_reap(self, *, timeout_seconds: float) -> ProcessReapResult:
        try:
            returncode = self.process.wait(timeout=float(timeout_seconds))
        except subprocess.TimeoutExpired:
            return self.terminate_and_reap(timeout_seconds=timeout_seconds)
        return ProcessReapResult(
            forced=False,
            verified_empty=all(not _pid_exists(pid) for pid in self._tracked_pids),
            returncode=returncode,
        )

    def terminate_and_reap(self, *, timeout_seconds: float) -> ProcessReapResult:
        """Forcibly stop this exact root tree and prove tracked PIDs exited."""

        forced = True
        if self.process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=max(1.0, float(timeout_seconds)),
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            returncode = self.process.wait(timeout=max(0.1, float(timeout_seconds)))
        except subprocess.TimeoutExpired:
            returncode = self.process.poll()
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            if all(not _pid_exists(pid) for pid in self._tracked_pids):
                break
            time.sleep(0.02)
        verified = all(not _pid_exists(pid) for pid in self._tracked_pids)
        return ProcessReapResult(forced, verified, returncode)
