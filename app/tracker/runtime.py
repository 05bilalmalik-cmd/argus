"""Discovery scheduling with one refresh owner and no application runner."""
from __future__ import annotations

import math
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.scouting.sweep_lock import SweepLock
from app.tracker.contracts import SourceResult, utc_now


class RefreshCoordinator:
    def __init__(self, data_dir: Path, store, collector: Callable[[], list[SourceResult]],
                 *, interval_seconds: float = 1800, executor: Callable[[], dict] | None = None):
        if not math.isfinite(interval_seconds) or interval_seconds < 60:
            raise ValueError("Refresh interval must be at least 60 seconds")
        self.data_dir, self.store, self.collector = Path(data_dir), store, collector
        self.interval_seconds = interval_seconds
        self.executor = executor
        self._guard = threading.RLock()
        self._stop = threading.Event()
        self._running = False
        self._worker: threading.Thread | None = None
        self._scheduler: threading.Thread | None = None
        self._state = {'last_started_at': None, 'last_finished_at': None,
                       'last_result': None, 'last_error': '', 'next_refresh_at': None}

    def snapshot(self) -> dict:
        with self._guard:
            return {**self._state, 'running': self._running,
                    'automatic': bool(self._scheduler and self._scheduler.is_alive()),
                    'interval_seconds': self.interval_seconds}

    def _reserve(self) -> bool:
        with self._guard:
            if self._running or self._stop.is_set():
                return False
            self._running = True
            self._state.update(last_started_at=utc_now())
            return True

    def request_refresh(self) -> bool:
        # Publication/start and shutdown share a guard: stop cannot miss a
        # reserved worker or attempt to join a thread which has not started.
        with self._guard:
            if not self._reserve():
                return False
            self._worker = threading.Thread(target=self._execute, name='argus-tracker-refresh',
                                            daemon=True)
            try:
                self._worker.start()
            except Exception:
                self._worker = None
                self._running = False
                raise
            return True

    def refresh_sync(self) -> bool:
        return self._execute() if self._reserve() else False

    def _execute(self) -> bool:
        lock = SweepLock(self.data_dir / 'refresh.lock')
        try:
            if not lock.acquire():
                raise RuntimeError('Another tracker process is already refreshing this store')
            result = self.executor() if self.executor else self.store.ingest(self.collector())
            with self._guard:
                self._state['last_result'] = result
                self._state['last_error'] = ''
            return True
        except Exception as exc:
            with self._guard:
                self._state['last_error'] = (
                    f'{type(exc).__name__}: automated stage failed; inspect pipeline health'
                    if self.executor else f'{type(exc).__name__}: {exc}'[:500]
                )
                self._state['last_result'] = None
            return False
        finally:
            lock.release()
            with self._guard:
                self._running = False
                self._state['last_finished_at'] = utc_now()

    def start(self) -> None:
        with self._guard:
            if self._scheduler and self._scheduler.is_alive():
                return
            self._stop.clear()
            self.request_refresh()
            self._scheduler = threading.Thread(target=self._loop, name='argus-tracker-scheduler',
                                               daemon=True)
            self._scheduler.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._guard:
                self._state['next_refresh_at'] = (
                    datetime.now(timezone.utc) + timedelta(seconds=self.interval_seconds)
                ).isoformat()
            if self._stop.wait(self.interval_seconds):
                break
            self.request_refresh()
        with self._guard:
            self._state['next_refresh_at'] = None

    def stop(self) -> None:
        with self._guard:
            self._stop.set()
            scheduler, worker = self._scheduler, self._worker
        if scheduler:
            scheduler.join(timeout=5)
        if worker:
            worker.join(timeout=180)
            if worker.is_alive():
                raise RuntimeError('Tracker refresh did not stop within 180 seconds')
