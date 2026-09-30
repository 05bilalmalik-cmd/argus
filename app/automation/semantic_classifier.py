"""Learned-ish semantic field classifier for non-ATS career portals.

Second-chance layer behind :data:`FLAG_NAME` (default off). It only ever runs
on fields the :class:`DeterministicClassifier
<app.automation.classifier.DeterministicClassifier>` already returned
``UNKNOWN`` for, and it only proposes a *mapping* (which canonical field this
is) with a confidence score and human-readable reason. It never invents
free-text content and never answers a field.

Safety: the layer can NEVER map to (or answer for) a sensitive key. If its
best guess lands in :data:`REFUSED_KEYS`, it returns ``UNKNOWN`` so the field
is escalated to the human instead.

Method: nearest-neighbour over a curated phrase corpus using only the
standard library (token Jaccard + ``difflib.SequenceMatcher``). Deterministic,
testable, offline, no model download.

The ``extra_context`` parameter accepts an optional plain richer-context
string (e.g. from a future FieldContext provider). It is deliberately a
plain ``str`` -- this module must NOT import that provider.
"""

from __future__ import annotations

import difflib
import os
import re

from app.domain.questions import CanonicalKey, FormQuestion, QuestionMapping, Sensitivity

#: Environment flag gating the whole layer. Default False (off).
FLAG_NAME = "ARGUS_SEMANTIC_CLASSIFIER_ENABLED"
#: Environment override for the acceptance threshold.
THRESHOLD_ENV_NAME = "ARGUS_SEMANTIC_CLASSIFIER_THRESHOLD"
#: Acceptance threshold: best scores below this return UNKNOWN.
DEFAULT_THRESHOLD = 0.45

