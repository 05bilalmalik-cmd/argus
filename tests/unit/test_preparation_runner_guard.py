"""Revocation must stop the next ARGUS-owned browser mutation."""
from types import SimpleNamespace

import pytest

from app.automation import runner as runner_module
from app.automation.runner import AutomationRunner, _OwnerThreadJourney
from app.automation.types import RunMode


def make_journey(guard):
    mutations = []
    journey = object.__new__(_OwnerThreadJourney)
    journey.runner = SimpleNamespace(preparation_guard=guard)
    journey.adapter = SimpleNamespace(fill=lambda scope, field, value: mutations.append(field))
    journey.document_manifest = {}
    journey.active_document_upload = {}
    return journey, mutations


@pytest.mark.parametrize('result', [False, None, 1, 'true'])
def test_non_true_guard_refuses_field_mutation(result):
    journey, mutations = make_journey(lambda: result)
    with pytest.raises(Exception, match='Preparation authority'):
        journey._fill_resolved_action(object(), SimpleNamespace(mapping=None, field='field', value='value'))
    assert mutations == []


def test_revocation_between_fields_stops_second_field():
    allowed = [True]
    journey, mutations = make_journey(lambda: allowed[0])
    action = SimpleNamespace(mapping=None, field='field', value='value')
    journey._fill_resolved_action(object(), action)
    allowed[0] = False
    with pytest.raises(Exception, match='Preparation authority'):
        journey._fill_resolved_action(object(), action)
    assert mutations == ['field']


def test_guard_exception_does_not_expose_private_detail():
    def guard():
        raise RuntimeError('PRIVATE_SENTINEL_MUST_NOT_ESCAPE')
    journey, mutations = make_journey(guard)
    with pytest.raises(Exception, match='Preparation authority') as error:
        journey._fill_resolved_action(object(), SimpleNamespace(mapping=None, field='field', value='value'))
    assert 'PRIVATE_SENTINEL' not in str(error.value)
    assert mutations == []


def test_bridge_prepare_never_clicks_application_entry(monkeypatch):
    journey, mutations = make_journey(lambda: True)
    journey.opportunity = object()
    journey.adapter_name = 'workday'
    journey.adapter.enter_application_flow = lambda page: mutations.append('entry') or True
    journey._identity_verified = lambda page: True
    journey._run_steps = lambda page: {'state': 'NEEDS_USER'}
    monkeypatch.setattr(runner_module, '_destination_findings', lambda *args: ())
    assert journey.prepare(object()) == {'state': 'NEEDS_USER'}
    assert mutations == []


@pytest.mark.parametrize('mode', [RunMode.SUBMIT, RunMode.REVIEW, RunMode.DRY_RUN])
def test_bridge_runner_rejects_other_modes_before_database_or_browser(mode):
    runner = AutomationRunner(
        None, None, None,
        notifier=SimpleNamespace(enabled=False),
        preparation_guard=lambda: True,
    )
    with pytest.raises(Exception, match='Preparation authority'):
        runner.run('opaque-application', mode, headed=True)


def test_retained_bridge_journey_cannot_be_upgraded_to_submit():
    journey, mutations = make_journey(lambda: True)
    journey.application_id = 'opaque-application'
    journey._result = lambda state, **values: dict(state=state, **values)
    def forbidden_probe(*args):
        pytest.fail('bridge submit reached the browser')
    journey._boundary = forbidden_probe
    result = journey.submit(object(), 'opaque-application')
    assert result['state'] == 'NEEDS_USER'
    assert result['blocked_reasons'] == ('preparation_guard',)
    assert mutations == []


def test_bridge_runner_checks_guard_before_directory_or_database_mutation():
    runner = AutomationRunner(
        None, None, None,
        notifier=SimpleNamespace(enabled=False),
        preparation_guard=lambda: False,
    )
    with pytest.raises(Exception, match='Preparation authority'):
        runner.run('opaque-application', RunMode.PREFILL, headed=True)
