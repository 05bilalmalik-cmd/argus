from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.automation.runner import _OwnerThreadJourney, build_fill_plan, redact_answer_preview
from app.automation.types import FillPlan, InspectedField, RunMode
from app.domain.questions import (
    CanonicalKey,
    FormQuestion,
    QuestionMapping,
    Sensitivity,
)
from app.domain.targets import TargetKind


REJECTED_SENTINEL = "REJECTED-CANDIDATE-VALUE-78421"


class _FixedClassifier:
    def __init__(self, key: CanonicalKey) -> None:
        self.key = key

    def classify(self, _question: FormQuestion) -> QuestionMapping:
        return QuestionMapping(
            canonical_key=self.key,
            confidence=0.99,
            sensitivity=Sensitivity.STANDARD,
            reason="literal test mapping",
        )


def _field(
    label: str,
    *,
    name: str = "",
    placeholder: str = "",
    field_type: str = "text",
    control_type: str | None = None,
    required: bool = True,
    options: tuple[str, ...] = (),
) -> InspectedField:
    return InspectedField(
        selector=f"#{name or 'field'}",
        control_type=control_type or field_type,
        question=FormQuestion(
            label=label,
            name=name,
            placeholder=placeholder,
            field_type=field_type,
            required=required,
            options=options,
        ),
    )


def _plan(
    field: InspectedField,
    *,
    offered: object,
    mapping_key: CanonicalKey,
    known_values: dict[str, object] | None = None,
) -> FillPlan:
    profile_values = dict(known_values or {})
    profile_values.setdefault(mapping_key.value, offered)
    return build_fill_plan(
        [field],
        _FixedClassifier(mapping_key),
        profile_values,
        answer_lookup=lambda _key, _label: offered,
        document_lookup=lambda _key: offered,
        adapter_name="greenhouse",
    )


def _assert_rejected(plan: FillPlan, *, required: bool = True) -> None:
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.value is None
    assert action.source == "plausibility_guard"
    assert action.status == ("blocked" if required else "omitted")
    findings = [
        finding
        for finding in plan.risk.findings
        if finding.code == "implausible_field_value"
    ]
    assert len(findings) == 1
    assert REJECTED_SENTINEL not in repr(action)
    assert REJECTED_SENTINEL not in repr(findings[0])
    assert redact_answer_preview(action) == ""


@pytest.mark.parametrize(
    ("mapping_key", "known_key"),
    [
        (CanonicalKey.DEGREE, "education.degree"),
        (CanonicalKey.MOTIVATION, "education.discipline"),
        (CanonicalKey.UNIVERSITY, "education.university"),
        (CanonicalKey.FIRST_NAME, "identity.first_name"),
    ],
)
def test_grade_control_rejects_known_non_grade_profile_categories(
    mapping_key: CanonicalKey,
    known_key: str,
) -> None:
    plan = _plan(
        _field("Current GPA / grade", name="candidate_gpa"),
        offered=REJECTED_SENTINEL,
        mapping_key=mapping_key,
        known_values={known_key: REJECTED_SENTINEL},
    )

    _assert_rejected(plan)


def test_aquatic_like_gpa_control_rejects_known_degree() -> None:
    degree = f"BSc {REJECTED_SENTINEL} Computer Science"
    plan = _plan(
        _field("Degree / GPA", name="degree_gpa"),
        offered=degree,
        mapping_key=CanonicalKey.DEGREE,
        known_values={"education.degree": degree},
    )

    assert plan.actions[0].value is None
    assert "implausible_field_value" in plan.risk.blocking_codes
    assert degree not in repr(plan.risk.findings)


@pytest.mark.parametrize(
    ("mapping_key", "known_key"),
    [
        (CanonicalKey.UNIVERSITY, "education.university"),
        (CanonicalKey.FIRST_NAME, "identity.first_name"),
        (CanonicalKey.DEGREE, "education.degree"),
        (CanonicalKey.EMAIL, "contact.email"),
        (CanonicalKey.LINKEDIN, "contact.linkedin"),
    ],
)
def test_date_control_rejects_identity_education_and_contact_values(
    mapping_key: CanonicalKey,
    known_key: str,
) -> None:
    plan = _plan(
        _field("Expected completion date", name="completion_date", field_type="date"),
        offered=REJECTED_SENTINEL,
        mapping_key=mapping_key,
        known_values={known_key: REJECTED_SENTINEL},
    )

    _assert_rejected(plan)


