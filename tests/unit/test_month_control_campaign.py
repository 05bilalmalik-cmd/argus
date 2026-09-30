"""Month-control fill-plan campaign (Agent 03).

RED-first regression coverage for the proven shape bug: a native
``type=month`` control classified to a graduation-derived key resolved the
approved year-only profile value (e.g. ``2028``) as a usable fill value, but
``GenericAdapter.fill`` passes it raw to ``month.fill`` which throws. The
product must escalate year-only data to human review WITHOUT fabricating a
``YYYY-MM`` month.

All tests exercise the real ``build_fill_plan``. No browser, no network.
"""
from __future__ import annotations

from app.automation.classifier import DeterministicClassifier
from app.automation.runner import build_fill_plan
from app.automation.types import InspectedField
from app.domain.questions import CanonicalKey, FormQuestion, QuestionMapping, Sensitivity


def _month_field(
    label: str = "Expected Graduation Date",
    *,
    name: str = "graduation_date",
    required: bool = True,
) -> InspectedField:
    return InspectedField(
        selector="#graduation_date",
        control_type="month",
        question=FormQuestion(
            label=label,
            field_type="month",
            name=name,
            required=required,
        ),
    )


def _year_select_field(*, required: bool = True) -> InspectedField:
    return InspectedField(
        selector="#graduation_year",
        control_type="select",
        question=FormQuestion(
            label="Expected graduation year",
            field_type="select",
            name="graduation_year",
            required=required,
            options=("2027", "2028", "2029"),
        ),
    )


class _FixedClassifier:
    """Force one canonical key (used only where the deterministic mapping is
    not the subject of the test)."""

    def __init__(self, key: CanonicalKey) -> None:
        self._key = key

    def classify(self, question: FormQuestion) -> QuestionMapping:
        return QuestionMapping(
            canonical_key=self._key,
            confidence=1.0,
            sensitivity=Sensitivity.STANDARD,
            reason="test-fixed mapping",
        )


def _no_lookup(key: str, label: str):  # noqa: ANN001, ANN202
    return None


def _no_doc(key: str):  # noqa: ANN001, ANN202
    return None


def _finding_codes(plan) -> set[str]:  # noqa: ANN001, ANN202
    return {finding.code for finding in plan.risk.findings}


# ---------------------------------------------------------------- RED core

class TestYearOnlyNeverFillsMonth:
    def test_required_month_with_year_only_profile_is_blocked(self) -> None:
        """Core reproducer: year-only 2028 on a required month control must
        NOT resolve. Pre-patch this yields status resolved / value '2028'."""
        plan = build_fill_plan(
            [_month_field()],
            DeterministicClassifier(),
            {"education.graduation_year": 2028},
            answer_lookup=_no_lookup,
            document_lookup=_no_doc,
            adapter_name="generic",
        )
        action = plan.actions[0]
        assert action.mapping.canonical_key == CanonicalKey.GRADUATION_YEAR
        assert action.value is None, (
            f"year-only value must never reach a month control, got {action.value!r}"
        )
        assert action.status == "blocked"
        assert plan.risk.can_submit is False

    def test_optional_month_with_year_only_profile_is_omitted(self) -> None:
        plan = build_fill_plan(
            [_month_field(required=False)],
            DeterministicClassifier(),
            {"education.graduation_year": 2028},
            answer_lookup=_no_lookup,
            document_lookup=_no_doc,
            adapter_name="generic",
        )
        action = plan.actions[0]
        assert action.value is None
        assert action.status == "omitted"

    def test_year_only_string_profile_is_blocked(self) -> None:
        plan = build_fill_plan(
            [_month_field()],
            DeterministicClassifier(),
            {"education.graduation_year": "2028"},
            answer_lookup=_no_lookup,
            document_lookup=_no_doc,
            adapter_name="generic",
        )
        assert plan.actions[0].value is None
        assert plan.actions[0].status == "blocked"


# ------------------------------------------------------- explicit YYYY-MM

