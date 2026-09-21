from app.tracker.runtime import RefreshCoordinator


class Store:
    def ingest(self, results):
        raise AssertionError('The composed pipeline owns ingestion')


def test_composed_executor_runs_under_refresh_owner(tmp_path):
    seen = []
    coordinator = RefreshCoordinator(tmp_path, Store(), lambda: [], interval_seconds=60,
                                     executor=lambda: seen.append('pipeline') or {'new': 2})
    assert coordinator.refresh_sync()
    assert seen == ['pipeline']
    assert coordinator.snapshot()['last_result'] == {'new': 2}


def test_previous_failure_remains_visible_until_success(tmp_path):
    attempt = []
    def work():
        attempt.append(1)
        if len(attempt) == 1:
            raise RuntimeError('failed')
        assert coordinator.snapshot()['last_error']
        return {'new': 0}
    coordinator = RefreshCoordinator(tmp_path, Store(), lambda: [], interval_seconds=60, executor=work)
    assert not coordinator.refresh_sync()
    assert coordinator.refresh_sync()
    assert coordinator.snapshot()['last_error'] == ''
