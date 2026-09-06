from types import SimpleNamespace

import pytest

from app.routers import api


def test_public_prefill_delegates_to_same_internal_entry_without_authority(monkeypatch):
    seen = []
    sentinel = {'status': 'fixture'}
    def shared(application_id, request, *, preparation_guard=None):
        seen.append((application_id, request, preparation_guard))
        return sentinel
    monkeypatch.setattr(api, '_prefill_application', shared, raising=False)
    request = object()
    assert api.prefill_application('application', request) is sentinel
    assert seen == [('application', request, None)]


def test_bridge_prefill_guard_fails_before_accessing_runtime():
    helper = getattr(api, '_prefill_application', None)
    assert helper is not None, 'shared PREFILL entry is missing'
    with pytest.raises(Exception, match='Preparation authority'):
        helper('application', SimpleNamespace(), preparation_guard=lambda: False)
