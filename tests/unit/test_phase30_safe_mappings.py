from __future__ import annotations

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


def test_explicit_greenhouse_education_and_location_ids_map_to_safe_keys() -> None:
    classifier = DeterministicClassifier()
    cases = (
        (_question("School*", name="school--0", field_type="text"), CanonicalKey.UNIVERSITY),
        (_question("Discipline*", name="discipline--0"), CanonicalKey.EDUCATION_DISCIPLINE),
        (_question("Start date year*", name="start-year--0", field_type="number"), CanonicalKey.EDUCATION_START_YEAR),
        (_question("End date year*", name="end-year--0", field_type="number"), CanonicalKey.EDUCATION_END_YEAR),
        (_question("Current Location*", name="question_current_location"), CanonicalKey.CITY),
        (_question("Location Preference*", name="question_location_preference"), CanonicalKey.WORK_LOCATION),
        (_question("What year of study are you in?", name="current-study-year"), CanonicalKey.STUDY_LEVEL),
        (
            _question(
                "Please select all fields of study that closely align with your education background *",
                name="question_subjects[]",
                field_type="checkbox",
            ),
            CanonicalKey.EDUCATION_SUBJECT,
        ),
    )

    for question, expected_key in cases:
        assert classifier.classify(question).canonical_key is expected_key


def test_ambiguous_bare_education_date_labels_stay_unknown() -> None:
    classifier = DeterministicClassifier()

    assert classifier.classify(_question("School*")).canonical_key is CanonicalKey.UNKNOWN
    assert classifier.classify(_question("Start date year*", field_type="select")).canonical_key is CanonicalKey.UNKNOWN
    assert classifier.classify(_question("End date year*", field_type="select")).canonical_key is CanonicalKey.UNKNOWN


def test_availability_maps_to_approved_availability_key_not_education_start() -> None:
    mapping = DeterministicClassifier().classify(
        _question("When are you available to start?", name="available_from")
    )

    assert mapping.canonical_key is CanonicalKey.AVAILABLE_FROM


def test_github_maps_to_its_own_profile_key_and_does_not_fall_back_to_linkedin() -> None:
    mapping = DeterministicClassifier().classify(
        _question("What is your Github username?", name="github")
    )

    assert mapping.canonical_key is CanonicalKey.GITHUB


def test_programme_tier_conflict_blanks_graduation_derived_fields() -> None:
    field = _field(
        _question(
            "What year are you expected to graduate?",
            name="graduation_year",
            field_type="select",
            options=("2028", "2029"),
        )
    )
    plan = build_fill_plan(
        [field],
        DeterministicClassifier(),
        {
            CanonicalKey.PROGRAMME_GRADUATION_CONFLICT.value: True,
            CanonicalKey.GRADUATION_YEAR.value: "2029",
        },
        answer_lookup=lambda _key, _label: "2029",
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    action = plan.actions[0]
    assert action.value is None
    assert action.source == "graduation_tier_guard"
    assert action.status == "blocked"
    assert "programme_tier_graduation_conflict" in plan.risk.blocking_codes


def test_legal_declaration_without_exact_profile_match_stays_blank() -> None:
    field = _field(_question("I certify that the information provided is accurate."))
    plan = build_fill_plan(
        [field],
        DeterministicClassifier(),
        {},
        answer_lookup=lambda _key, _label: "Yes",
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    action = plan.actions[0]
    assert action.mapping.canonical_key is CanonicalKey.LEGAL_ATTESTATION
    assert action.value is None
    assert action.source == "human"


def test_known_mapping_with_no_stored_value_stays_blank() -> None:
    field = _field(
        _question("Start date year*", name="start-year--0", field_type="number")
    )
    plan = build_fill_plan(
        [field],
        DeterministicClassifier(),
        {},
        answer_lookup=lambda _key, _label: None,
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    action = plan.actions[0]
    assert action.value is None
    assert action.source == "missing"
    assert "required_answer_missing" in plan.risk.blocking_codes


def test_full_profile_degree_is_normalised_to_one_unique_provider_option() -> None:
    field = _field(
        _question(
            "Degree*",
            name="degree--0",
            options=("Bachelors", "Bachelor's Degree", "Master's Degree"),
        )
    )
    plan = build_fill_plan(
        [field],
        DeterministicClassifier(),
        {CanonicalKey.DEGREE.value: "Bachelor of Science, Finance"},
        answer_lookup=lambda _key, _label: None,
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    assert plan.actions[0].value == "Bachelor's Degree"
    assert plan.actions[0].source == "approved_profile"


def test_subject_checkbox_only_resolves_the_matching_option() -> None:
    matching = _field(
        FormQuestion(
            label="Fields of study",
            field_type="checkbox",
            name="subjects[]",
            required=True,
            option_label="Finance",
        )
    )
    non_matching = _field(
        FormQuestion(
            label="Fields of study",
            field_type="checkbox",
            name="subjects[]",
            required=True,
            option_label="Economics",
        ),
        selector="#other",
    )
    plan = build_fill_plan(
        [matching, non_matching],
        DeterministicClassifier(),
        {},
        answer_lookup=lambda _key, _label: "Finance",
        document_lookup=lambda _key: None,
        adapter_name="greenhouse",
    )

    assert plan.actions[0].value == "Finance"
    assert plan.actions[0].status == "resolved"
    assert plan.actions[1].value is None
    assert plan.actions[1].source == "option_not_selected"
    assert plan.actions[1].status == "omitted"
