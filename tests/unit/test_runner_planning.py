from app.automation.classifier import DeterministicClassifier
from app.automation.runner import build_fill_plan, redact_answer_preview
from app.automation.types import InspectedField
from app.domain.questions import FormQuestion


def field(label: str, field_type: str = "text", *, required: bool = True, options=()):
    return InspectedField(
        selector="#field",
        control_type=field_type,
        question=FormQuestion(
            label=label,
            field_type=field_type,
            required=required,
            options=tuple(options),
        ),
    )


def test_fill_plan_resolves_approved_sponsorship_without_exposing_answer() -> None:
    plan = build_fill_plan(
        [field("Will you require sponsorship?", "radio", options=("Yes", "No"))],
        DeterministicClassifier(),
        {"legal.sponsorship": False},
        answer_lookup=lambda key, label: None,
        document_lookup=lambda key: None,
        adapter_name="greenhouse",
    )

    assert plan.risk.level == 0
    assert plan.actions[0].value == "No"
    assert plan.actions[0].source == "approved_profile"
    assert redact_answer_preview(plan.actions[0]) == "[REDACTED]"


def test_fill_plan_pauses_for_assessment_and_captcha() -> None:
    plan = build_fill_plan(
        [
            field("Begin online assessment", "button"),
            field("Verify you are human", "captcha"),
        ],
        DeterministicClassifier(),
        {},
        answer_lookup=lambda key, label: None,
        document_lookup=lambda key: None,
        adapter_name="greenhouse",
    )

    assert plan.risk.level == 3
    assert {finding.code for finding in plan.risk.findings} == {
        "assessment_handoff",
        "captcha_handoff",
    }
    assert all(action.value is None for action in plan.actions)


def test_fill_plan_fails_closed_for_unknown_required_field() -> None:
    plan = build_fill_plan(
        [field("Name the desk partner you spoke with")],
        DeterministicClassifier(),
        {},
        answer_lookup=lambda key, label: None,
        document_lookup=lambda key: None,
        adapter_name="greenhouse",
    )

    assert plan.risk.level == 2
    assert plan.risk.blocking_codes == ("unknown_required_field",)


def test_generic_unknown_ats_requires_review_even_when_fields_are_known() -> None:
    plan = build_fill_plan(
        [field("First name")],
        DeterministicClassifier(),
        {"identity.first_name": "Alex"},
        answer_lookup=lambda key, label: None,
        document_lookup=lambda key: None,
        adapter_name="generic",
    )

    assert plan.risk.level == 1
    assert "unknown_ats" in plan.risk.blocking_codes