#: Keys the semantic layer must never map to. A best guess in this set is
#: refused (-> UNKNOWN + human escalation), even on an obvious text match.
REFUSED_KEYS = frozenset(
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

# Exact _parse_bool pattern from app/config.py (copied, not imported, so this
# module stays dependency-light; should move into Settings when config.py
# gains the flag -- see docs/SEMANTIC_CLASSIFIER.md).
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in _TRUE_VALUES


def semantic_classifier_enabled() -> bool:
    """Return True only when the env flag explicitly opts in."""
    return _parse_bool(os.environ.get(FLAG_NAME), default=False)


def _resolve_threshold(threshold: float | None) -> float:
    if threshold is not None:
        return threshold
    raw = (os.environ.get(THRESHOLD_ENV_NAME) or "").strip()
    if not raw:
        return DEFAULT_THRESHOLD
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


# Curated paraphrase corpus: how non-ATS portals phrase each canonical field.
# Safe keys carry mapping phrases; refused keys carry phrases ONLY so the
# refusal path triggers (and is tested) on obvious matches.
_PHRASES: dict[CanonicalKey, tuple[str, ...]] = {
    CanonicalKey.FIRST_NAME: ("first name", "given name", "forename", "christian name"),
    CanonicalKey.LAST_NAME: ("last name", "family name", "surname"),
    CanonicalKey.FULL_NAME: ("full name", "your name", "candidate name", "applicant name"),
    CanonicalKey.EMAIL: ("email", "e-mail", "email address", "electronic mail"),
    CanonicalKey.PHONE: (
        "phone",
        "telephone",
        "mobile telephone",
        "mobile number",
        "cell number",
        "contact number",
        "phone number",
        "telephone number",
    ),
    CanonicalKey.ADDRESS_LINE_1: (
        "address line 1",
        "street address",
        "home address",
        "residential address",
        "first line of address",
    ),
    CanonicalKey.CITY: ("city", "town", "current location", "home location", "place of residence"),
    CanonicalKey.POSTCODE: (
        "post code",
        "postcode",
        "postal code",
        "zip",
        "zip code",
        "home post code",
    ),
    CanonicalKey.COUNTRY: ("country", "country of residence"),
    CanonicalKey.LINKEDIN: ("linkedin", "linkedin profile", "linkedin url"),
    CanonicalKey.GITHUB: ("github", "github profile", "git hub"),
    CanonicalKey.UNIVERSITY: (
        "university",
        "which university did you attend",
        "where did you study",
        "institution",
        "college",
        "alma mater",
        "name of university",
    ),
    CanonicalKey.DEGREE: ("degree", "degree title", "qualification", "major", "undergraduate degree"),
    CanonicalKey.EDUCATION_SUBJECT: ("field of study", "subjects", "areas of study"),
    CanonicalKey.EDUCATION_DISCIPLINE: ("discipline", "academic discipline"),
    CanonicalKey.EDUCATION_START_YEAR: ("education start year", "year started university"),
    CanonicalKey.EDUCATION_END_MONTH: ("education end month", "month of completion"),
    CanonicalKey.EDUCATION_END_YEAR: ("education end year", "year of completion"),
    CanonicalKey.GRADUATION_YEAR: (
        "graduation",
        "expected date of graduation",
        "graduation year",
        "year of graduation",
        "when do you graduate",
        "anticipated graduation date",
        "expected graduation",
    ),
    CanonicalKey.AVAILABLE_FROM: (
        "available to start",
        "availability",
        "when can you start",
        "earliest start date",
    ),
    CanonicalKey.WORK_LOCATION: ("location preference", "preferred work location", "preferred location"),
    CanonicalKey.DESIRED_OFFICE: ("desired office", "which office", "office preference", "preferred office"),
    CanonicalKey.STUDY_LEVEL: ("year of study", "study year", "academic year", "current study year"),
    CanonicalKey.PENULTIMATE_YEAR: ("penultimate year", "penultimate", "final year student"),
    CanonicalKey.CV: ("cv", "resume", "curriculum vitae", "upload cv"),
    CanonicalKey.COVER_LETTER: ("cover letter", "covering letter"),
    CanonicalKey.SOURCE: (
        "how did you hear about us",
        "referral source",
        "where did you hear",
        "how did you find this role",
    ),
    CanonicalKey.MOTIVATION: (
        "why this firm",
        "why us",
        "motivation",
        "why are you interested",
        "interest in the role",
    ),
    CanonicalKey.ACCOUNT_PASSWORD: ("password", "create a password", "choose password", "account password"),
    CanonicalKey.FINAL_GRADE: ("final grade", "overall grade", "degree classification", "predicted grade"),
    # Refused keys: phrases exist so obvious matches resolve to the refused
    # key internally, which the safety guard then converts to UNKNOWN.
    CanonicalKey.CRIMINAL_RECORD: (
        "criminal record",
        "criminal history",
        "criminal convictions",
        "have you been convicted",
        "have you ever been convicted of a criminal offence",
        "unspent convictions",
    ),
    CanonicalKey.SPONSORSHIP: (
        "visa sponsorship",
        "require sponsorship",
        "will you need sponsorship",
        "will you now or in the future require sponsorship",
        "sponsorship required",
    ),
    CanonicalKey.WORK_AUTHORISATION: (
        "right to work",
        "legally authorised to work",
        "legally authorized to work",
        "work authorisation",
        "work authorization",
        "employment eligibility",
    ),
    CanonicalKey.LEGAL_ATTESTATION: (
        "i certify",
        "i attest",
        "declaration",
        "information provided is accurate",
        "electronic signature",
        "confirm you understand",
    ),
    CanonicalKey.DEMOGRAPHIC: (
        "ethnicity",
        "ethnic background",
        "what is your ethnic background",
        "gender",
        "what is your gender",
        "date of birth",
        "sexual orientation",
        "disability",
        "religion",
        "veteran status",
        "national insurance",
    ),
    CanonicalKey.ASSESSMENT: (
        "online assessment",
        "psychometric",
        "hirevue",
        "video interview",
        "coding test",
        "numerical reasoning",
        "begin assessment",
    ),
    CanonicalKey.CAPTCHA: ("captcha", "verify you are human", "not a robot"),
}

_SENSITIVITY: dict[CanonicalKey, Sensitivity] = {
    CanonicalKey.ACCOUNT_PASSWORD: Sensitivity.SENSITIVE,
    CanonicalKey.CRIMINAL_RECORD: Sensitivity.LEGAL,
    CanonicalKey.SPONSORSHIP: Sensitivity.LEGAL,
    CanonicalKey.WORK_AUTHORISATION: Sensitivity.LEGAL,
    CanonicalKey.LEGAL_ATTESTATION: Sensitivity.LEGAL,
    CanonicalKey.DEMOGRAPHIC: Sensitivity.SENSITIVE,
    CanonicalKey.ASSESSMENT: Sensitivity.ASSESSMENT,
    CanonicalKey.CAPTCHA: Sensitivity.ASSESSMENT,
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.casefold())


def _phrase_score(phrase: str, text_tokens: frozenset[str], text_norm: str) -> float:
    phrase_tokens = frozenset(_tokens(phrase))
    if not phrase_tokens or not text_tokens:
        return 0.0
    jaccard = len(phrase_tokens & text_tokens) / len(phrase_tokens | text_tokens)
    sequence = difflib.SequenceMatcher(None, phrase.casefold(), text_norm).ratio()
    return 0.6 * jaccard + 0.4 * sequence


class SemanticClassifier:
    """Nearest-neighbour mapper over the curated phrase corpus.

    Only proposes mappings for non-sensitive keys; anything else (refused
    best guess, below-threshold score) returns ``UNKNOWN`` for human review.
    """

    def __init__(self, threshold: float | None = None) -> None:
        self.threshold = _resolve_threshold(threshold)

    def classify(self, question: FormQuestion, extra_context: str = "") -> QuestionMapping:
        text_norm = " ".join(
            part.casefold().strip()
            for part in (question.label, question.name, question.placeholder, extra_context)
            if part and part.strip()
        )
        text_tokens = frozenset(_tokens(text_norm))

        best_key = CanonicalKey.UNKNOWN
        best_phrase = ""
        best_score = 0.0
        for key, phrases in _PHRASES.items():
            for phrase in phrases:
                score = _phrase_score(phrase, text_tokens, text_norm)
                if score > best_score:
                    best_key, best_phrase, best_score = key, phrase, score

        # Safety first: never map to (or answer for) a sensitive key.
        if best_key in REFUSED_KEYS:
            return QuestionMapping(
                CanonicalKey.UNKNOWN,
                0.0,
                Sensitivity.STANDARD,
                f"Semantic best-guess {best_key.value} is sensitive; refused and escalated to human",
            )
        if best_score < self.threshold:
            return QuestionMapping(
                CanonicalKey.UNKNOWN,
                round(best_score, 4),
                Sensitivity.STANDARD,
                f"No semantic phrase above threshold {self.threshold:.2f} "
                f"(best {best_key.value} {best_score:.2f}); escalated to human",
            )
        return QuestionMapping(
            best_key,
            round(best_score, 4),
            _SENSITIVITY.get(best_key, Sensitivity.STANDARD),
            f"Semantic match {best_phrase!r} -> {best_key.value} (score {best_score:.2f})",
        )
