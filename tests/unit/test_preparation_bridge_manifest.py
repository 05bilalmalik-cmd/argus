"""The legacy receipt_json column also holds preparation-only field audits."""
import json

import pytest

from tests.unit.test_preparation_bridge_core import env, bridge, seed_application, successful_executor


def manifest():
    return {'mode': 'prefill', 'state': 'NEEDS_USER', 'adapter': 'generic', 'risk_level': 0,
            'blocked_reasons': [], 'fields': [{'label': 'First name', 'canonical_key': 'first_name',
            'outcome': 'filled', 'required': True, 'reason_code': '', 'readback_confirmed': True}]}


def run_with_receipt_json(env, raw):
    from app.models import AutomationRun
    app_id = seed_application(env)
    succeed = successful_executor(env)
    def execute(application_id, guard):
        result = succeed(application_id, guard)
        with env.database.session_scope() as session:
            session.get(AutomationRun, result['run_id']).receipt_json = raw
        return result
    service = bridge(env, execute=execute)
    service.approve(app_id, app_id, 60)
    return service.dispatch('request_approved_preparation', {'idempotency_key': 'real-shaped-manifest'})


def test_real_preparation_manifest_is_not_a_submission_receipt(env):
    assert run_with_receipt_json(env, json.dumps(manifest()))['status'] == 'PREFILLED_HANDOFF'


@pytest.mark.parametrize('mutate', [
    lambda m: m.update(receipt={'reference': 'synthetic-receipt'}),
    lambda m: m.update(unknown_diagnostic='private-marker'),
    lambda m: m.update(mode='submit'),
    lambda m: m.update(state='CONFIRMED'),
    lambda m: m.update(state='SUBMISSION_UNKNOWN'),
    lambda m: m['fields'][0].update(readback_confirmed=False),
    lambda m: m['fields'][0].update(readback_confirmed=1),
    lambda m: m['fields'][0].update(value='private-marker'),
])
def test_non_preparation_or_unverified_manifest_is_integrity_refusal(env, mutate):
    value = manifest(); mutate(value)
    result = run_with_receipt_json(env, json.dumps(value))
    assert (result['status'], result['reason']) == ('BLOCKED', 'INTEGRITY')


@pytest.mark.parametrize('raw', ['invalid-json', '[]', 'false', '{"mode":"submit","mode":"prefill"}', '"private-marker"'])
def test_malformed_persisted_evidence_does_not_become_a_permissive_default(env, raw):
    result = run_with_receipt_json(env, raw)
    assert (result['status'], result['reason']) == ('BLOCKED', 'INTEGRITY')
