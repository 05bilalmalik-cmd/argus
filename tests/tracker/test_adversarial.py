from pathlib import Path
from datetime import datetime, timezone

from app.tracker.contracts import Listing, SourceResult
from app.tracker.store import TrackerStore
from app.tracker.server import create_tracker_app
from app.tracker.source_parsers import parse_simplytk_html


def test_table_roles_sharing_apply_url_survive_parser_and_store(tmp_path: Path):
    html = '''<p>2 live UK openings</p><table><thead><tr><th>Company</th><th>Role</th>
    <th>Programme</th><th>Location</th><th>Deadline</th><th>Apply</th></tr></thead><tbody>
    <tr><td>Firm</td><td>Markets Internship</td><td>Summer internship</td><td>London</td><td>-</td>
    <td><a href="https://firm.example/earlycareers">Apply</a></td></tr>
    <tr><td>Firm</td><td>Risk Internship</td><td>Summer internship</td><td>London</td><td>-</td>
    <td><a href="https://firm.example/earlycareers">Apply</a></td></tr></tbody></table>'''
    listings = parse_simplytk_html(html, 'https://simplytk.com/internship-tracker')
    assert len(listings) == 2
    store = TrackerStore(tmp_path / 'table.sqlite3')
    store.ingest([SourceResult('simplytk', 'https://simplytk.com/internship-tracker', 'ok', listings)])
    assert len(store.list_jobs()) == 2
from fastapi.testclient import TestClient


def test_distinct_roles_on_unknown_careers_page_are_not_merged(tmp_path: Path):
    store = TrackerStore(tmp_path / 'roles.sqlite3')
    store.ingest([SourceResult('public-table', 'https://firm.example/earlycareers', 'partial', [
        Listing('Firm', 'Summer Markets Internship', 'https://firm.example/earlycareers', 'London', 'summer'),
        Listing('Firm', 'Summer Risk Internship', 'https://firm.example/earlycareers', 'London', 'summer'),
    ], error='partial table')])
    assert len(store.list_jobs()) == 2


def test_partial_source_is_visible_as_attention_in_api(tmp_path: Path):
    app = create_tracker_app(tmp_path, auto_refresh=False, collector=lambda: [])
    app.state.store.ingest([SourceResult('table', 'https://example.com/jobs', 'partial', [], error='Only first page captured')])
    with TestClient(app) as client:
        data = client.get('/api/status').json()
        assert data['summary']['source_attention'] == 1
        assert data['sources'][0]['last_success_at'] is None


def test_future_observation_is_not_reported_as_new_today(tmp_path: Path):
    store = TrackerStore(tmp_path / 'roles.sqlite3')
    store.ingest([SourceResult('future', 'https://example.com/jobs', 'ok', [
        Listing('Firm', 'Summer Internship', 'https://example.com/jobs/1', 'London', 'summer')
    ], checked_at='2099-01-01T12:00:00Z')])
    assert store.summary()['new_24h'] == 0
