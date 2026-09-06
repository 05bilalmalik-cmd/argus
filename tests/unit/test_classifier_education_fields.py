"""Regression tests: classifier behaviour for Greenhouse-style School and
End-date fields (the 2026-08-23 Schonfeld dry-run blockers).

Contract updated 2026-08-24 (forensic root cause 12): bare "school" and
bare "start date"/"end date" labels are ambiguous when multiple education or
employment records exist.  They must NOT map deterministically to a single
canonical key; they land in UNKNOWN at low confidence so the fill planner
fails closed and the human decides.

Unambiguous forms still map: an explicit "University / institution" label
maps to UNIVERSITY, and graduation-specific wording ("Expected graduation
year", "Completion year") still maps to GRADUATION_YEAR.
"""
from __future__ import annotations

import pytest

from app.automation.classifier import DeterministicClassifier
from app.domain.questions import CanonicalKey, FormQuestion, Sensitivity


def _classify(label: str, field_type: str = "text", required: bool = True):
    return DeterministicClassifier().classify(
        FormQuestion(label=label, field_type=field_type, required=required)
    )


@pytest.mark.parametrize(
    "label",
    ["School*", "School Name", "school"],
)
def test_bare_school_is_ambiguous_not_university(label: str) -> None:
    mapping = _classify(label)
    assert mapping.canonical_key is CanonicalKey.UNKNOWN
    assert mapping.confidence < 0.75


def test_explicit_university_still_maps_to_university() -> None:
    assert _classify("University / institution").canonical_key is (
        CanonicalKey.UNIVERSITY
    )
    assert _classify("Institution").canonical_key is CanonicalKey.UNIVERSITY


@pytest.mark.parametrize(
    "label",
    [
        "If you attend school in Poland, is your university public?*",
        "If your university is not public, confirm that it appears on the approved list.*",
    ],
)
def test_university_policy_questions_are_not_filled_with_the_university_name(
    label: str,
) -> None:
    mapping = _classify(label, "select")
    assert mapping.canonical_key is CanonicalKey.UNKNOWN
    assert mapping.confidence < 0.75


@pytest.mark.parametrize(
    ("label", "ftype"),
    [
        ("End date month*", "select"),
        ("End date year*", "select"),
        ("End date", "text"),
    ],
)
def test_bare_end_date_is_ambiguous(label: str, ftype: str) -> None:
    mapping = _classify(label, ftype)
    assert mapping.canonical_key is CanonicalKey.UNKNOWN
    assert mapping.confidence < 0.75


def test_bare_start_date_is_ambiguous() -> None:
    mapping = _classify("Start date year*", "select")
    assert mapping.canonical_key is CanonicalKey.UNKNOWN


def test_graduation_wording_still_maps() -> None:
    assert _classify("Expected graduation year", "select").canonical_key is (
        CanonicalKey.GRADUATION_YEAR
    )
    assert _classify("Completion year").canonical_key is CanonicalKey.GRADUATION_YEAR


def test_referral_source_does_not_map_to_motivation() -> None:
    mapping = _classify("How did you hear about us?", "select")
    assert mapping.canonical_key is not CanonicalKey.MOTIVATION


def test_captcha_still_blocks() -> None:
    assert _classify("Verify you are human", "captcha").canonical_key is (
        CanonicalKey.CAPTCHA
    )