class TestExplicitMonthValues:
    def test_matching_approved_yyyy_mm_resolves(self) -> None:
        """An explicitly approved YYYY-MM whose year matches the framed
        profile evidence fills a correctly classified end-month control."""
        plan = build_fill_plan(
            [_month_field(label="Education end month", name="end_month")],
            _FixedClassifier(CanonicalKey.EDUCATION_END_MONTH),
            {"education.graduation_year": 2028, "answer.end_year": 2028},
            answer_lookup=lambda key, label: "2028-06"
            if key == CanonicalKey.EDUCATION_END_MONTH.value
            else None,
            document_lookup=_no_doc,
            adapter_name="generic",
        )
        action = plan.actions[0]
        assert action.status == "resolved"
        assert action.value == "2028-06"

    def test_wrong_year_month_is_blocked(self) -> None:
        """A YYYY-MM whose year contradicts the framed profile evidence must
        fail closed, not fill."""
        plan = build_fill_plan(
            [_month_field(label="Education end month", name="end_month")],
            _FixedClassifier(CanonicalKey.EDUCATION_END_MONTH),
            {"education.graduation_year": 2028, "answer.end_year": 2028},
            answer_lookup=lambda key, label: "2029-06"
            if key == CanonicalKey.EDUCATION_END_MONTH.value
            else None,
            document_lookup=_no_doc,
            adapter_name="generic",
        )
        action = plan.actions[0]
        assert action.value is None
        assert action.status == "blocked"

    def test_restored_positive_after_correction(self) -> None:
        """Correcting the approved month back to the evidence year restores a
        resolved fill (no latch-on-failure)."""
        for month, status, value in (("2029-06", "blocked", None), ("2028-06", "resolved", "2028-06")):
            plan = build_fill_plan(
                [_month_field(label="Education end month", name="end_month")],
                _FixedClassifier(CanonicalKey.EDUCATION_END_MONTH),
                {"education.graduation_year": 2028, "answer.end_year": 2028},
                answer_lookup=lambda key, label, _m=month: _m
                if key == CanonicalKey.EDUCATION_END_MONTH.value
                else None,
                document_lookup=_no_doc,
                adapter_name="generic",
            )
            assert plan.actions[0].status == status
            assert plan.actions[0].value == value

    def test_missing_authoritative_year_fails_closed(self) -> None:
        """A YYYY-MM with no framed graduation-year evidence to check against
        must fail closed rather than fill unverified."""
        plan = build_fill_plan(
            [_month_field(label="Education end month", name="end_month")],
            _FixedClassifier(CanonicalKey.EDUCATION_END_MONTH),
            {},
            answer_lookup=lambda key, label: "2028-06"
            if key == CanonicalKey.EDUCATION_END_MONTH.value
            else None,
            document_lookup=_no_doc,
            adapter_name="generic",
        )
        assert plan.actions[0].value is None
        assert plan.actions[0].status == "blocked"

    def test_graduation_year_stays_evidence_only(self) -> None:
        """An answer-bank YYYY-MM for education.graduation_year must NOT be
        consumed: the key stays evidence-only and the missing profile value
        escalates as before."""
        plan = build_fill_plan(
            [_month_field()],
            DeterministicClassifier(),
            {},
            answer_lookup=lambda key, label: "2028-06"
            if key == CanonicalKey.GRADUATION_YEAR.value
            else None,
            document_lookup=_no_doc,
            adapter_name="generic",
        )
        action = plan.actions[0]
        assert action.mapping.canonical_key == CanonicalKey.GRADUATION_YEAR
        assert action.value is None
        assert action.status == "blocked"


# ---------------------------------------------------------------- invalid

class TestInvalidMonthValues:
    def test_rejects_bad_months_and_junk(self) -> None:
        bad_values = [
            "2028-00",
            "2028-13",
            "2028",
            "2028/06",
            "June 2028",
            " 2028-06",
            "2028-06 ",
            "",
            "not-a-date",
            "2028-6",
            "28-06",
        ]
        for bad in bad_values:
            plan = build_fill_plan(
                [_month_field(label="Education end month", name="end_month")],
                _FixedClassifier(CanonicalKey.EDUCATION_END_MONTH),
                {"education.graduation_year": 2028, "answer.end_year": 2028},
                answer_lookup=lambda key, label, _b=bad: _b
                if key == CanonicalKey.EDUCATION_END_MONTH.value
                else None,
                document_lookup=_no_doc,
                adapter_name="generic",
            )
            assert plan.actions[0].value is None, f"value {bad!r} must not resolve"
            assert plan.actions[0].status == "blocked", f"value {bad!r}"

    def test_bool_profile_value_cannot_fill_month(self) -> None:
        plan = build_fill_plan(
            [_month_field()],
            DeterministicClassifier(),
            {"education.graduation_year": True},
            answer_lookup=_no_lookup,
            document_lookup=_no_doc,
            adapter_name="generic",
        )
        assert plan.actions[0].value is None
        assert plan.actions[0].status == "blocked"


# ------------------------------------------------- non-month controls kept

class TestRegularYearControlsUnchanged:
    def test_select_year_control_still_resolves(self) -> None:
        """Non-month year controls must keep accepting legitimate years."""
        plan = build_fill_plan(
            [_year_select_field()],
            DeterministicClassifier(),
            {"education.graduation_year": "2028"},
            answer_lookup=_no_lookup,
            document_lookup=_no_doc,
            adapter_name="generic",
        )
        action = plan.actions[0]
        assert action.mapping.canonical_key == CanonicalKey.GRADUATION_YEAR
        assert action.status == "resolved"
        assert action.value == "2028"

    def test_optional_missing_month_is_omitted(self) -> None:
        """Optional month control with no evidence at all stays omitted."""
        plan = build_fill_plan(
            [_month_field(label="Education end month", name="end_month", required=False)],
            _FixedClassifier(CanonicalKey.EDUCATION_END_MONTH),
            {},
            answer_lookup=_no_lookup,
            document_lookup=_no_doc,
            adapter_name="generic",
        )
        assert plan.actions[0].value is None
        assert plan.actions[0].status == "omitted"
