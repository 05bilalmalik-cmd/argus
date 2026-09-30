"""Question identity must not be replaced by help/options or credential labels."""
import pytest

from app.automation.adapters.generic import _enrich_label_for_classifier
from app.automation.classifier import DeterministicClassifier
from app.automation.field_context import extract_field_context
from app.domain.questions import CanonicalKey as K, FormQuestion, Sensitivity as S


@pytest.mark.parametrize("label,key,sensitivity", [
    ("Verify that you are human", K.CAPTCHA, S.ASSESSMENT),
    ("Begin online assessment", K.ASSESSMENT, S.ASSESSMENT),
    ("Criminal record", K.CRIMINAL_RECORD, S.LEGAL),
    ("I certify the information provided is accurate", K.LEGAL_ATTESTATION, S.LEGAL),
])
def test_password_type_precedes_keywords_without_changing_ordinary_fields(label, key, sensitivity):
    classifier = DeterministicClassifier()
    ordinary = FormQuestion(label, "text", name="question")
    before = classifier.classify(ordinary)
    assert (before.canonical_key, before.sensitivity) == (key, sensitivity)
    password = classifier.classify(FormQuestion(label, "password", name="password"))
    assert (password.canonical_key, password.sensitivity) == (K.ACCOUNT_PASSWORD, S.SENSITIVE)
    assert classifier.classify(ordinary) == before


@pytest.mark.parametrize("transport", ["options", "linked_help"])
def test_source_phrase_in_context_does_not_replace_primary_linkedin_question(transport):
    classifier = DeterministicClassifier()
    if transport == "options":
        options = ("How did you find us?", "Other")
        label = _enrich_label_for_classifier("LinkedIn Profile URL", "select", options, "")
        question = FormQuestion(label, "select", name="linkedin", options=options)
    else:
        html = '<form><label for="linkedin">LinkedIn Profile URL</label><input id="linkedin" name="linkedin" type="url" aria-describedby="guide"><small id="guide">How did you find us?</small></form>'
        context = extract_field_context(html, html, selector="#linkedin")
        assert context.label == "LinkedIn Profile URL"
        assert "How did you find us?" in context.help_text
        question = context.to_form_question()
    result = classifier.classify(question)
    assert (result.canonical_key, result.sensitivity) == (K.LINKEDIN, S.STANDARD)


@pytest.mark.parametrize("label,name", [
    ("How did you hear about us?", "source"),
    ("Referral source", "referral"),
    ("How did you find us?", "field17"),
    ("Source", "field17"),
])
def test_genuine_source_identity_survives_brand_options(label, name):
    options = ("LinkedIn", "GitHub", "Other")
    enriched = _enrich_label_for_classifier(label, "select", options, "")
    result = DeterministicClassifier().classify(FormQuestion(enriched, "select", name=name, options=options))
    assert (result.canonical_key, result.sensitivity) == (K.SOURCE, S.STANDARD)
