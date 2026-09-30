from __future__ import annotations

import json
import re
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.automation.semantic_classifier import SemanticClassifier, semantic_classifier_enabled
from app.domain.questions import (
    CanonicalKey,
    FormQuestion,
    QuestionMapping,
    Sensitivity,
)


class ClassificationError(RuntimeError):
    pass


class Classifier(Protocol):
    def classify(self, question: FormQuestion) -> QuestionMapping: ...


def _text(question: FormQuestion) -> str:
    return " ".join(
        part for part in (question.label, question.name, question.placeholder) if part
    ).casefold()


def _primary_label(question: FormQuestion) -> str:
    """Return the label's primary identity before option enrichment (split on ' — ')."""
    label = (question.label or "").strip()
    if " — " in label:
        return label.split(" — ")[0].strip()
    return label


def _is_source_question(question: FormQuestion) -> bool:
    """Check if question is a SOURCE/referral question from primary identity."""
    name = question.name.casefold().strip()
    primary = _primary_label(question).casefold()
    if name == "source" or primary == "source":
        return True
    # Option/help enrichment is context, not the question's identity.
    text = " ".join((primary, name, question.placeholder.casefold()))
    return _contains(text, r"hear about", r"referral source", r"how did you find")


def _contains(text: str, *patterns: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _name_matches(question: FormQuestion, *patterns: str) -> bool:
    name = question.name.casefold().strip()
    return any(re.fullmatch(pattern, name, flags=re.IGNORECASE) for pattern in patterns)


class DeterministicClassifier:
    def classify(self, question: FormQuestion) -> QuestionMapping:
        text = _text(question)

        # The actual credential control type outranks all label keywords.
        if question.field_type.casefold() == "password":
            return self._mapping(
                CanonicalKey.ACCOUNT_PASSWORD, Sensitivity.SENSITIVE, "Account password matched"
            )

        if question.field_type.casefold() == "captcha" or _contains(
            text, r"captcha", r"verify (?:that )?you are human", r"not a robot"
        ):
            return self._mapping(CanonicalKey.CAPTCHA, Sensitivity.ASSESSMENT, "CAPTCHA detected")

        if _contains(
            text,
            r"online assessment",
            r"begin assessment",
            r"psychometric",
            r"hirevue",
            r"video interview",
            r"coding test",
            r"numerical reasoning",
        ):
            return self._mapping(
                CanonicalKey.ASSESSMENT,
                Sensitivity.ASSESSMENT,
                "Assessment or recorded response detected",
            )

        if _contains(text, r"criminal record"):
            return self._mapping(
                CanonicalKey.CRIMINAL_RECORD,
                Sensitivity.LEGAL,
                "Criminal record question matched to stored answer",
            )

        if _contains(
            text,
            r"i certify",
            r"i attest",
            r"declaration",
            r"conflict of interest",
            r"regulatory",
            r"information provided is accurate",
            r"electronic signature",
            r"\bprivacy\b",
            r"confirm you understand",
        ):
            return self._mapping(
                CanonicalKey.LEGAL_ATTESTATION,
                Sensitivity.LEGAL,
                "Legal declaration requires human approval",
            )

        # Untyped credential wording is also sensitive; legal wording above
        # retains precedence for ordinary non-password controls.
        if _contains(text, r"\bpassword\b"):
            return self._mapping(
                CanonicalKey.ACCOUNT_PASSWORD, Sensitivity.SENSITIVE, "Account password matched"
            )

        if _contains(
            text,
            r"ethnic",
            r"race\b",
            r"gender",
            r"sexual orientation",
            r"disab",
            r"religion",
            r"veteran",
            r"date of birth",
            r"national insurance",
            r"\bmilitary\b",
        ):
            return self._mapping(
                CanonicalKey.DEMOGRAPHIC,
                Sensitivity.SENSITIVE,
                "Protected or sensitive demographic question",
            )

        if _contains(text, r"\bvisa\b", r"sponsor", r"visa sponsorship", r"immigration sponsorship"):
            return self._mapping(
                CanonicalKey.SPONSORSHIP,
                Sensitivity.LEGAL,
                "Sponsorship wording matched",
            )

        if _contains(
            text,
            r"employment eligibility",
            r"right to work",
            r"legally authori[sz]ed to work",
            r"work authori[sz]ation",
            r"permission to work",
        ):
            return self._mapping(
                CanonicalKey.WORK_AUTHORISATION,
                Sensitivity.LEGAL,
                "Work-authorisation wording matched",
            )

        # These are factual or negotiating answers about the candidate. They
        # must remain human-only even when an answer bank happens to contain a
        # similarly worded entry; reusing prose here could misrepresent the
        # candidate's history or negotiating position.
        if _contains(
            text,
            r"compensation",
            r"salary expectation",
            r"expected salary",
            r"desired salary",
            r"pay expectation",
            r"annual(?:ized|ised) total compensation",
        ):
            return QuestionMapping(
                CanonicalKey.UNKNOWN,
                0.5,
                Sensitivity.STANDARD,
                "Compensation expectation requires a candidate answer",
            )
        if _contains(
            text,
            r"\bcompetition\b",
            r"competitive event",
            r"math competition",
            r"programming contest",
            r"coding contest",
            r"hackathon",
        ):
            return QuestionMapping(
                CanonicalKey.UNKNOWN,
                0.5,
                Sensitivity.STANDARD,
                "Competition history is a factual candidate answer",
            )
        if _contains(
            text,
            r"(?:previous|prior|completed|have\s+you\s+(?:completed|done|participated)).*\binternship",
            r"internship\s+(?:history|experience)",
            r"\binterned\b",
            r"hedge fund or proprietary trading",
        ):
            return QuestionMapping(
                CanonicalKey.UNKNOWN,
                0.5,
                Sensitivity.STANDARD,
                "Internship history is a factual candidate answer",
            )
        if _contains(
            text,
            r"overall\s+grade",
            r"final\s+grade",
            r"degree\s+classification",
            r"predicted\s+grade",
        ):
            return self._mapping(
                CanonicalKey.FINAL_GRADE, Sensitivity.STANDARD, "Final grade question matched to stored answer"
            )
        if _contains(text, r"\bgpa\b", r"grade point average"):
            return QuestionMapping(
                CanonicalKey.UNKNOWN,
                0.5,
                Sensitivity.STANDARD,
                "GPA is a factual candidate answer",
            )

        if question.field_type.casefold() == "file":
            cover = _contains(text, r"cover[ _-]*(?:ing[ _-]*)?letter")
            cv = _contains(text, r"\bcv\b", r"r[eé]sum[eé]", r"curriculum vitae")
            other = _contains(
                text, r"transcript", r"certificate", r"passport", r"portfolio",
                r"writing sample", r"proof of", r"supporting document",
            )
            if other or (cover and cv):
                return QuestionMapping(
                    CanonicalKey.UNKNOWN, 0.0, Sensitivity.STANDARD,
                    "Unsupported or conflicting document purpose requires human review",
                )
            if cover:
                return self._mapping(
                    CanonicalKey.COVER_LETTER, Sensitivity.STANDARD, "Cover-letter upload matched"
                )
            if cv:
                return self._mapping(CanonicalKey.CV, Sensitivity.STANDARD, "CV upload matched")
            return QuestionMapping(
                CanonicalKey.UNKNOWN, 0.0, Sensitivity.STANDARD,
                "File upload has no verified document purpose",
            )

        # SOURCE before brand matches: a "How did you hear about us?" select
        # legitimately lists brands (LinkedIn, Indeed, GitHub) among its
        # options, and the adapter enriches those option texts into the label.
        # The question identity (hear about / referral source / how did you
        # find / name=source / label=Source) must outrank an incidental brand
        # mention. Legal, demographic, sponsorship and work-authorisation checks
        # stay above, so sensitive questions with brand options still escalate.
        # A bare LinkedIn profile/URL field carries no SOURCE phrasing and still
        # falls through to LINKEDIN below.
        if _is_source_question(question):
            return self._mapping(
                CanonicalKey.SOURCE,
                Sensitivity.STANDARD,
                "Referral/marketing source matched to stored answer",
            )
        if _contains(text, r"github", r"git hub"):
            return self._mapping(CanonicalKey.GITHUB, Sensitivity.STANDARD, "GitHub matched")
        if _contains(text, r"linkedin"):
            return self._mapping(CanonicalKey.LINKEDIN, Sensitivity.STANDARD, "LinkedIn matched")
        if _contains(text, r"first[ _-]*name", r"given[ _-]*name", r"forename"):
            return self._mapping(CanonicalKey.FIRST_NAME, Sensitivity.STANDARD, "First name matched")
        if _contains(text, r"last[ _-]*name", r"family[ _-]*name", r"surname"):
            return self._mapping(CanonicalKey.LAST_NAME, Sensitivity.STANDARD, "Last name matched")
        if _contains(text, r"full[ _-]*name", r"your name"):
            return self._mapping(CanonicalKey.FULL_NAME, Sensitivity.STANDARD, "Full name matched")
        if question.field_type.casefold() == "email" or _contains(text, r"e-?mail"):
            return self._mapping(CanonicalKey.EMAIL, Sensitivity.STANDARD, "Email matched")
        if question.field_type.casefold() == "tel" or _contains(
            text, r"phone", r"mobile", r"telephone"
        ):
            return self._mapping(CanonicalKey.PHONE, Sensitivity.STANDARD, "Phone matched")
        if _contains(text, r"address line ?1", r"street address", r"home address"):
            return self._mapping(
                CanonicalKey.ADDRESS_LINE_1, Sensitivity.STANDARD, "Address matched"
            )
        if _contains(text, r"post ?code", r"postal code", r"zip code"):
            return self._mapping(CanonicalKey.POSTCODE, Sensitivity.STANDARD, "Postcode matched")
        if _contains(text, r"\bcurrent location\b", r"\bhome location\b"):
            return self._mapping(CanonicalKey.CITY, Sensitivity.STANDARD, "Current location matched")
        if _contains(text, r"location preference", r"preferred work location"):
            return self._mapping(
                CanonicalKey.WORK_LOCATION,
                Sensitivity.STANDARD,
                "Work-location preference matched",
            )
        if _contains(text, r"desired office", r"which office", r"office applying"):
            return self._mapping(
                CanonicalKey.DESIRED_OFFICE,
                Sensitivity.STANDARD,
                "Desired-office preference matched",
            )
        if _contains(text, r"\bcity\b", r"town"):
            return self._mapping(CanonicalKey.CITY, Sensitivity.STANDARD, "City matched")
        if _contains(text, r"\bcountry\b"):
            return self._mapping(CanonicalKey.COUNTRY, Sensitivity.STANDARD, "Country matched")
        if _contains(
            text,
            r"(?:is|was) (?:your|the) university",
            r"(?:your|the) university is",
            r"university (?:public|private|approved|accredited)",
        ):
            return QuestionMapping(
                CanonicalKey.UNKNOWN,
                0.4,
                Sensitivity.STANDARD,
                "University policy/status question requires a distinct human-reviewed answer",
            )
        if _contains(text, r"university", r"institution"):
            return self._mapping(
                CanonicalKey.UNIVERSITY, Sensitivity.STANDARD, "University matched"
            )
        # Greenhouse's repeated education row uses generated IDs such as
        # ``school--0``.  That ID is independent evidence for the institution
        # control; an unqualified text label remains ambiguous below.
        if _name_matches(question, r"school[-_]{1,2}\d+"):
            return self._mapping(
                CanonicalKey.UNIVERSITY,
                Sensitivity.STANDARD,
                "Generated education-school control matched",
            )
        # A bare "school" match is ambiguous when several education records
        # exist; only an explicit university/institution label maps directly.
        if _contains(text, r"school name", r"\bschool\b"):
            return QuestionMapping(
                CanonicalKey.UNKNOWN,
                0.4,
                Sensitivity.STANDARD,
                "Ambiguous school reference; requires human mapping",
            )
        if _contains(text, r"graduation", r"expected.*graduate", r"graduate date", r"completion year"):
            return self._mapping(
                CanonicalKey.GRADUATION_YEAR, Sensitivity.STANDARD, "Graduation date matched"
            )
        if _name_matches(question, r"end[-_]month[-_]-?\d+", r"end[-_]month--\d+"):
            return self._mapping(
                CanonicalKey.EDUCATION_END_MONTH,
                Sensitivity.STANDARD,
                "Generated education end-month control matched",
            )
        if _name_matches(question, r"end[-_]year[-_]-?\d+", r"end[-_]year--\d+"):
            return self._mapping(
                CanonicalKey.EDUCATION_END_YEAR,
                Sensitivity.STANDARD,
                "Generated education end-year control matched",
            )
        if _contains(text, r"end date", r"end\s*\?", r"\bend\b.*\byear"):
            return QuestionMapping(
                CanonicalKey.UNKNOWN,
                0.4,
                Sensitivity.STANDARD,
                "Ambiguous end date (education vs employment); requires human mapping",
            )
        if _contains(text, r"fields? of study", r"subjects?", r"undergrad disciplin"):
            return self._mapping(
                CanonicalKey.EDUCATION_SUBJECT,
                Sensitivity.STANDARD,
                "Education subject/field-of-study matched",
            )
        if _contains(text, r"\bdiscipline\b"):
            return self._mapping(
                CanonicalKey.EDUCATION_DISCIPLINE,
                Sensitivity.STANDARD,
                "Education discipline matched",
            )
        if _contains(
            text,
            r"year of study",
            r"study year",
            r"academic year",
            r"current year at",
            r"year at university",
        ):
            if _contains(text, r"penultimate", r"final year"):
                return self._mapping(
                    CanonicalKey.PENULTIMATE_YEAR,
                    Sensitivity.STANDARD,
                    "Penultimate/final-year question matched",
                )
            return self._mapping(
                CanonicalKey.STUDY_LEVEL,
                Sensitivity.STANDARD,
                "Study-level question matched",
            )
        if _contains(text, r"degree", r"course of study", r"field of study", r"qualification",
                     r"discipline", r"major", r"undergrad"):
            return self._mapping(CanonicalKey.DEGREE, Sensitivity.STANDARD, "Degree matched")
        if _name_matches(question, r"start[-_]year--\d+"):
            return self._mapping(
                CanonicalKey.EDUCATION_START_YEAR,
                Sensitivity.STANDARD,
                "Generated education start-year control matched",
            )
        if _contains(
            text,
            r"start date",
            r"start\?",
            r"available from",
            r"available to start",
            r"availability",
            r"\bstart\b.*\byear",
        ):
            if _contains(text, r"available from", r"available to start", r"availability"):
                return self._mapping(
                    CanonicalKey.AVAILABLE_FROM,
                    Sensitivity.STANDARD,
                    "Availability date matched",
                )
            return QuestionMapping(
                CanonicalKey.UNKNOWN,
                0.4,
                Sensitivity.STANDARD,
                "Ambiguous start date (education vs employment); requires human mapping",
            )
        if question.field_type.casefold() == "textarea" and _contains(
            text,
            r"\bwhy\b",
            r"motivat",
            r"interest",
            r"tell us about",
            r"please explain",
        ):
            return self._mapping(
                CanonicalKey.MOTIVATION, Sensitivity.STANDARD, "Written answer detected"
            )

        if semantic_classifier_enabled():
            # Second-chance layer: only fields with no deterministic mapping
            # (UNKNOWN, confidence 0.0) reach here. Confident mappings above
            # -- including deliberate UNKNOWN escalations -- are never
            # overridden.
            return SemanticClassifier().classify(question)
        return QuestionMapping(
            CanonicalKey.UNKNOWN,
            0.0,
            Sensitivity.STANDARD,
            "No deterministic mapping",
        )

    @staticmethod
    def _mapping(
        key: CanonicalKey, sensitivity: Sensitivity, reason: str
    ) -> QuestionMapping:
        return QuestionMapping(key, 0.98, sensitivity, reason)


class _OllamaResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_key: CanonicalKey
    confidence: float = Field(ge=0.0, le=1.0)
    sensitivity: Sensitivity
    reason: str = Field(min_length=1, max_length=300)


class OllamaClassifier:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        client: httpx.Client | object | None = None,
        timeout: float = 8.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = client or httpx.Client()
        self.timeout = timeout

    @staticmethod
    def _schema() -> dict[str, object]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["canonical_key", "confidence", "sensitivity", "reason"],
            "properties": {
                "canonical_key": {
                    "type": "string",
                    "enum": [key.value for key in CanonicalKey],
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "sensitivity": {
                    "type": "string",
                    "enum": [value.value for value in Sensitivity],
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 300},
            },
        }

    def classify(self, question: FormQuestion) -> QuestionMapping:
        prompt = (
            "Classify this internship-application form field into the supplied canonical schema. "
            "Do not answer the field, infer a candidate fact, or rewrite the question. "
            "Choose unknown whenever uncertain.\n\n"
            f"Label: {question.label}\n"
            f"Name: {question.name}\n"
            f"Placeholder: {question.placeholder}\n"
            f"Input type: {question.field_type}\n"
            f"Required: {question.required}\n"
            f"Options: {list(question.options)}"
        )
        body = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": self._schema(),
            "options": {"temperature": 0},
        }
        try:
            response = self.client.post(
                f"{self.base_url}/api/generate", json=body, timeout=self.timeout
            )
            response.raise_for_status()
            payload = response.json()
            raw = payload.get("response")
            if isinstance(raw, str):
                raw = json.loads(raw)
            result = _OllamaResult.model_validate(raw)
        except (ValidationError, json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
            raise ClassificationError(f"Ollama schema validation failed: {exc}") from exc
        except Exception as exc:
            raise ClassificationError(f"Ollama classification failed: {exc}") from exc
        return QuestionMapping(
            result.canonical_key,
            result.confidence,
            result.sensitivity,
            result.reason,
        )


class CompositeClassifier:
    def __init__(
        self,
        deterministic: DeterministicClassifier | None = None,
        fallback: Classifier | None = None,
    ) -> None:
        self.deterministic = deterministic or DeterministicClassifier()
        self.fallback = fallback

    def classify(self, question: FormQuestion) -> QuestionMapping:
        result = self.deterministic.classify(question)
        if result.canonical_key is not CanonicalKey.UNKNOWN or self.fallback is None:
            return result
        try:
            fallback = self.fallback.classify(question)
        except ClassificationError:
            return result
        if fallback.confidence < 0.75:
            return QuestionMapping(
                CanonicalKey.UNKNOWN,
                fallback.confidence,
                fallback.sensitivity,
                "AI mapping below confidence threshold",
            )
        return fallback