def test_drw_like_finishing_studies_control_rejects_university() -> None:
    university = f"{REJECTED_SENTINEL} University"
    plan = _plan(
        _field(
            "When will you be finishing university studies?",
            name="finishing_university_studies",
        ),
        offered=university,
        mapping_key=CanonicalKey.UNIVERSITY,
        known_values={"education.university": university},
    )

    assert plan.actions[0].value is None
    assert plan.actions[0].source == "plausibility_guard"
    assert university not in repr(plan.risk.findings)


@pytest.mark.parametrize(
    ("label", "name"),
    [
        ("First name", "first_name"),
        ("Last name", "last_name"),
        ("Full legal name", "full_name"),
    ],
)
@pytest.mark.parametrize(
    ("mapping_key", "known_key"),
    [
        (CanonicalKey.GRADUATION_YEAR, "education.graduation_year"),
        (CanonicalKey.EMAIL, "contact.email"),
        (CanonicalKey.LINKEDIN, "contact.linkedin"),
        (CanonicalKey.DEGREE, "education.degree"),
        (CanonicalKey.UNIVERSITY, "education.university"),
    ],
)
def test_name_controls_reject_date_contact_and_education_values(
    label: str,
    name: str,
    mapping_key: CanonicalKey,
    known_key: str,
) -> None:
    plan = _plan(
        _field(label, name=name),
        offered=REJECTED_SENTINEL,
        mapping_key=mapping_key,
        known_values={known_key: REJECTED_SENTINEL},
    )

    _assert_rejected(plan)


def test_exact_option_control_rejects_unmatched_text() -> None:
    plan = _plan(
        _field(
            "Will you require sponsorship?",
            name="sponsorship",
            field_type="select",
            options=("Yes", "No"),
        ),
        offered=REJECTED_SENTINEL,
        mapping_key=CanonicalKey.SPONSORSHIP,
    )

    _assert_rejected(plan)


def test_optional_implausible_value_is_omitted_and_flagged_without_value() -> None:
    plan = _plan(
        _field("GPA", name="gpa", required=False),
        offered=REJECTED_SENTINEL,
        mapping_key=CanonicalKey.DEGREE,
        known_values={"education.degree": REJECTED_SENTINEL},
    )

    _assert_rejected(plan, required=False)


def test_discipline_control_rejects_entire_known_degree() -> None:
    degree = f"BSc {REJECTED_SENTINEL} with Honours"
    plan = _plan(
        _field("Course / discipline", name="field_of_study"),
        offered=degree,
        mapping_key=CanonicalKey.DEGREE,
        known_values={"education.degree": degree},
    )

    assert plan.actions[0].value is None
    assert degree not in repr(plan.risk.findings)


@pytest.mark.parametrize(
    "grade",
    ["3.8", "85%", "2:1", "First-Class"],
)
def test_valid_grade_shapes_remain_resolved(grade: str) -> None:
    plan = _plan(
        _field("Current GPA / grade", name="gpa"),
        offered=grade,
        mapping_key=CanonicalKey.MOTIVATION,
    )

    assert plan.actions[0].value == grade
    assert plan.actions[0].status == "resolved"
    assert "implausible_field_value" not in plan.risk.blocking_codes


@pytest.mark.parametrize(
    ("label", "field_type", "value"),
    [
        ("Graduation year", "text", "2027"),
        ("Completion date", "date", "2027-06-30"),
        ("Graduation month", "month", "2027-06"),
        ("Completion month and year", "text", "June 2027"),
    ],
)
def test_valid_date_and_year_shapes_remain_resolved(
    label: str,
    field_type: str,
    value: str,
) -> None:
    plan = _plan(
        _field(label, name="completion", field_type=field_type),
        offered=value,
        mapping_key=CanonicalKey.GRADUATION_YEAR,
    )

    assert plan.actions[0].value == value
    assert plan.actions[0].status == "resolved"


