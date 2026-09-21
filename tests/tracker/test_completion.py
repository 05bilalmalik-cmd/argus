from pathlib import Path
from threading import Event, Thread

from bs4 import BeautifulSoup

from app.tracker.runtime import RefreshCoordinator


def test_programme_options_are_selectable():
    soup = BeautifulSoup(Path('app/tracker/static/index.html').read_text(encoding='utf-8'), 'html.parser')
    assert {option.get('value') for option in soup.select('#programme option')} == {
        '', 'summer', 'year_in_industry', 'spring_week'}


def test_shutdown_does_not_return_before_reserved_refresh_finishes(tmp_path):
    reserved, release, stopped, ran = Event(), Event(), Event(), Event()

    class Store:
        def ingest(self, results):
            return {'new': 0}

    coordinator = RefreshCoordinator(tmp_path, Store(), lambda: ran.set() or [])
    original = coordinator._reserve

    def delayed():
        ok = original()
        reserved.set()
        assert release.wait(3)
        return ok

    coordinator._reserve = delayed
    request = Thread(target=coordinator.request_refresh)
    request.start()
    assert reserved.wait(2)
    stopper = Thread(target=lambda: (coordinator.stop(), stopped.set()))
    stopper.start()
    try:
        assert not stopped.wait(.15), 'shutdown returned while a refresh was reserved but unpublished'
    finally:
        release.set()
        request.join(3)
        stopper.join(3)
    assert stopped.is_set()
    assert coordinator.snapshot()['running'] is False
    assert not coordinator.request_refresh()
