from fastapi.testclient import TestClient
from app.tracker.contracts import Listing, SourceResult
from app.tracker.server import create_tracker_app
from app.tracker.profile import DEFAULT_PROFILE, validate_profile
import copy


class Intel:
    def __init__(self):
        self.profile = copy.deepcopy(DEFAULT_PROFILE)
    def run(self, jobs):
        return {'checked': len(jobs), 'errors': 0}
    def decorate(self, jobs):
        return [{**j, 'availability': 'open', 'match_status': 'potential', 'evidence': [],
                 'match_reasons': ['Test fixture'], 'match_unknowns': ['Grade unknown']}
                for j in jobs]
    def summary(self):
        return {'checked': 1}
    def get_profile(self):
        return copy.deepcopy(self.profile)
    def set_profile(self, profile):
        self.profile = validate_profile(profile)
        return self.get_profile()


class Alerts:
    def run(self, jobs):
        return {'status': 'disabled'}
    def summary(self):
        return {'status': 'disabled'}
    def list_events(self, limit=50):
        return []


def test_automated_api_exposes_profile_evidence_alerts_and_readiness(tmp_path):
    def collect():
        return [SourceResult('fixture', 'https://example.org/jobs', 'ok', [
            Listing('Firm', 'Summer Internship 2027', 'https://example.org/jobs/7', 'London', 'summer')])]
    app = create_tracker_app(tmp_path, collector=collect, auto_refresh=False, automated=True,
                             intelligence=Intel(), alerts=Alerts())
    with TestClient(app) as c:
        assert c.get('/readyz').status_code == 503
        assert c.get('/api/status').json()['pipeline']['stages']['discovery']['status'] == 'never_checked'
        assert app.state.refresh.refresh_sync()
        jobs = c.get('/api/jobs?match_status=potential&availability=open&sort=priority').json()
        assert jobs['total'] == 1
        detail = c.get('/api/jobs/' + str(jobs['jobs'][0]['id'])).json()
        assert detail['match_unknowns'] == ['Grade unknown']
        assert c.get('/api/jobs?availability=closed').json()['total'] == 0
        assert c.get('/api/jobs?match_status=eligible').status_code == 422
        assert c.get('/api/alerts').json()['summary']['status'] == 'disabled'
        assert c.get('/readyz').status_code == 503  # Disabled email is not full automation.
        profile = c.get('/api/profile').json()['profile']
        profile['graduation_years']['summer'] = 2030
        assert c.put('/api/profile', json=profile).status_code == 403
        assert c.put('/api/profile', json=profile, headers={'X-Argus-Tracker': '1'}).status_code == 200
        assert c.get('/api/profile').json()['profile']['graduation_years']['summer'] == 2030
        profile['graduation_years']['summer'] = True
        assert c.put('/api/profile', json=profile, headers={'X-Argus-Tracker': '1'}).status_code == 422


def test_email_setup_route_is_guarded_and_never_echoes_password(tmp_path):
    from app.tracker.mail_settings import ConfiguredAlerts
    mail = ConfiguredAlerts(tmp_path, credential_check=lambda config: None)
    app = create_tracker_app(tmp_path, collector=lambda: [], auto_refresh=False,
                             automated=True, intelligence=Intel(), alerts=mail)
    with TestClient(app) as c:
        assert c.get('/settings/email').status_code == 200
        assert c.get('/api/email/configuration').json()['configured'] is False
        payload = {'smtp_host': 'smtp.gmail.com', 'smtp_port': 587,
                   'smtp_user': 'fixture@example.test', 'smtp_password': 'fixture-password-only',
                   'from_address': 'fixture@example.test', 'to_address': 'fixture@example.test'}
        assert c.put('/api/email/configuration', json=payload).status_code == 403
        response = c.put('/api/email/configuration', json=payload, headers={'X-Argus-Tracker': '1'})
        assert response.status_code == 200
        assert 'fixture-password-only' not in response.text
        bad = c.put('/api/email/configuration', json=[payload], headers={'X-Argus-Tracker': '1'})
        assert bad.status_code == 422
        assert 'fixture-password-only' not in bad.text
