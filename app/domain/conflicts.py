from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Sequence

from app.domain.states import ApplicationState


@dataclass(frozen=True, slots=True)
class CandidateApplication:
    employer: str
    cycle: str
    division: str
    programme_group: str | None


@dataclass(frozen=True, slots=True)
class ExistingApplication:
    employer: str
    cycle: str
    division: str
    programme_group: str | None
    state: ApplicationState


@dataclass(frozen=True, slots=True)
class ConflictRuleSnapshot:
    employer_pattern: str
    cycle: str
    max_applications: int | None
    mutually_exclusive_groups: tuple[frozenset[str], ...]


@dataclass(frozen=True, slots=True)
class ConflictDecision:
    blocked: bool
    reason_codes: tuple[str, ...]
    explanations: tuple[str, ...]


_INACTIVE_STATES = {
    ApplicationState.DISCOVERED,
    ApplicationState.ELIGIBILITY_CHECKED,
    ApplicationState.BLOCKED,
    ApplicationState.REJECTED,
}


def _matches_employer(pattern: str, employer: str) -> bool:
    return fnmatchcase(employer.casefold(), pattern.casefold())


def _normalise_programme_group(value: str) -> str:
    return "_".join(value.strip().casefold().split())


def evaluate_conflicts(
    candidate: CandidateApplication,
    existing: Sequence[ExistingApplication],
    rules: Sequence[ConflictRuleSnapshot],
) -> ConflictDecision:
    reasons: list[tuple[str, str]] = []
    active = [
        application
        for application in existing
        if application.state not in _INACTIVE_STATES
        and application.cycle.casefold() == candidate.cycle.casefold()
        and application.employer.casefold() == candidate.employer.casefold()
    ]

    for rule in rules:
        if rule.cycle.casefold() != candidate.cycle.casefold():
            continue
        if not _matches_employer(rule.employer_pattern, candidate.employer):
            continue

        if rule.max_applications is not None and len(active) >= rule.max_applications:
            reasons.append(
                (
                    "maximum_applications_reached",
                    f"Employer permits at most {rule.max_applications} active application(s).",
                )
            )

        if candidate.programme_group:
            candidate_group = _normalise_programme_group(candidate.programme_group)
            for exclusive_group in rule.mutually_exclusive_groups:
                normalised_group = {
                    _normalise_programme_group(item) for item in exclusive_group
                }
                if candidate_group not in normalised_group:
                    continue
                conflict = next(
                    (
                        item
                        for item in active
                        if item.programme_group
                        and _normalise_programme_group(item.programme_group) in normalised_group
                        and _normalise_programme_group(item.programme_group) != candidate_group
                    ),
                    None,
                )
                if conflict:
                    reasons.append(
                        (
                            "mutually_exclusive_programme",
                            f"Conflicts with active {conflict.division} application.",
                        )
                    )
                    break

    deduped: dict[str, str] = {}
    for code, explanation in reasons:
        deduped.setdefault(code, explanation)
    return ConflictDecision(
        blocked=bool(deduped),
        reason_codes=tuple(deduped),
        explanations=tuple(deduped.values()),
    )
