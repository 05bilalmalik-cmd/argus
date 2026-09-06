# RED/GREEN: PREFILL mode must never call advance_one_step.
# Tests for the explicit mode guard in _run_steps.
from __future__ import annotations

from types import SimpleNamespace

from app.automation.runner import _OwnerThreadJourney
from app.automation.targets import TargetResolution
from app.automation.types import RunMode
from app.domain.targets import TargetKind


def test_prefill_with_risk_zero_does_not_advance():
    """PREFILL with risk==0 must return NEEDS_USER without calling advance."""
    advanced = False
    build_count = 0

    class Adapter:
        name = "workday"

        def fill(self, _scope, field, value):
            pass

        def advance_one_step(self, _scope):
            nonlocal advanced
            advanced = True
            return False

        def is_final_submit_visible(self, _scope):
            return False

    journey = object.__new__(_OwnerThreadJourney)
    journey.application_id = "prefill-no-advance-test"
    journey.opportunity = SimpleNamespace(employer="TestCorp", role_title="Analyst")
    journey.resolution = TargetResolution(
        source_url="http://test/source",
        final_url="http://test/application",
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={},
    )
    journey.mode = RunMode.PREFILL
    journey.adapter = Adapter()
    journey.adapter_name = "workday"
    journey.step_index = 0
    journey.last_scope = None
    journey.last_fields = []
    journey.last_evidence = {}
    journey.last_plan = None
    journey.final_handle = None
    journey.final_target = None
    journey.final_scope = None
    journey.target_error = ""
    journey._human_stage = ""
    journey._boundary = lambda _page: (False, "")
    journey._inspect = lambda _page: (
                [SimpleNamespace(
                    selector="#first",
                    question=SimpleNamespace(name="first_name", label="First name", sensitivity="visible"),
                    control_type="text",
                    current_value="",
                    options=(),
                )],
        {"root_found": True, "step_name": "step-1", "root_selector": "#step"},
        object(),
    )
    journey._build_plan = lambda fields, _evidence: SimpleNamespace(
        actions=[
            SimpleNamespace(
                field=fields[0],
                value="Alex",
                status="resolved",
            )
        ],
        risk=SimpleNamespace(level=0, blocking_codes=()),
    )
    journey._verify_after_fill = lambda _scope, _fields, _values: True

    result = journey._run_steps(SimpleNamespace(evaluate=lambda _script: {}))

    assert result["state"] == "NEEDS_USER"
    assert advanced is False, "advance_one_step must NOT be called in PREFILL mode"
    assert "prefill" in result.get("reason", "").lower() or "human" in result.get("reason", "").lower()


def test_non_prefill_mode_still_advances():
    """SUBMIT mode must still call advance_one_step."""
    advanced = False

    class Adapter:
        name = "workday"

        def fill(self, _scope, field, value):
            pass

        def advance_one_step(self, _scope):
            nonlocal advanced
            advanced = True
            return False  # False = step transition unproven

        def is_final_submit_visible(self, _scope):
            return False

    journey = object.__new__(_OwnerThreadJourney)
    journey.application_id = "submit-advance-test"
    journey.opportunity = SimpleNamespace(employer="TestCorp", role_title="Analyst")
    journey.resolution = TargetResolution(
        source_url="http://test/source",
        final_url="http://test/application",
        kind=TargetKind.APPLICATION_ENTRY,
        provider="greenhouse",
        identity_verified=True,
        evidence={},
    )
    journey.mode = RunMode.SUBMIT
    journey.adapter = Adapter()
    journey.adapter_name = "workday"
    journey.step_index = 0
    journey.last_scope = None
    journey.last_fields = []
    journey.last_evidence = {}
    journey.last_plan = None
    journey.final_handle = None
    journey.final_target = None
    journey.final_scope = None
    journey.target_error = ""
    journey._human_stage = ""
    journey._boundary = lambda _page: (False, "")
    journey._inspect = lambda _page: (
        [],
        {"root_found": True, "step_name": "step-1", "root_selector": "#step"},
        object(),
    )
    journey._build_plan = lambda fields, _evidence: SimpleNamespace(
        actions=[],
        risk=SimpleNamespace(level=0, blocking_codes=()),
    )
    journey._verify_after_fill = lambda _scope, _fields, _values: True

    result = journey._run_steps(SimpleNamespace(evaluate=lambda _script: {}))

    assert advanced is True, "SUBMIT mode must call advance_one_step"