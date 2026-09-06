import pytest

from app.automation.classifier import DeterministicClassifier
from app.domain.questions import CanonicalKey, FormQuestion, Sensitivity


@pytest.mark.parametrize(
    ("question", "expected", "sensitivity"),
    [
        (FormQuestion("First name", "text", name="first_name", required=True), CanonicalKey.FIRST_NAME, Sensitivity.STANDARD),
        (FormQuestion("Email address", "email", required=True), CanonicalKey.EMAIL, Sensitivity.STANDARD),
        (FormQuestion("Mobile phone number", "tel"), CanonicalKey.PHONE, Sensitivity.STANDARD),
        (FormQuestion("University / institution", "text"), CanonicalKey.UNIVERSITY, Sensitivity.STANDARD),
        (FormQuestion("Expected graduation year", "select"), CanonicalKey.GRADUATION_YEAR, Sensitivity.STANDARD),
        (FormQuestion("Upload your CV or résumé", "file", required=True), CanonicalKey.CV, Sensitivity.STANDARD),
        (FormQuestion("Cover letter", "file"), CanonicalKey.COVER_LETTER, Sensitivity.STANDARD),
        (FormQuestion("Will you now or in future require visa sponsorship?", "radio", required=True), CanonicalKey.SPONSORSHIP, Sensitivity.LEGAL),
        (FormQuestion("Are you legally authorised to work in the UK?", "radio", required=True), CanonicalKey.WORK_AUTHORISATION, Sensitivity.LEGAL),
        (FormQuestion("I certify that the information provided is accurate", "checkbox", required=True), CanonicalKey.LEGAL_ATTESTATION, Sensitivity.LEGAL),
        (FormQuestion("What is your ethnic background?", "select"), CanonicalKey.DEMOGRAPHIC, Sensitivity.SENSITIVE),
        (FormQuestion("Why are you interested in private credit?", "textarea", required=True), CanonicalKey.MOTIVATION, Sensitivity.STANDARD),
        (FormQuestion("Begin online assessment", "button"), CanonicalKey.ASSESSMENT, Sensitivity.ASSESSMENT),
        (FormQuestion("Verify you are human", "captcha"), CanonicalKey.CAPTCHA, Sensitivity.ASSESSMENT),
        (FormQuestion("Password", "password", required=True), CanonicalKey.ACCOUNT_PASSWORD, Sensitivity.SENSITIVE),
        (FormQuestion("Verify New Password", "password", required=True), CanonicalKey.ACCOUNT_PASSWORD, Sensitivity.SENSITIVE),
    ],
)
def test_deterministic_classifier_maps_known_question_families(
    question: FormQuestion, expected: CanonicalKey, sensitivity: Sensitivity
) -> None:
    mapping = DeterministicClassifier().classify(question)

    assert mapping.canonical_key == expected
    assert mapping.sensitivity == sensitivity
    assert mapping.confidence >= 0.9


def test_unknown_question_fails_closed() -> None:
    mapping = DeterministicClassifier().classify(
        FormQuestion("Tell us something else", "text", required=True)
    )

    assert mapping.canonical_key == CanonicalKey.UNKNOWN
    assert mapping.confidence == 0.0
