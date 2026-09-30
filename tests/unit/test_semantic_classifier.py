"""Tests for the semantic (non-ATS paraphrase) classifier layer.

The deterministic classifier stays first and authoritative; the semantic
layer only maps fields it leaves UNKNOWN, and must NEVER map to (or answer
for) a sensitive key.
"""

from __future__ import annotations

import os

import pytest

from app.automation.classifier import DeterministicClassifier
from app.automation.semantic_classifier import (
    FLAG_NAME,
    REFUSED_KEYS,
    SemanticClassifier,
    semantic_classifier_enabled,
)
from app.domain.questions import CanonicalKey, FormQuestion, QuestionMapping, Sensitivity


def _q(label: str, field_type: str = "text", **kwargs: object) -> FormQuestion:
    return FormQuestion(label, field_type, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Home post code", CanonicalKey.POSTCODE),
        ("Mobile telephone", CanonicalKey.PHONE),
        ("Which university did you attend?", CanonicalKey.UNIVERSITY),
        ("Expected date of graduation", CanonicalKey.GRADUATION_YEAR),
    ],
)
def test_realistic_non_ats_phrasings_map_correctly(
    label: str, expected: CanonicalKey
) -> None:
    mapping = SemanticClassifier().classify(_q(label))

    assert mapping.canonical_key is expected
    assert 0.0 <= mapping.confidence <= 1.0
    assert mapping.reason  # human-readable reason required


def test_semantic_layer_resolves_fields_deterministic_leaves_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end chain: deterministic UNKNOWN + flag on -> semantic mapping."""
    monkeypatch.setenv(FLAG_NAME, "1")
    classifier = DeterministicClassifier()
    cases = (
        ("Cell number", CanonicalKey.PHONE),
        ("Alma mater", CanonicalKey.UNIVERSITY),
    )
    for label, expected in cases:
        question = _q(label)
        # Guard: the deterministic layer really does leave these UNKNOWN, so
        # this test proves the semantic layer added value (not an override).
        monkeypatch.delenv(FLAG_NAME, raising=False)
        assert classifier.classify(question).canonical_key is CanonicalKey.UNKNOWN
        monkeypatch.setenv(FLAG_NAME, "1")

        mapping = classifier.classify(question)
        assert mapping.canonical_key is expected


@pytest.mark.parametrize(
    ("label", "field_type", "refused_key"),
    [
        (
            "Have you ever been convicted of a criminal offence?",
            "text",
            CanonicalKey.CRIMINAL_RECORD,
        ),
        (
            "Will you need visa sponsorship now or in the future?",
            "radio",
            CanonicalKey.SPONSORSHIP,
        ),
        (
            "Are you legally authorised to work in the UK?",
            "radio",
            CanonicalKey.WORK_AUTHORISATION,
        ),
        (
            "I certify the information provided is accurate",
            "checkbox",
            CanonicalKey.LEGAL_ATTESTATION,
        ),
        ("What is your gender?", "select", CanonicalKey.DEMOGRAPHIC),
        ("What is your ethnic background?", "select", CanonicalKey.DEMOGRAPHIC),
        ("Begin online assessment", "button", CanonicalKey.ASSESSMENT),
        ("Verify you are human", "captcha", CanonicalKey.CAPTCHA),
    ],
)
def test_sensitive_keys_are_always_refused(
    label: str, field_type: str, refused_key: CanonicalKey
) -> None:
    """MANDATORY: obvious sensitive matches must return UNKNOWN, never a mapping."""
    assert refused_key in REFUSED_KEYS
    mapping = SemanticClassifier().classify(_q(label, field_type))

    assert mapping.canonical_key is CanonicalKey.UNKNOWN
    assert "escalat" in mapping.reason


def test_all_refused_keys_covered() -> None:
    assert REFUSED_KEYS == frozenset(
        {
            CanonicalKey.CRIMINAL_RECORD,
            CanonicalKey.SPONSORSHIP,
            CanonicalKey.WORK_AUTHORISATION,
            CanonicalKey.LEGAL_ATTESTATION,
            CanonicalKey.DEMOGRAPHIC,
            CanonicalKey.ASSESSMENT,
            CanonicalKey.CAPTCHA,
        }
    )


def test_refusal_holds_through_the_chained_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with the flag on, a sensitive best-guess escalates, never maps."""
    monkeypatch.setenv(FLAG_NAME, "true")
    mapping = DeterministicClassifier().classify(
        _q("Have you ever been convicted of a criminal offence?")
    )

    assert mapping.canonical_key is CanonicalKey.UNKNOWN


