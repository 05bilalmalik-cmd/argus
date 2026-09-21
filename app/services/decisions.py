from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.domain.questions import CanonicalKey, Sensitivity, QuestionMapping
from app.domain.states import ApplicationState
from app.models import Application, Opportunity

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


#: Single source for the legal/sensitive tiers that must never carry a
#: confidence guess or a default option. The router imports this name;
#: the underscore alias below exists only for backward compatibility.
LEGAL_SENSITIVE_KEYS = frozenset(
    {
        CanonicalKey.WORK_AUTHORISATION,
        CanonicalKey.SPONSORSHIP,
        CanonicalKey.CRIMINAL_RECORD,
        CanonicalKey.LEGAL_ATTESTATION,
        CanonicalKey.DEMOGRAPHIC,
    }
)

_LEGAL_SENSITIVE_KEYS = LEGAL_SENSITIVE_KEYS


def decision_context_revision(
    *,
    canonical_key: CanonicalKey,
    question_text: str,
    permitted_options: tuple[str, ...] | list[str],
    sensitivity: Sensitivity,
) -> str:
    """Bind a decision to its exact request context.

    The revision covers everything a human sees as "the question": the
    canonical key, the wording, the offered action labels, and the
    sensitivity tier. A later, different question for the same
    application/key therefore yields a different decision ID, so an old
    acknowledgement can never permanently block the new question — while
    the identical context deterministically reproduces the same ID.
    """
    material = "|".join(
        [
            str(canonical_key.value),
            str(question_text),
            "||".join(str(option) for option in permitted_options),
            str(sensitivity.value),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def stable_decision_id(
    application_id: str,
    canonical_key: CanonicalKey,
    *,
    context_revision: str,
) -> str:
    """Deterministic decision ID for one application/key/context triple."""
    if not application_id:
        raise ValueError("stable_decision_id application_id is required")
    if not context_revision:
        raise ValueError("stable_decision_id context_revision is required")
    key = f"{application_id}:{canonical_key.value}:{context_revision}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def context_revision_for_request(request: DecisionRequest) -> str:
    """Return the context revision bound into ``request.id``."""
    return decision_context_revision(
        canonical_key=request.canonical_key,
        question_text=request.question_text,
        permitted_options=request.permitted_options,
        sensitivity=request.sensitivity,
    )


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """A typed decision request carrying confidence and risk tier for human resolution."""

    id: str
    application_id: str
    employer: str
    role: str
    question_text: str
    canonical_key: CanonicalKey
    sensitivity: Sensitivity
    permitted_options: tuple[str, ...]
    confidence: float | None
    prompt: str

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("DecisionRequest id is required")
        if not self.application_id:
            raise ValueError("DecisionRequest application_id is required")
        if not self.employer:
            raise ValueError("DecisionRequest employer is required")
        if not self.role:
            raise ValueError("DecisionRequest role is required")
        if not self.question_text:
            raise ValueError("DecisionRequest question_text is required")
        if not self.permitted_options:
            raise ValueError("DecisionRequest permitted_options must not be empty")
        if self.confidence is not None and not (0 <= self.confidence <= 1):
            raise ValueError("DecisionRequest confidence must be between 0 and 1")
        if self.canonical_key in _LEGAL_SENSITIVE_KEYS:
            if self.confidence is not None:
                raise ValueError(
                    f"Sensitive tier {self.canonical_key} must not carry a confidence guess"
                )
            if any(opt.casefold() == "default" for opt in self.permitted_options):
                raise ValueError(
                    f"Sensitive tier {self.canonical_key} must not include a default option"
                )

    def to_dict(self) -> dict[str, object]:
        """Serialise to a compact dict suitable for a message payload."""
        return {
            "id": self.id,
            "application_id": self.application_id,
            "employer": self.employer,
            "role": self.role,
            "question_text": self.question_text,
            "canonical_key": self.canonical_key.value,
            "sensitivity": self.sensitivity.value,
            "permitted_options": list(self.permitted_options),
            "confidence": self.confidence,
            "prompt": self.prompt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DecisionRequest:
        """Deserialise from a compact dict."""
        return cls(
            id=str(data["id"]),
            application_id=str(data["application_id"]),
            employer=str(data["employer"]),
            role=str(data["role"]),
            question_text=str(data["question_text"]),
            canonical_key=CanonicalKey(str(data["canonical_key"])),
            sensitivity=Sensitivity(str(data["sensitivity"])),
            permitted_options=tuple(str(opt) for opt in data["permitted_options"]),
            confidence=data.get("confidence"),
            prompt=str(data["prompt"]),
        )

    def validate_response(self, response: DecisionResponse) -> None:
        """Validate that a response conforms to this request."""
        if response.decision_id != self.id:
            raise ValueError("DecisionResponse decision_id does not match request")
        if response.chosen_option not in self.permitted_options:
            raise ValueError(
                f"Chosen option {response.chosen_option!r} not in permitted options {self.permitted_options}"
            )


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    """A human decision response to a DecisionRequest."""

    decision_id: str
    chosen_option: str
    decided_by: str
    decided_at: datetime

    def __post_init__(self) -> None:
        if not self.decision_id:
            raise ValueError("DecisionResponse decision_id is required")
        if not self.chosen_option:
            raise ValueError("DecisionResponse chosen_option is required")
        if not self.decided_by:
            raise ValueError("DecisionResponse decided_by is required")
        if self.decided_at.tzinfo is None:
            raise ValueError("DecisionResponse decided_at must be timezone-aware")

    def to_dict(self) -> dict[str, object]:
        """Serialise to a compact dict."""
        return {
            "decision_id": self.decision_id,
            "chosen_option": self.chosen_option,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DecisionResponse:
        """Deserialise from a compact dict."""
        decided_at = data["decided_at"]
        if isinstance(decided_at, str):
            decided_at = datetime.fromisoformat(decided_at)
        return cls(
            decision_id=str(data["decision_id"]),
            chosen_option=str(data["chosen_option"]),
            decided_by=str(data["decided_by"]),
            decided_at=decided_at,
        )


def _canonical_key_for_reason(reason: str) -> CanonicalKey | None:
    """Map a notification reason code to the canonical key it concerns."""
    mapping = {
        "captcha": CanonicalKey.CAPTCHA,
        "assessment_handoff": CanonicalKey.ASSESSMENT,
        "required_cv_missing": CanonicalKey.CV,
        "required_cover_letter_missing": CanonicalKey.COVER_LETTER,
        "work_authorisation_missing": CanonicalKey.WORK_AUTHORISATION,
        "approved_legal_answer_missing": CanonicalKey.LEGAL_ATTESTATION,
        "sensitive_demographic": CanonicalKey.DEMOGRAPHIC,
        "programme_framing_required": CanonicalKey.PROGRAMME_GRADUATION_CONFLICT,
        "authentication_handoff": CanonicalKey.CAPTCHA,
        "application_entry_unresolved": CanonicalKey.UNKNOWN,
        "application_root_unverified": CanonicalKey.UNKNOWN,
        "field_verification_failed": CanonicalKey.UNKNOWN,
        "human_review_required": CanonicalKey.UNKNOWN,
        "step_budget_exceeded": CanonicalKey.UNKNOWN,
        "step_transition_unproven": CanonicalKey.UNKNOWN,
        "submission_network_guard": CanonicalKey.UNKNOWN,
        "submission_target_guard": CanonicalKey.UNKNOWN,
        "destination_identity_unverified": CanonicalKey.UNKNOWN,
    }
    return mapping.get(reason)


def _sensitivity_for_key(key: CanonicalKey) -> Sensitivity:
    """Determine the sensitivity tier for a canonical key."""
    if key in {CanonicalKey.WORK_AUTHORISATION, CanonicalKey.SPONSORSHIP, CanonicalKey.CRIMINAL_RECORD, CanonicalKey.LEGAL_ATTESTATION}:
        return Sensitivity.LEGAL
    if key == CanonicalKey.DEMOGRAPHIC:
        return Sensitivity.SENSITIVE
    if key in {CanonicalKey.CAPTCHA, CanonicalKey.ASSESSMENT}:
        return Sensitivity.ASSESSMENT
    return Sensitivity.STANDARD


def _permitted_options_for_key(key: CanonicalKey) -> tuple[str, ...]:
    """Return the permitted typed options for a canonical key."""
    options_map: dict[CanonicalKey, tuple[str, ...]] = {
        CanonicalKey.WORK_AUTHORISATION: ("Yes", "No"),
        CanonicalKey.SPONSORSHIP: ("Yes", "No"),
        CanonicalKey.CRIMINAL_RECORD: ("Yes", "No"),
        CanonicalKey.LEGAL_ATTESTATION: ("I confirm", "I do not confirm"),
        CanonicalKey.DEMOGRAPHIC: ("Prefer not to say", "Provide details"),
        CanonicalKey.CAPTCHA: ("Completed", "Cannot complete"),
        CanonicalKey.ASSESSMENT: ("Start now", "Schedule later", "Decline"),
        CanonicalKey.CV: ("Upload CV", "Skip for now"),
        CanonicalKey.COVER_LETTER: ("Upload cover letter", "Skip for now"),
        CanonicalKey.PROGRAMME_GRADUATION_CONFLICT: ("Confirm programme", "Update graduation year"),
    }
    return options_map.get(key, ("Confirm", "Decline"))


def _question_text_for_key(key: CanonicalKey) -> str:
    """Return a human-readable question text for a canonical key."""
    texts: dict[CanonicalKey, str] = {
        CanonicalKey.WORK_AUTHORISATION: "Are you authorised to work in the UK?",
        CanonicalKey.SPONSORSHIP: "Will you require visa sponsorship now or in the future?",
        CanonicalKey.CRIMINAL_RECORD: "Do you have any criminal convictions?",
        CanonicalKey.LEGAL_ATTESTATION: "I certify that all information provided is true and complete.",
        CanonicalKey.DEMOGRAPHIC: "Optional demographic information — how would you like to proceed?",
        CanonicalKey.CAPTCHA: "A CAPTCHA challenge requires human completion.",
        CanonicalKey.ASSESSMENT: "An online assessment is required for this application.",
        CanonicalKey.CV: "A CV is required for this application.",
        CanonicalKey.COVER_LETTER: "A cover letter is required for this application.",
        CanonicalKey.PROGRAMME_GRADUATION_CONFLICT: "Programme type and graduation year must be confirmed.",
    }
    return texts.get(key, "Human input required.")


def _prompt_for_key(key: CanonicalKey, employer: str, role: str) -> str:
    """Return a short human-readable prompt for the decision."""
    base = _question_text_for_key(key)
    if key in _LEGAL_SENSITIVE_KEYS:
        return f"{employer} — {role}: {base} (No default; you must choose explicitly)"
    return f"{employer} — {role}: {base}"


def _confidence_for_application(application: Application, key: CanonicalKey) -> float | None:
    """Return ARGUS's best-guess confidence for this application/key, or None for sensitive tiers."""
    if key in _LEGAL_SENSITIVE_KEYS:
        return None
    # For non-sensitive keys, we could compute confidence from stored answers.
    # For now, return None to indicate no automated guess.
    return None


def build_decision_requests(session: Session) -> list[DecisionRequest]:
    """Produce DecisionRequests for applications currently needing a human.

    Read-only; no DB writes. Reuses the state/reason vocabulary from review_digest.py
    and the notification reason codes.
    """
    from app.services.notifications import select_human_attention_reason

    human_states = {
        ApplicationState.NEEDS_USER.value,
        ApplicationState.NEEDS_OA.value,
        ApplicationState.BLOCKED.value,
        ApplicationState.READY_TO_SUBMIT.value,
        ApplicationState.FAILED_RETRYABLE.value,
    }

    applications = session.query(Application).join(Opportunity).filter(
        Application.state.in_(human_states)
    ).all()

    requests: list[DecisionRequest] = []
    for application in applications:
        opportunity = application.opportunity
        if not opportunity:
            continue

        # Determine the reason code for this application
        boundary_kind = ""
        blocked_reasons: list[object] = []
        detail = application.next_action or ""

        # Try to extract reason from eligibility/conflict JSON
        for attr in ("eligibility_json", "conflict_json"):
            try:
                import json
                document = json.loads(str(getattr(application, attr, "") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(document, dict):
                values = document.get("reason_codes") or ()
                if isinstance(values, (list, tuple, set)):
                    blocked_reasons.extend(values)

        reason = select_human_attention_reason(
            state=application.state,
            boundary_kind=boundary_kind,
            blocked_reasons=blocked_reasons,
            detail=detail,
        )

        canonical_key = _canonical_key_for_reason(reason)
        if canonical_key is None or canonical_key == CanonicalKey.UNKNOWN:
            # Skip applications without a clear canonical key mapping
            continue

        sensitivity = _sensitivity_for_key(canonical_key)
        permitted_options = _permitted_options_for_key(canonical_key)
        confidence = _confidence_for_application(application, canonical_key)
        question_text = _question_text_for_key(canonical_key)
        prompt = _prompt_for_key(canonical_key, opportunity.employer, opportunity.role_title)

        request = DecisionRequest(
            id=uuid.uuid4().hex,
            application_id=str(application.id),
            employer=str(opportunity.employer),
            role=str(opportunity.role_title),
            question_text=question_text,
            canonical_key=canonical_key,
            sensitivity=sensitivity,
            permitted_options=permitted_options,
            confidence=confidence,
            prompt=prompt,
        )
        requests.append(request)

    return requests