@pytest.mark.parametrize(
    ("label", "name", "mapping_key", "value"),
    [
        ("First name", "first_name", CanonicalKey.FIRST_NAME, "Alex"),
        ("Last name", "last_name", CanonicalKey.LAST_NAME, "O'Neill"),
        ("Full name", "full_name", CanonicalKey.FULL_NAME, "Anne-Marie O'Neill"),
    ],
)
def test_realistic_names_remain_resolved(
    label: str,
    name: str,
    mapping_key: CanonicalKey,
    value: str,
) -> None:
    plan = _plan(
        _field(label, name=name),
        offered=value,
        mapping_key=mapping_key,
    )

    assert plan.actions[0].value == value
    assert plan.actions[0].status == "resolved"


@pytest.mark.parametrize(
    ("label", "name", "mapping_key", "value"),
    [
        (
            "University / institution",
            "university",
            CanonicalKey.UNIVERSITY,
            "Example University",
        ),
        ("Degree qualification", "degree", CanonicalKey.DEGREE, "BSc Computer Science"),
    ],
)
def test_valid_education_values_remain_resolved(
    label: str,
    name: str,
    mapping_key: CanonicalKey,
    value: str,
) -> None:
    plan = _plan(
        _field(label, name=name),
        offered=value,
        mapping_key=mapping_key,
    )

    assert plan.actions[0].value == value
    assert plan.actions[0].status == "resolved"


def test_exact_option_matches_case_insensitively_and_uses_provider_casing() -> None:
    plan = _plan(
        _field(
            "Will you require sponsorship?",
            name="sponsorship",
            field_type="radio",
            options=("Yes", "No"),
        ),
        offered="yes",
        mapping_key=CanonicalKey.SPONSORSHIP,
    )

    assert plan.actions[0].value == "Yes"
    assert plan.actions[0].status == "resolved"


def test_exact_option_membership_outweighs_free_text_year_heuristic() -> None:
    plan = _plan(
        _field(
            "Current year of study",
            name="study_year",
            field_type="select",
            options=("First Year", "Second Year", "Third Year"),
        ),
        offered="second year",
        mapping_key=CanonicalKey.MOTIVATION,
    )

    assert plan.actions[0].value == "Second Year"
    assert plan.actions[0].status == "resolved"


def test_academic_year_exact_option_is_not_mistaken_for_a_calendar_date() -> None:
    plan = _plan(
        _field(
            "Current academic year",
            name="academic_year",
            field_type="select",
            options=("First Year", "Second Year", "Third Year"),
        ),
        offered="Third Year",
        mapping_key=CanonicalKey.MOTIVATION,
    )

    assert plan.actions[0].value == "Third Year"
    assert plan.actions[0].status == "resolved"


def test_study_level_kind_precedes_university_for_current_year_wording() -> None:
    university = "Example University"
    plan = _plan(
        _field(
            "Current year in university",
            name="current_year_in_university",
        ),
        offered=university,
        mapping_key=CanonicalKey.UNIVERSITY,
        known_values={"education.university": university},
    )

    action = plan.actions[0]
    assert action.value is None
    assert action.source == "plausibility_guard"
    assert action.status == "blocked"
    finding = next(
        item for item in plan.risk.findings if item.code == "implausible_field_value"
    )
    assert "study_level" in finding.reason
    assert university not in finding.reason


@pytest.mark.parametrize(
    "value",
    [
        "Year 1",
        "Year 2",
        "Year 3",
        "Year 4",
        "Year 5",
        "1st Year",
        "2nd Year",
        "3rd Year",
        "4th Year",
        "5th Year",
        "First Year",
        "Second Year",
        "Third Year",
        "Fourth Year",
        "Fifth Year",
        "Final Year",
        "Penultimate Year",
    ],
)
def test_closed_free_text_study_levels_remain_resolved(value: str) -> None:
    plan = _plan(
        _field(
            "Current year at university",
            name="current_year_at_university",
        ),
        offered=value,
        mapping_key=CanonicalKey.MOTIVATION,
    )

    assert plan.actions[0].value == value
    assert plan.actions[0].status == "resolved"


