from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CanonicalKey(StrEnum):
    FIRST_NAME = "identity.first_name"
    LAST_NAME = "identity.last_name"
    FULL_NAME = "identity.full_name"
    EMAIL = "contact.email"
    PHONE = "contact.phone"
    ADDRESS_LINE_1 = "contact.address_line_1"
    CITY = "contact.city"
    POSTCODE = "contact.postcode"
    COUNTRY = "contact.country"
    LINKEDIN = "contact.linkedin"
    GITHUB = "contact.github"
    UNIVERSITY = "education.university"
    DEGREE = "education.degree"
    EDUCATION_SUBJECT = "answer.subjects"
    EDUCATION_DISCIPLINE = "answer.discipline"
    EDUCATION_START_YEAR = "education.start_year"
    EDUCATION_END_MONTH = "answer.end_month"
    EDUCATION_END_YEAR = "answer.end_year"
    GRADUATION_YEAR = "education.graduation_year"
    AVAILABLE_FROM = "answer.available_from"
    WORK_LOCATION = "answer.work_location"
    DESIRED_OFFICE = "answer.desired_office"
    STUDY_LEVEL = "education.current_study_year"
    PENULTIMATE_YEAR = "answer.penultimate_year"
    PROGRAMME_GRADUATION_CONFLICT = "guard.programme_graduation_conflict"
    WORK_AUTHORISATION = "legal.work_authorisation"
    SPONSORSHIP = "legal.sponsorship"
    CV = "document.cv"
    COVER_LETTER = "document.cover_letter"
    SOURCE = "answer.source"
    CRIMINAL_RECORD = "legal.criminal_record"
    MOTIVATION = "answer.motivation"
    ACCOUNT_PASSWORD = "account.password"
    DEMOGRAPHIC = "sensitive.demographic"
    LEGAL_ATTESTATION = "legal.attestation"
    FINAL_GRADE = "answer.final_grade"
    ASSESSMENT = "handoff.assessment"
    CAPTCHA = "handoff.captcha"
    UNKNOWN = "unknown"


class Sensitivity(StrEnum):
    STANDARD = "standard"
    SENSITIVE = "sensitive"
    LEGAL = "legal"
    ASSESSMENT = "assessment"


@dataclass(frozen=True, slots=True)
class FormQuestion:
    label: str
    field_type: str
    name: str = ""
    placeholder: str = ""
    required: bool = False
    options: tuple[str, ...] = ()
    option_label: str = ""


@dataclass(frozen=True, slots=True)
class QuestionMapping:
    canonical_key: CanonicalKey
    confidence: float
    sensitivity: Sensitivity
    reason: str

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("Confidence must be between 0 and 1")
