from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    expected_graduation_year: int | None
    requires_sponsorship: bool | None
    work_authorisation_approved: bool
    preferred_locations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OpportunitySnapshot:
    opportunity_id: str
    url: str
    deadline: date | None
    min_graduation_year: int | None
    max_graduation_year: int | None
    sponsorship_supported: bool | None
    location: str | None
    already_applied: bool = False


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    eligible: bool
    requires_review: bool
    reason_codes: tuple[str, ...]
    explanations: tuple[str, ...]


def evaluate_eligibility(
    profile: CandidateSnapshot,
    opportunity: OpportunitySnapshot,
    today: date,
) -> EligibilityDecision:
    blocking: list[tuple[str, str]] = []
    review: list[tuple[str, str]] = []

    if opportunity.already_applied:
        blocking.append(("duplicate_application", "An active application already exists."))

    if opportunity.deadline is not None and opportunity.deadline < today:
        blocking.append(("deadline_expired", "The application deadline has passed."))

    graduation = profile.expected_graduation_year
    if graduation is None:
        review.append(("graduation_year_unverified", "Expected graduation year is missing."))
    else:
        if (
            opportunity.min_graduation_year is not None
            and graduation < opportunity.min_graduation_year
        ):
            blocking.append(
                ("graduation_year_too_early", "Graduation year is below the role minimum.")
            )
        if (
            opportunity.max_graduation_year is not None
            and graduation > opportunity.max_graduation_year
        ):
            blocking.append(
                ("graduation_year_too_late", "Graduation year is above the role maximum.")
            )

    if opportunity.sponsorship_supported is False:
        if not profile.work_authorisation_approved or profile.requires_sponsorship is None:
            review.append(
                (
                    "work_authorisation_unverified",
                    "Work-authorisation data needs exact candidate approval.",
                )
            )
        elif profile.requires_sponsorship:
            blocking.append(
                ("sponsorship_not_supported", "The role does not support required sponsorship.")
            )

    combined = blocking + review
    return EligibilityDecision(
        eligible=not blocking,
        requires_review=bool(review),
        reason_codes=tuple(code for code, _ in combined),
        explanations=tuple(message for _, message in combined),
    )