def test_free_text_study_level_rejects_text_outside_closed_shapes() -> None:
    value = "Third-ish year maybe"
    plan = _plan(
        _field(
            "Current year at university",
            name="current_year_at_university",
        ),
        offered=value,
        mapping_key=CanonicalKey.MOTIVATION,
    )

    assert plan.actions[0].value is None
    assert plan.actions[0].source == "plausibility_guard"
    assert value not in repr(plan.risk.findings)


def test_explicit_date_word_is_not_masked_by_study_year_phrase() -> None:
    plan = _plan(
        _field("Study year end date", name="study_year_end_date"),
        offered=REJECTED_SENTINEL,
        mapping_key=CanonicalKey.FIRST_NAME,
        known_values={"identity.first_name": REJECTED_SENTINEL},
    )

    _assert_rejected(plan)


def test_study_year_end_wording_keeps_date_kind_precedence() -> None:
    value = "Example University"
    plan = _plan(
        _field("Study year end", name="study_year_end"),
        offered=value,
        mapping_key=CanonicalKey.UNIVERSITY,
        known_values={"education.university": value},
    )

    finding = next(
        item for item in plan.risk.findings if item.code == "implausible_field_value"
    )
    assert plan.actions[0].value is None
    assert "date field" in finding.reason
    assert value not in finding.reason


def test_date_control_rejects_calendar_shaped_known_phone() -> None:
    value = "2027"
    plan = _plan(
        _field("Completion year", name="completion_year"),
        offered=value,
        mapping_key=CanonicalKey.PHONE,
        known_values={"contact.phone": value},
    )

    action = plan.actions[0]
    assert action.value is None
    assert action.source == "plausibility_guard"
    assert action.status == "blocked"
    assert value not in repr(plan.risk.findings)


@pytest.mark.parametrize(
    ("label", "name", "mapping_key", "value"),
    [
        ("Email address", "email", CanonicalKey.EMAIL, "candidate@example.test"),
        (
            "Telephone number",
            "phone",
            CanonicalKey.PHONE,
            "".join(("+", "44", " (", "0", ")", "20", " ", "7946", " ", "0958")),
        ),
        (
            "LinkedIn URL",
            "linkedin_url",
            CanonicalKey.LINKEDIN,
            "https://www.linkedin.com/in/candidate",
        ),
    ],
)
def test_valid_contact_shapes_remain_resolved(
    label: str,
    name: str,
    mapping_key: CanonicalKey,
    value: str,
) -> None:
    plan = _plan(
        _field(label, name=name),
        offered=value,
        mapping_key=mapping_key,
    )

    assert plan.actions[0].value == value
    assert plan.actions[0].status == "resolved"


@pytest.mark.parametrize(
    "value",
    [
        "https://www.linkedin.com/in/candidate",
        "https://xn--bcher-kva.de/profile",
        "http://sub-domain.example.com:8080/path?source=profile",
    ],
)
def test_structurally_valid_public_dns_urls_remain_resolved(value: str) -> None:
    plan = _plan(
        _field("Portfolio URL", name="portfolio_url"),
        offered=value,
        mapping_key=CanonicalKey.LINKEDIN,
    )

    assert plan.actions[0].value == value
    assert plan.actions[0].status == "resolved"


@pytest.mark.parametrize(
    "value",
    [
        "https://-bad.example/profile",
        "https://bad-.example/profile",
        "https://localhost/profile",
        "https://intranet/profile",
        "https://service.internal/profile",
        "https://127.0.0.1/profile",
        "https://127.000.000.001/profile",
        "https://0x7f.0.0.1/profile",
        "https://127.0.0.0x1/profile",
        "https://0177.0.0.1/profile",
        "https://[2001:db8::1]/profile",
        "https://bad_label.example/profile",
    ],
)
def test_url_control_rejects_invalid_private_or_ip_hosts(value: str) -> None:
    plan = _plan(
        _field("Portfolio URL", name="portfolio_url"),
        offered=value,
        mapping_key=CanonicalKey.LINKEDIN,
    )

    assert plan.actions[0].value is None
    assert plan.actions[0].source == "plausibility_guard"
    assert value not in repr(plan.risk.findings)


