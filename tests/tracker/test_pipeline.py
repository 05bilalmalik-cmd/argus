"""One vertical pipeline slice: collect -> verify -> alert, with durable health."""
from app.tracker.contracts import Listing, SourceResult
from app.tracker.store import TrackerStore


class Intelligence:
    def __init__(self):
        self.seen = []
    def run(self, jobs):
        self.seen = jobs
        return {'checked': len(jobs), 'errors': 0}
    def decorate(self, jobs):
        return [{**job, 'availability': 'open', 'match_status': 'review'} for job in jobs]
    def summary(self):
        return {'checked': len(self.seen)}


class Alerts:
    def __init__(self):
        self.seen = []
    def run(self, jobs):
        self.seen = jobs
        return {'status': 'disabled', 'sent': 0}
    def summary(self):
        return {'status': 'disabled'}


def test_pipeline_passes_persisted_verified_records_to_alerts_and_persists_health(tmp_path):
    from app.tracker.pipeline import AutomationPipeline
    store = TrackerStore(tmp_path / 'tracker.sqlite3')
    intel, alerts = Intelligence(), Alerts()
    def collect():
        return [SourceResult('direct', 'https://jobs.example.org', 'ok', [
            Listing('Firm', 'Summer Intern 2027', 'https://jobs.example.org/jobs/123', 'London', 'summer')])]
    pipeline = AutomationPipeline(tmp_path, store, collect, intel, alerts)
    result = pipeline.run()
    assert result['new'] == 1
    assert len(intel.seen) == len(alerts.seen) == 1
    assert alerts.seen[0]['availability'] == 'open'
    assert alerts.seen[0]['uk_match'] is True
    assert pipeline.snapshot()['stages']['delivery']['status'] == 'blocked'
    reopened = AutomationPipeline(tmp_path, store, collect, intel, alerts)
    assert reopened.snapshot()['stages']['discovery']['last_success_at']
    assert reopened.run()['new'] == 0
