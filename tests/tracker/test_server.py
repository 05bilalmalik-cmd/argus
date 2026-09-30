from pathlib import Path
from fastapi.testclient import TestClient

from app.tracker.contracts import Listing, SourceResult
from app.tracker.server import create_tracker_app


def test_real_store_api_flow_and_no_implicit_refresh(tmp_path: Path):
    calls = []

    def collect():
        calls.append(True)
        return [SourceResult('fixture', 'https://example.com/jobs', 'ok', [
            Listing('Firm', 'Summer Internship 2027', 'https://example.com/jobs/7',
                    'London', 'summer', source_id='7')])]

    app = create_tracker_app(tmp_path, collector=collect, auto_refresh=False)
    with TestClient(app) as client:
        assert client.get('/healthz').json()['live_submit'] is False
        assert client.get('/api/jobs').json()['total'] == 0
        assert calls == []
        assert app.state.refresh.refresh_sync()
        data = client.get('/api/jobs?q=firm').json()
        assert data['total'] == 1
        identifier = data['jobs'][0]['id']
        url = f'/api/jobs/{identifier}'
        assert client.patch(url, json={'saved': True}).status_code == 403
        headers = {'X-Argus-Tracker': '1'}
        assert client.patch(url, headers=headers, json={'saved': True, 'stage': 'applied'}).status_code == 200
        assert client.get('/api/jobs?saved_only=true').json()['jobs'][0]['stage'] == 'applied'
        assert client.patch(url, headers=headers, json={'stage': 'invented'}).status_code == 422
        assert client.patch(url, headers=headers, json={'saved': 'false'}).status_code == 422
        assert client.patch(url, headers={**headers, 'Origin': 'https://attacker.example'}, json={'saved': False}).status_code == 403
        assert client.get('/api/export.csv').status_code == 200
        assert 'Summer Internship 2027' in client.get('/api/export.csv').text
        assert client.get('/api/jobs?programmes=graduate').status_code == 422
        assert client.get('/api/jobs?limit=0').status_code == 422
        assert client.get('/api/jobs', headers={'Host': 'attacker.example'}).status_code == 400
    reopened = create_tracker_app(tmp_path, collector=collect, auto_refresh=False)
    with TestClient(reopened) as client:
        assert client.get('/api/jobs?saved_only=true').json()['total'] == 1
