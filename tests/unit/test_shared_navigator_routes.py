"""Fail-closed shared-Navigator wiring for scout and full-sweep routes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.automation.types import AutomationOutcome, RunMode
from app.routers import scout, sweep


def _request(*, navigator=object(), legacy_manager=object()):
    state = SimpleNamespace(
        db=object(),
        settings=object(),
        crypto=object(),
        navigator=navigator,
        handoff_manager=legacy_manager,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_scout_factory_binds_the_create_app_navigator(monkeypatch):
    navigator = object()
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            captured["handoff_manager"] = kwargs["handoff_manager"]

        def run(self, application_id, mode, *, headed):  # noqa: ANN001
            captured["run"] = (application_id, mode, headed)
            return {"state": "NEEDS_USER"}

    monkeypatch.setattr(scout, "AutomationRunner", FakeRunner)
    factory = scout._runner_factory(_request(navigator=navigator))

    assert factory("application-1", RunMode.REVIEW, False) == {"state": "NEEDS_USER"}
    assert captured["handoff_manager"] is navigator
    assert captured["run"] == ("application-1", RunMode.REVIEW, False)


def test_scout_factory_does_not_fall_back_to_legacy_handoff_manager():
    request = _request(navigator=None, legacy_manager=object())

    with pytest.raises(RuntimeError, match="navigator"):
        scout._runner_factory(request)("application-1", RunMode.REVIEW, False)


def test_full_sweep_factory_binds_the_same_create_app_navigator(monkeypatch):
    navigator = object()
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            captured["handoff_manager"] = kwargs["handoff_manager"]

        def run(self, application_id, mode, *, headed):  # noqa: ANN001
            captured["run"] = (application_id, mode, headed)
            return AutomationOutcome(
                state="NEEDS_USER",
                risk_level=2,
                adapter="greenhouse",
            )

    import app.automation.runner as runner_module

    monkeypatch.setattr(runner_module, "AutomationRunner", FakeRunner)
    request = _request(navigator=navigator)
    factory = sweep._runner_factory(request, request.app.state.settings)

    payload = factory("application-2", "review", True)

    assert payload["state"] == "NEEDS_USER"
    assert captured["handoff_manager"] is navigator
    assert captured["run"] == ("application-2", RunMode.REVIEW, True)


def test_full_sweep_factory_fails_closed_without_create_app_navigator():
    request = _request(navigator=None, legacy_manager=object())

    with pytest.raises(RuntimeError, match="navigator"):
        sweep._runner_factory(request, request.app.state.settings)(
            "application-2", "review", True
        )
