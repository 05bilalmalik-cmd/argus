# TDD: R1 — form readiness contract.
# A recognised ATS with ZERO inspected fields must produce Risk 4
# `application_form_not_found`, never READY_TO_SUBMIT.
from __future__ import annotations

import pytest

from app.automation.adapters.generic import GenericAdapter
from app.automation.runner import build_fill_plan
from app.automation.types import InspectedField
from app.domain.questions import CanonicalKey, FormQuestion, Sensitivity
from app.domain.risk import RiskFinding, calculate_risk


class _StubClassifier:
    def classify(self, question: FormQuestion):
        from app.automation.classifier import DeterministicClassifier

        return DeterministicClassifier().classify(question)


def _field(label: str = "Email", field_type: str = "email") -> InspectedField:
    return InspectedField(
        selector="#email",
        control_type=field_type,
        question=FormQuestion(
            label=label,
            field_type=field_type,
            name="email",
            required=True,
        ),
    )


def test_zero_fields_generic_adapter_is_risk4_not_found():
    plan = build_fill_plan(
        [],
        _StubClassifier(),
        {},
        answer_lookup=lambda key, label: None,
        document_lookup=lambda key: None,
        adapter_name="generic",
    )
    assert plan.risk.level == 4
    assert "application_form_not_found" in plan.risk.blocking_codes


def test_zero_fields_recognised_adapter_is_risk4_not_found():
    # The forensic finding: a real Workday review reached Risk 0 with zero
    # inspected fields. Recognised adapters must fail closed too.
    for adapter in ("workday", "greenhouse", "lever"):
        plan = build_fill_plan(
            [],
            _StubClassifier(),
            {},
            answer_lookup=lambda key, label: None,
            document_lookup=lambda key: None,
            adapter_name=adapter,
        )
        assert plan.risk.level == 4, adapter
        assert "application_form_not_found" in plan.risk.blocking_codes, adapter


def test_explicit_step_needs_no_fields_can_be_ready():
    # A provider adapter may carry explicit evidence that the current step
    # legitimately needs no fields (e.g. a review/consent step). That is the
    # ONLY way zero fields can avoid the not-found finding.
    plan = build_fill_plan(
        [],
        _StubClassifier(),
        {},
        answer_lookup=lambda key, label: None,
        document_lookup=lambda key: None,
        adapter_name="workday",
        step_evidence={"step_requires_no_fields": True, "step_name": "review"},
    )
    assert plan.risk.level == 0 or plan.risk.level < 4


def test_nonempty_fields_do_not_trigger_not_found():
    plan = build_fill_plan(
        [_field()],
        _StubClassifier(),
        {"contact.email": "test@example.com"},
        answer_lookup=lambda key, label: None,
        document_lookup=lambda key: None,
        adapter_name="greenhouse",
    )
    assert "application_form_not_found" not in plan.risk.blocking_codes
