from __future__ import annotations

from enum import StrEnum


class UserApplicationStatus(StrEnum):
    """Candidate-owned status, deliberately separate from machine state."""

    NOT_APPLIED = "NOT_APPLIED"
    INTERESTED = "INTERESTED"
    NOT_INTERESTED = "NOT_INTERESTED"
    APPLICATION_SUBMITTED = "APPLICATION_SUBMITTED"
    ONLINE_ASSESSMENT = "ONLINE_ASSESSMENT"
    HIREVUE = "HIREVUE"
    ONLINE_TEST = "ONLINE_TEST"
    FIRST_ROUND = "FIRST_ROUND"
    OFFER = "OFFER"
    REJECTED = "REJECTED"


class UserStatusActor(StrEnum):
    DEFAULT = "default"
    USER = "user"
    IMPORT = "import"


_AUTOMATION_ELIGIBLE_USER_STATUSES = frozenset(
    {
        UserApplicationStatus.NOT_APPLIED,
        UserApplicationStatus.INTERESTED,
    }
)


def user_status_is_automation_eligible(value: object) -> bool:
    """Fail closed unless a persisted value is one of the two eligible states."""

    try:
        status = UserApplicationStatus(str(value))
    except (TypeError, ValueError):
        return False
    return status in _AUTOMATION_ELIGIBLE_USER_STATUSES


def user_status_work_rank(value: object) -> int:
    """Return deterministic work rank: interested, not applied, then excluded."""

    try:
        status = UserApplicationStatus(str(value))
    except (TypeError, ValueError):
        return 2
    if status is UserApplicationStatus.INTERESTED:
        return 0
    if status is UserApplicationStatus.NOT_APPLIED:
        return 1
    return 2


def user_status_exclusion_reason(value: object) -> str | None:
    """Return a stable user-facing reason when status filters out automation."""

    if user_status_is_automation_eligible(value):
        return None
    try:
        status = UserApplicationStatus(str(value)).value
    except (TypeError, ValueError):
        status = "INVALID_OR_MISSING"
    return f"User status {status} excludes this opportunity from automation"


class ApplicationState(StrEnum):
    DISCOVERED = "DISCOVERED"
    ELIGIBILITY_CHECKED = "ELIGIBILITY_CHECKED"
    QUEUED = "QUEUED"
    PACKAGE_PREPARED = "PACKAGE_PREPARED"
    FILLING = "FILLING"
    NEEDS_USER = "NEEDS_USER"
    NEEDS_OA = "NEEDS_OA"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    BLOCKED = "BLOCKED"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    CONFIRMATION_VERIFIED = "CONFIRMATION_VERIFIED"
    OA_PENDING = "OA_PENDING"
    INTERVIEW = "INTERVIEW"
    REJECTED = "REJECTED"
    OFFER = "OFFER"


_ALLOWED_TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    ApplicationState.DISCOVERED: frozenset({ApplicationState.ELIGIBILITY_CHECKED}),
    ApplicationState.ELIGIBILITY_CHECKED: frozenset(
        {ApplicationState.QUEUED, ApplicationState.BLOCKED}
    ),
    ApplicationState.QUEUED: frozenset(
        {ApplicationState.PACKAGE_PREPARED, ApplicationState.NEEDS_USER, ApplicationState.BLOCKED}
    ),
    ApplicationState.PACKAGE_PREPARED: frozenset(
        {ApplicationState.FILLING, ApplicationState.NEEDS_USER, ApplicationState.BLOCKED}
    ),
    ApplicationState.FILLING: frozenset(
        {
            ApplicationState.NEEDS_USER,
            ApplicationState.NEEDS_OA,
            ApplicationState.FAILED_RETRYABLE,
            ApplicationState.BLOCKED,
            ApplicationState.READY_TO_SUBMIT,
            ApplicationState.SUBMISSION_UNKNOWN,
        }
    ),
    ApplicationState.NEEDS_USER: frozenset(
        {ApplicationState.PACKAGE_PREPARED, ApplicationState.QUEUED, ApplicationState.BLOCKED}
    ),
    ApplicationState.NEEDS_OA: frozenset(
        {ApplicationState.OA_PENDING, ApplicationState.INTERVIEW, ApplicationState.REJECTED}
    ),
    ApplicationState.FAILED_RETRYABLE: frozenset(
        {ApplicationState.QUEUED, ApplicationState.FILLING, ApplicationState.BLOCKED}
    ),
    ApplicationState.BLOCKED: frozenset(
        {ApplicationState.ELIGIBILITY_CHECKED, ApplicationState.QUEUED}
    ),
    ApplicationState.READY_TO_SUBMIT: frozenset(
        {ApplicationState.SUBMITTED, ApplicationState.NEEDS_USER, ApplicationState.BLOCKED, ApplicationState.SUBMISSION_UNKNOWN}
    ),
    ApplicationState.SUBMITTED: frozenset(
        {
            ApplicationState.CONFIRMATION_VERIFIED,
            ApplicationState.SUBMISSION_UNKNOWN,
            ApplicationState.FAILED_RETRYABLE,
            ApplicationState.BLOCKED,
        }
    ),
    # SUBMISSION_UNKNOWN is deliberately near-terminal: the only exits are
    # human reconciliation (CONFIRMATION_VERIFIED via evidence) or BLOCKED
    # for record-keeping.  No automatic retry edge exists — a duplicate
    # submission must be impossible.
    ApplicationState.SUBMISSION_UNKNOWN: frozenset(
        {
            ApplicationState.BLOCKED,
        }
    ),
    ApplicationState.CONFIRMATION_VERIFIED: frozenset(
        {
            ApplicationState.OA_PENDING,
            ApplicationState.INTERVIEW,
            ApplicationState.REJECTED,
            ApplicationState.OFFER,
        }
    ),
    ApplicationState.OA_PENDING: frozenset(
        {ApplicationState.INTERVIEW, ApplicationState.REJECTED}
    ),
    ApplicationState.INTERVIEW: frozenset(
        {ApplicationState.OFFER, ApplicationState.REJECTED}
    ),
    ApplicationState.REJECTED: frozenset(),
    ApplicationState.OFFER: frozenset(),
}


class InvalidTransition(ValueError):
    pass


def validate_transition(current: ApplicationState, target: ApplicationState) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"Invalid application transition: {current.value} -> {target.value}")