@pytest.mark.parametrize(
    ("label", "name", "mapping_key", "value"),
    [
        ("Email address", "email", CanonicalKey.EMAIL, ".candidate@example.test"),
        ("Telephone number", "phone", CanonicalKey.PHONE, "0000000"),
        (
            "Portfolio URL",
            "portfolio_url",
            CanonicalKey.LINKEDIN,
            "https://candidate:secret@example.test/profile",
        ),
    ],
)
def test_explicit_contact_controls_reject_malformed_or_credentialed_values(
    label: str,
    name: str,
    mapping_key: CanonicalKey,
    value: str,
) -> None:
    plan = _plan(
        _field(label, name=name),
        offered=value,
        mapping_key=mapping_key,
    )

    assert plan.actions[0].value is None
    assert plan.actions[0].source == "plausibility_guard"
    assert value not in repr(plan.risk.findings)


@pytest.mark.parametrize("value", ["O’Neill", "DʼAngelo"])
def test_names_accept_standard_unicode_apostrophes(value: str) -> None:
    plan = _plan(
        _field("Last name", name="last_name"),
        offered=value,
        mapping_key=CanonicalKey.LAST_NAME,
    )

    assert plan.actions[0].value == value
    assert plan.actions[0].status == "resolved"


def test_file_action_remains_resolved() -> None:
    plan = _plan(
        _field("Upload CV", name="resume", field_type="file"),
        offered=r"C:\approved\candidate-cv.pdf",
        mapping_key=CanonicalKey.CV,
    )

    assert plan.actions[0].value == r"C:\approved\candidate-cv.pdf"
    assert plan.actions[0].status == "resolved"


def test_prefill_adapter_receives_only_plausible_resolved_actions() -> None:
    invalid = _field("GPA", name="gpa")
    valid = _field("Full name", name="full_name")
    invalid_plan = _plan(
        invalid,
        offered=REJECTED_SENTINEL,
        mapping_key=CanonicalKey.DEGREE,
        known_values={"education.degree": REJECTED_SENTINEL},
    )
    valid_plan = _plan(
        valid,
        offered="Demo Candidate",
        mapping_key=CanonicalKey.FULL_NAME,
    )
    plan = FillPlan(
        actions=(invalid_plan.actions[0], valid_plan.actions[0]),
        risk=invalid_plan.risk,
    )

    class _Adapter:
        name = "greenhouse"

        def __init__(self) -> None:
            self.filled: list[tuple[str, str]] = []

        def fill(self, _scope: object, field: InspectedField, value: str) -> None:
            self.filled.append((field.question.name, value))

    class _Page:
        url = "https://job-boards.greenhouse.io/example/jobs/123"

        @staticmethod
        def evaluate(_script: str) -> dict[str, bool]:
            return {"blocked": False}

    adapter = _Adapter()
    page = _Page()
    journey = _OwnerThreadJourney.__new__(_OwnerThreadJourney)
    journey.mode = RunMode.PREFILL
    journey.adapter = adapter
    journey.adapter_name = adapter.name
    journey.step_index = 0
    journey.application_id = "application-id"
    journey.resolution = SimpleNamespace(
        kind=TargetKind.APPLICATION_FORM,
        source_url=page.url,
        final_url=page.url,
        evidence={},
    )
    journey.document_manifest = {}
    journey.active_document_upload = {}
    journey._egress_fatal_probe = None
    journey._inspect = lambda _page: ([invalid, valid], {"root_found": True}, page)
    journey._build_plan = lambda _fields, _evidence: plan
    journey._boundary = lambda _page: (False, "")
    journey._verify_after_fill = lambda _scope, _fields, _values: True

    result = journey._run_steps(page)

    assert result["state"] == "NEEDS_USER"
    assert adapter.filled == [("full_name", "Demo Candidate")]
    assert REJECTED_SENTINEL not in repr(adapter.filled)
