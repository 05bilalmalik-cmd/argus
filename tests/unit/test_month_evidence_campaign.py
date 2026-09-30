"""Month values require agreeing canonical year evidence, not a policy tier."""
import pytest
from app.automation.runner import build_fill_plan
from app.domain.questions import CanonicalKey
from tests.unit.test_month_control_campaign import _month_field, _FixedClassifier

G = CanonicalKey.GRADUATION_YEAR.value
E = CanonicalKey.EDUCATION_END_YEAR.value
T = 'guard.programme_graduation_tier'

@pytest.mark.parametrize('values,month,allowed', [
    ({G:2028, E:2028}, '2028-06', True),
    ({G:2029, E:2029, T:2029}, '2029-06', True),
    ({G:2028}, '2028-06', True),
    ({E:2029}, '2029-06', True),
    ({T:2028}, '2028-06', False),
    ({G:2029, T:2028}, '2029-06', False),
    ({G:2028, E:2029}, '2028-06', False),
    ({G:2028, E:'invalid'}, '2028-06', False),
    ({G:2028}, '\u0662\u0660\u0662\u0668-06', False),
    ({G:'\u0662\u0660\u0662\u0668'}, '2028-06', False),
    ({G:2028, T:'invalid'}, '2028-06', False),
    ({G:2028}, '2028-13', False),
    ({G:2028}, '2029-06', False),
])
def test_calendar_month_canonical_year_evidence(values, month, allowed):
    key = CanonicalKey.EDUCATION_END_MONTH
    plan = build_fill_plan(
        [_month_field(label='Education end month', name='end_month')],
        _FixedClassifier(key), values,
        answer_lookup=lambda candidate,label: month if candidate == key.value else None,
        document_lookup=lambda _:None, adapter_name='generic')
    action = plan.actions[0]
    if allowed:
        assert action.status == 'resolved'
        assert action.value == month
    else:
        assert action.status != 'resolved'
        assert action.value is None
        assert not plan.risk.can_submit