def test_genuinely_novel_question_returns_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = _q("Do you have any outstanding offers or deadlines?")
    assert SemanticClassifier().classify(question).canonical_key is CanonicalKey.UNKNOWN

    monkeypatch.setenv(FLAG_NAME, "on")
    assert DeterministicClassifier().classify(question).canonical_key is CanonicalKey.UNKNOWN


def test_below_threshold_text_returns_unknown() -> None:
    mapping = SemanticClassifier().classify(_q("Xqzt blorp wobble"))

    assert mapping.canonical_key is CanonicalKey.UNKNOWN


def test_configurable_threshold_forces_unknown() -> None:
    question = _q("Your contact number")
    assert SemanticClassifier().classify(question).canonical_key is CanonicalKey.PHONE

    mapping = SemanticClassifier(threshold=0.95).classify(question)

    assert mapping.canonical_key is CanonicalKey.UNKNOWN


def test_extra_context_string_is_consumed_without_field_context_import() -> None:
    """The optional richer-context hook works as a plain string parameter."""
    question = _q("Entry")
    assert SemanticClassifier().classify(question).canonical_key is CanonicalKey.UNKNOWN

    mapping = SemanticClassifier().classify(question, extra_context="home post code")

    assert mapping.canonical_key is CanonicalKey.POSTCODE


def test_deterministic_mappings_are_never_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confident deterministic mappings (incl. sensitive ones) win flag on/off."""
    cases = (
        (_q("First name", name="first_name"), CanonicalKey.FIRST_NAME),
        (
            _q("Will you now or in future require visa sponsorship?", "radio"),
            CanonicalKey.SPONSORSHIP,
        ),
        (
            _q("I certify that the information provided is accurate", "checkbox"),
            CanonicalKey.LEGAL_ATTESTATION,
        ),
    )
    for question, expected in cases:
        monkeypatch.delenv(FLAG_NAME, raising=False)
        off = DeterministicClassifier().classify(question)
        monkeypatch.setenv(FLAG_NAME, "1")
        on = DeterministicClassifier().classify(question)
        assert off.canonical_key is expected
        assert on == off


def test_flag_off_is_byte_identical_to_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: with the flag off, behaviour is exactly the old mapping."""
    monkeypatch.delenv(FLAG_NAME, raising=False)
    assert semantic_classifier_enabled() is False
    classifier = DeterministicClassifier()

    assert classifier.classify(_q("Cell number")) == QuestionMapping(
        CanonicalKey.UNKNOWN, 0.0, Sensitivity.STANDARD, "No deterministic mapping"
    )
    assert classifier.classify(_q("Tell us something else")) == QuestionMapping(
        CanonicalKey.UNKNOWN, 0.0, Sensitivity.STANDARD, "No deterministic mapping"
    )
    assert (
        classifier.classify(_q("First name", name="first_name")).canonical_key
        is CanonicalKey.FIRST_NAME
    )


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " On "])
def test_flag_truthy_values_enable(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(FLAG_NAME, value)

    assert semantic_classifier_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", " "])
def test_flag_falsy_values_disable(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(FLAG_NAME, value)

    assert semantic_classifier_enabled() is False


def test_flag_defaults_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLAG_NAME, raising=False)
    # os.environ.pop guard: no other code path may enable this by default.
    assert os.environ.get(FLAG_NAME) is None
    assert semantic_classifier_enabled() is False
