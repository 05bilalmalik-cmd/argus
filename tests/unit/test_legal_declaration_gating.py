from __future__ import annotations

import pytest

from app.automation.classifier import DeterministicClassifier
from app.automation.runner import build_fill_plan
from app.automation.types import InspectedField
from app.domain.questions import CanonicalKey, FormQuestion


def _question(
    label: str,
    *,
    name: str = "",
    field_type: str = "text",
    required: bool = True,
    options: tuple[str, ...] = (),
) -> FormQuestion:
    return FormQuestion(
        label=label,
        field_type=field_type,
        name=name,
        required=required,
        options=options,
    )


def _field(question: FormQuestion, *, selector: str = "#field") -> InspectedField:
    return InspectedField(
        selector=selector,
        question=question,
        control_type=question.field_type,
    )


@pytest.mark.parametrize(
    "label",
    [
        # Non-UK jurisdictions: the stored UK-scoped answer must not fill these.
        "Will you now, or in the future, require sponsorship for employment "
        "visa status to work in the United States?",
        "Do you now, or will you in the future, need sponsorship from an "
        "employer in order to obtain, extend or renew your authorization "
        "to work in Ireland?",
        "Will you require immigration sponsorship now or in the future? "
        "Examples include F-1 OPT, H-1B, H-4 EAD, L-1, TN, O-1.",
        # Unknown jurisdiction: no country signal at all — still escalate.
        "Will you require sponsorship?",
    ],
)
def test_sponsorship_with_approved_profile_value_is_escalated_not_filled(
    label: str,
) -> None:
    """A stored UK sponsorship answer must never auto-fill a declaration.

    Fail closed: the field is left blank for the human through the exact
    same `missing` escalation used for any question with no approved answer.
    """
    field = _field(
        _question(label, name="sponsorship", field_type="radio", options=("Yes", "No"))
    )
    plan = build_fill_plan(
        [field],
        DeterministicClassifier(),
        {CanonicalKey.SPONSORSHIP.value: False},
        answer_lookup=lambda _key, _label: (
            "synthetic fixture: UK sponsorship placeholder (synthetic fixture)"
        ),
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    action = plan.actions[0]
    assert action.mapping.canonical_key is CanonicalKey.SPONSORSHIP
    assert action.value is None
    assert action.source == "missing"
    assert action.status == "blocked"
    assert "approved_legal_answer_missing" in plan.risk.blocking_codes
    assert plan.risk.level == 3
    assert "United Kingdom" not in repr(plan)


def test_work_authorisation_with_approved_profile_value_is_escalated_not_filled() -> None:
    field = _field(
        _question(
            "Are you legally authorized to work in the United States?",
            name="work_auth",
            field_type="radio",
            options=("Yes", "No"),
        )
    )
    plan = build_fill_plan(
        [field],
        DeterministicClassifier(),
        {CanonicalKey.WORK_AUTHORISATION.value: "Yes — UK settled status"},
        answer_lookup=lambda _key, _label: "Yes — UK settled status",
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    action = plan.actions[0]
    assert action.mapping.canonical_key is CanonicalKey.WORK_AUTHORISATION
    assert action.value is None
    assert action.source == "missing"
    assert action.status == "blocked"
    assert "approved_legal_answer_missing" in plan.risk.blocking_codes
    assert "settled" not in repr(plan)


@pytest.mark.parametrize("required", [True, False])
def test_criminal_record_is_always_escalated_even_with_approved_answer(
    required: bool,
) -> None:
    """A criminal-record declaration is never auto-answered, unconditionally."""
    field = _field(
        _question(
            "Do you have any criminal record (unspent convictions) to declare?",
            name="criminal_record",
            field_type="radio",
            required=required,
            options=("Yes", "No"),
        )
    )
    plan = build_fill_plan(
        [field],
        DeterministicClassifier(),
        {CanonicalKey.CRIMINAL_RECORD.value: "No convictions recorded (United Kingdom scope)"},
        answer_lookup=lambda _key, _label: "No convictions recorded (United Kingdom scope)",
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    action = plan.actions[0]
    assert action.mapping.canonical_key is CanonicalKey.CRIMINAL_RECORD
    assert action.value is None
    assert action.source == "missing"
    assert action.status == ("blocked" if required else "omitted")
    assert "Kingdom" not in repr(plan)
    if required:
        assert "approved_legal_answer_missing" in plan.risk.blocking_codes
        assert plan.risk.level == 3


def test_gated_legal_action_matches_the_existing_missing_answer_mechanism() -> None:
    """The escalation must reuse the codebase's real cannot-answer path.

    A gated declaration with a stored value must produce exactly the same
    action shape (`missing` / `blocked`) and finding code as a known
    mapping that genuinely has no stored value.
    """
    gated = _field(
        _question("Will you require sponsorship?", name="sponsorship", field_type="radio")
    )
    control = _field(
        _question("Start date year*", name="start-year--0", field_type="number")
    )
    plan = build_fill_plan(
        [gated, control],
        DeterministicClassifier(),
        {CanonicalKey.SPONSORSHIP.value: False},
        answer_lookup=lambda _key, _label: None,
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    gated_action, control_action = plan.actions
    assert gated_action.source == control_action.source == "missing"
    assert gated_action.status == control_action.status == "blocked"
    assert gated_action.value is None and control_action.value is None


def test_benign_profile_questions_still_autofill_as_before() -> None:
    """Regression guard: ordinary prefill must keep working unchanged."""
    fields = [
        _field(_question("First name", name="first_name")),
        _field(_question("Email", name="email", field_type="email")),
        _field(_question("University", name="university")),
    ]
    plan = build_fill_plan(
        fields,
        DeterministicClassifier(),
        {
            CanonicalKey.FIRST_NAME.value: "Alex",
            CanonicalKey.EMAIL.value: "alex@example.test",
            CanonicalKey.UNIVERSITY.value: "Lancaster University",
        },
        answer_lookup=lambda _key, _label: None,
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    assert [action.value for action in plan.actions] == [
        "Alex",
        "alex@example.test",
        "Lancaster University",
    ]
    assert all(action.status == "resolved" for action in plan.actions)
    assert all(action.source == "approved_profile" for action in plan.actions)
