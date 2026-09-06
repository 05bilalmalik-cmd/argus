"""A BLOCKED row must not offer PREFILL; it needs human re-evaluation.

Per the egress wall fix (Phase 37), the runner refuses BLOCKED rows with
``resume_not_allowed``.  The button must agree with the engine, so BLOCKED
is not in ``PREFILL_APPLICATION_STATES``.  The row should surface the
blocker-resolution control (``/resolve-blocker``) instead.
"""
from __future__ import annotations

import pytest

from app.services.prefill import (
    PREFILL_APPLICATION_STATES,
    prefill_start_reason,
    prefill_ui_allowed,
)

FORM = "APPLICATION_FORM"


def _reason(**overrides: object) -> tuple[str, str] | None:
    kwargs: dict[str, object] = {
        "target_status": FORM,
        "user_status": "NOT_APPLIED",
        "application_state": "BLOCKED",
        "live_session": False,
    }
    kwargs.update(overrides)
    return prefill_start_reason(**kwargs)  # type: ignore[arg-type]


def test_blocked_row_is_not_eligible_for_prefill() -> None:
    """BLOCKED is intentionally excluded from PREFILL_APPLICATION_STATES per
    the Phase 37 egress wall fix.  The button must not offer PREFILL; the
    row should surface /resolve-blocker instead."""
    assert "BLOCKED" not in PREFILL_APPLICATION_STATES
    outcome = _reason()
    assert outcome is not None
    assert outcome[0] == "application_state_not_prepareable"
    assert not prefill_ui_allowed(
        target_status=FORM,
        user_status="NOT_APPLIED",
        application_state="BLOCKED",
    )


@pytest.mark.parametrize(
    "user_status",
    [
        "NOT_INTERESTED",
        "APPLICATION_SUBMITTED",
        "HIREVUE",
        "ONLINE_ASSESSMENT",
        "ONLINE_TEST",
        "FIRST_ROUND",
        "OFFER",
        "REJECTED",
    ],
)
def test_candidate_status_still_wins_over_blocked(user_status: str) -> None:
    """A candidate status still wins even for a blocked row."""

    outcome = _reason(user_status=user_status)
    assert outcome is not None
    assert outcome[0] == "user_status_excluded"
    assert not prefill_ui_allowed(
        target_status=FORM,
        user_status=user_status,
        application_state="BLOCKED",
    )


@pytest.mark.parametrize(
    "target_status",
    ["UNRESOLVED", "JOB_DETAIL", "LISTING", "AUTH_WALL", "MISSING_EMPLOYER_LINK"],
)
def test_blocked_row_short_of_a_form_is_still_refused(target_status: str) -> None:
    outcome = _reason(target_status=target_status)
    assert outcome is not None
    assert outcome[0] == "target_not_application_form"


def test_blocked_row_with_a_live_session_offers_continue_instead() -> None:
    outcome = _reason(live_session=True)
    assert outcome is not None
    assert outcome[0] == "prefill_session_active"


def test_states_that_are_not_a_failed_attempt_stay_refused() -> None:
    """Widening covers failed attempts only, not the whole state machine."""

    for state in ("DISCOVERED", "SUBMITTED", "CONFIRMATION_VERIFIED", "NEEDS_OA"):
        outcome = _reason(application_state=state)
        assert outcome is not None, state
        assert outcome[0] == "application_state_not_prepareable", state