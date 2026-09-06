from __future__ import annotations

import importlib

import pytest


def _scope_module():
    try:
        return importlib.import_module("app.domain.opportunity_scope")
    except ModuleNotFoundError:
        pytest.fail("Phase 11 opportunity-scope policy is not implemented")


@pytest.mark.parametrize(
    ("title", "programme_evidence", "expected_reason"),
    [
        ("2027 Graduate Scheme", "", "graduate_scheme"),
        ("Graduate Programme - Markets", "", "graduate_programme"),
        ("Graduate Program - Markets", "", "graduate_programme"),
        ("Graduate Analyst - Investment Banking", "summer", "graduate_analyst"),
        ("Full-Time Analyst", "", "full_time_analyst"),
        ("School Leaver Finance Role", "", "school_leaver"),
        ("School Leaver Internship", "", "school_leaver"),
        ("School Leaver Placement", "", "school_leaver"),
        ("Finance Apprenticeship", "", "apprenticeship"),
        ("Undergraduate Apprenticeship", "", "apprenticeship"),
        ("Internship Apprenticeship", "", "apprenticeship"),
        ("Analyst Development", "graduate_scheme", "graduate_scheme"),
        ("Analyst Development", "full_time", "full_time"),
        ("Summer Internship", "school_leaver", "school_leaver"),
    ],
)
def test_out_of_scope_programme_detector_names_explicit_exclusion(
    title: str,
    programme_evidence: str,
    expected_reason: str,
) -> None:
    scope = _scope_module()

    assert (
        scope.out_of_scope_programme_reason(title, programme_evidence)
        == expected_reason
    )


@pytest.mark.parametrize(
    "title",
    [
        "Graduate One Year Business Placement 2027",
        "Commercial Real Estate Debt Graduate Intern (6 Months)",
        "Graduate Internship Programme",
        "Actuarial Undergraduate Placement",
        "Prudential Risk Undergraduate Internship",
        "Summer Analyst Programme 2027",
        "Full-Time 6 months Internship",
        "Finance Apprenticeship Placement Year",
    ],
)
def test_in_scope_title_evidence_outranks_ambiguous_words(title: str) -> None:
    scope = _scope_module()

    assert scope.out_of_scope_programme_reason(title, "summer") is None


def test_placement_is_the_only_apprenticeship_exception() -> None:
    scope = _scope_module()

    assert (
        scope.out_of_scope_programme_reason(
            "Finance Placement Year",
            "apprenticeship",
        )
        is None
    )


@pytest.mark.parametrize(
    ("location", "expected_disposition", "expected_reason"),
    [
        ("", "keep_unknown", "location_blank"),
        ("   ", "keep_unknown", "location_blank"),
        ("London", "keep_uk", "uk_location"),
        ("Bristol", "keep_uk", "uk_location"),
        ("London; Amsterdam", "keep_uk", "uk_location"),
        ("New York, London, or Paris", "keep_uk", "uk_location"),
        ("Scotland", "keep_uk", "uk_location"),
        ("N. Ireland", "keep_uk", "uk_location"),
        ("Chicago, IL", "archive_non_uk", "non_uk_location"),
        ("Hong Kong", "archive_non_uk", "non_uk_location"),
        ("Madrid, Spain", "archive_non_uk", "non_uk_location"),
        ("São Paulo, Brazil", "archive_non_uk", "non_uk_location"),
        ("New York", "archive_non_uk", "non_uk_location"),
        ("Atlantis", "keep_unknown", "location_unknown"),
    ],
)
def test_location_scope_is_uk_first_and_unknown_safe(
    location: str,
    expected_disposition: str,
    expected_reason: str,
) -> None:
    scope = _scope_module()

    decision = scope.classify_location_scope(location)

    assert decision.disposition.value == expected_disposition
    assert decision.reason == expected_reason
