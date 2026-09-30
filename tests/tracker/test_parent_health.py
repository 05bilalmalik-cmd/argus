from app.tracker.pipeline import AutomationPipeline
from app.tracker.mail_settings import ConfiguredAlerts
from app.tracker.contracts import SourceResult
from app.tracker.store import TrackerStore
from tests.tracker.test_server_v2 import Intel


def test_real_unconfigured_mail_transport_is_blocked_not_green(tmp_path):
    pipeline = AutomationPipeline(tmp_path, TrackerStore(tmp_path / 'tracker.sqlite3'),
        lambda: [SourceResult('fixture', 'https://example.test/jobs', 'empty')], Intel(), ConfiguredAlerts(tmp_path))
    pipeline.run()
    assert pipeline.snapshot()['stages']['delivery']['status'] == 'blocked'
