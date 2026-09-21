from datetime import date, datetime
from email.message import EmailMessage

import pytest

from app.services.email import classify_email, extract_deadline, parse_eml


def eml(subject: str, body: str, *, sender: str = "recruiting@example.test") -> bytes:
    message = EmailMessage()
    message["Message-ID"] = "<test-123@example.test>"
    message["From"] = sender
    message["To"] = "demo.candidate@example.test"
    message["Subject"] = subject
    message["Date"] = "Sat, 22 Aug 2026 09:30:00 +0100"
    message.set_content(body)
    return message.as_bytes()


def test_parse_eml_extracts_headers_plain_text_and_aware_timestamp() -> None:
    parsed = parse_eml(eml("Application received", "Thank you for applying."))

    assert parsed.message_id == "<test-123@example.test>"
    assert parsed.sender == "recruiting@example.test"
    assert parsed.subject == "Application received"
    assert parsed.body_text.strip() == "Thank you for applying."
    assert parsed.received_at.tzinfo is not None


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("Application received", "Thank you for applying. We received your application.", "confirmation"),
        ("Complete your online assessment", "Please complete the numerical reasoning test.", "assessment"),
        ("HireVue interview invitation", "Record your video responses using HireVue.", "hirevue"),
        ("Interview invitation", "We would like to invite you to a first-round interview.", "interview"),
        ("Update on your application", "We regret to inform you that we will not proceed.", "rejection"),
    ],
)
def test_email_classifier_recognises_recruitment_events(
    subject: str, body: str, expected: str
) -> None:
    parsed = parse_eml(eml(subject, body))

    assert classify_email(parsed).kind == expected


def test_deadline_extractor_handles_absolute_and_relative_uk_dates() -> None:
    absolute = extract_deadline("Complete this by 17:00 on 25 August 2026.", date(2026, 8, 22))
    relative = extract_deadline("Please complete within 5 days.", date(2026, 8, 22))

    assert absolute is not None
    assert absolute.isoformat().startswith("2026-08-25T17:00:00")
    assert relative is not None
    assert relative.date().isoformat() == "2026-08-27"


def test_deadline_extractor_accepts_common_sept_abbreviation() -> None:
    deadline = extract_deadline("Complete this by 23:59 on 5 Sept 2026.", date(2026, 8, 22))

    assert deadline is not None
    assert deadline.isoformat().startswith("2026-09-05T23:59:00")


def test_deadline_extractor_ignores_impossible_calendar_date() -> None:
    deadline = extract_deadline("Complete this by 31 February 2026.", date(2026, 8, 22))

    assert deadline is None
