import pytest

from app.domain.states import ApplicationState, InvalidTransition, validate_transition


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ApplicationState.DISCOVERED, ApplicationState.ELIGIBILITY_CHECKED),
        (ApplicationState.ELIGIBILITY_CHECKED, ApplicationState.QUEUED),
        (ApplicationState.QUEUED, ApplicationState.PACKAGE_PREPARED),
        (ApplicationState.PACKAGE_PREPARED, ApplicationState.FILLING),
        (ApplicationState.FILLING, ApplicationState.NEEDS_USER),
        (ApplicationState.FILLING, ApplicationState.NEEDS_OA),
        (ApplicationState.FILLING, ApplicationState.FAILED_RETRYABLE),
        (ApplicationState.FILLING, ApplicationState.BLOCKED),
        (ApplicationState.FILLING, ApplicationState.READY_TO_SUBMIT),
        (ApplicationState.READY_TO_SUBMIT, ApplicationState.SUBMITTED),
        (ApplicationState.SUBMITTED, ApplicationState.CONFIRMATION_VERIFIED),
        (ApplicationState.CONFIRMATION_VERIFIED, ApplicationState.OA_PENDING),
        (ApplicationState.CONFIRMATION_VERIFIED, ApplicationState.INTERVIEW),
        (ApplicationState.OA_PENDING, ApplicationState.INTERVIEW),
        (ApplicationState.INTERVIEW, ApplicationState.OFFER),
        (ApplicationState.INTERVIEW, ApplicationState.REJECTED),
        (ApplicationState.NEEDS_USER, ApplicationState.PACKAGE_PREPARED),
        (ApplicationState.FAILED_RETRYABLE, ApplicationState.QUEUED),
    ],
)
def test_valid_transitions_are_accepted(current: ApplicationState, target: ApplicationState) -> None:
    validate_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ApplicationState.DISCOVERED, ApplicationState.SUBMITTED),
        (ApplicationState.QUEUED, ApplicationState.SUBMITTED),
        (ApplicationState.NEEDS_OA, ApplicationState.SUBMITTED),
        (ApplicationState.REJECTED, ApplicationState.OFFER),
        (ApplicationState.OFFER, ApplicationState.FILLING),
    ],
)
def test_invalid_transitions_fail_closed(current: ApplicationState, target: ApplicationState) -> None:
    with pytest.raises(InvalidTransition, match=f"{current.value}.*{target.value}"):
        validate_transition(current, target)
