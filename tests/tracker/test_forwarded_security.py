from fastapi.testclient import TestClient
from app.tracker.server import create_tracker_app
from app.tracker.contracts import SourceResult


def test_ssh_forwarded_origin_can_save_without_allowing_other_origins(tmp_path):
    app = create_tracker_app(tmp_path, auto_refresh=False,
        collector=lambda: [SourceResult('fixture', 'https://example.test/jobs', 'empty')])
    async def forwarded(scope, receive, send):
        await app({**scope, 'server': ('127.0.0.1', 8791)}, receive, send)
    with TestClient(forwarded, base_url='http://127.0.0.1:8792') as client:
        headers = {'X-Argus-Tracker': '1', 'Origin': 'http://127.0.0.1:8792', 'Sec-Fetch-Site': 'same-origin'}
        assert client.post('/api/refresh', headers=headers).status_code == 202
        headers['Origin'] = 'http://127.0.0.1:9999'
        assert client.post('/api/refresh', headers=headers).status_code == 403
        headers['Origin'] = 'https://evil.example'
        assert client.post('/api/refresh', headers=headers).status_code == 403
        headers['Host'] = '127.0.0.1:bad-port'
        assert client.post('/api/refresh', headers=headers).status_code == 400
