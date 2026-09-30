from pathlib import Path
from threading import Event

from app.tracker.runtime import RefreshCoordinator


class Store:
    def __init__(self):
        self.calls = 0

    def ingest(self, results):
        self.calls += 1
        return {'new': 1, 'updated': 0, 'observed': 1, 'sources': len(results), 'errors': 0}


def test_refresh_single_flight_and_failure_are_visible(tmp_path: Path):
    entered, release = Event(), Event()
    store = Store()

    def collect():
        entered.set()
        assert release.wait(3)
        return []

    coordinator = RefreshCoordinator(tmp_path, store, collect, interval_seconds=60)
    assert coordinator.request_refresh()
    assert entered.wait(3)
    assert coordinator.request_refresh() is False
    release.set()
    coordinator.stop()
    assert store.calls == 1
    assert coordinator.snapshot()['running'] is False
    assert coordinator.snapshot()['last_result']['new'] == 1


def test_refresh_exception_is_not_reported_as_success(tmp_path: Path):
    def broken():
        raise RuntimeError('fixture source collection failed')

    store = Store()
    coordinator = RefreshCoordinator(tmp_path, store, broken, interval_seconds=60)
    assert coordinator.refresh_sync() is False
    assert coordinator.snapshot()['last_error'] == 'RuntimeError: fixture source collection failed'
    assert store.calls == 0


def test_two_coordinators_cannot_refresh_one_store(tmp_path: Path):
    entered, release = Event(), Event()

    def collect():
        entered.set()
        assert release.wait(3)
        return []

    first = RefreshCoordinator(tmp_path, Store(), collect, interval_seconds=60)
    second = RefreshCoordinator(tmp_path, Store(), lambda: [], interval_seconds=60)
    first.request_refresh()
    assert entered.wait(3)
    try:
        assert second.refresh_sync() is False
        assert 'another' in second.snapshot()['last_error'].lower()
    finally:
        release.set()
        first.stop()
