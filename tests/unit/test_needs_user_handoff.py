"""Regression tests for the NEEDS_USER handoff (headed browser re-open).

Covers the 2026-08-23 bug: clicking "Finish in browser" on the Needs-You page
re-runs automation from a terminal state (NEEDS_USER / NEEDS_OA / BLOCKED /
FAILED_RETRYABLE) and `_run_claimed` raised
"Application is not prepared for automation", so no browser ever opened and
the page stayed empty.

The fix lets a *headed review* run resume from those states by first walking
the application back into FILLING via state-machine-legal edges only.
"""
from __future__ import annotations

import pytest

from app.automation.runner import AutomationRunner, SubmissionBlocked
from app.automation.types import SessionState
from app.domain.states import ApplicationState


class _App:
    def __init__(self, state: ApplicationState) -> None:
        self.state = state.value
        self.id = "test-app"


def _recording_transition(transitions: list[tuple[str, str]]):
    def transition(_session, application, target):
        transitions.append((application.state, target.value))
        application.state = target.value

    return transition


class TestHandoffResumeStates:
    def test_terminal_handoff_states_are_resumable(self) -> None:
        assert AutomationRunner._resume_states() == {
            ApplicationState.NEEDS_USER,
            ApplicationState.NEEDS_OA,
            ApplicationState.BLOCKED,
            ApplicationState.FAILED_RETRYABLE,
        }

    @pytest.mark.parametrize(
        "state",
        [
            ApplicationState.SUBMITTED,
            ApplicationState.CONFIRMATION_VERIFIED,
            ApplicationState.OA_PENDING,
            ApplicationState.INTERVIEW,
            ApplicationState.OFFER,
            ApplicationState.DISCOVERED,
            ApplicationState.ELIGIBILITY_CHECKED,
        ],
    )
    def test_success_and_pre_queue_states_are_never_resumable(self, state) -> None:
        assert state not in AutomationRunner._resume_states()


class TestFinalReviewSnapshotSettlement:
    def test_final_review_waits_for_matching_owner_state(self) -> None:
        final_snapshot = object()

        class Navigator:
            def wait_for_state(self, session_id, state, *, timeout):
                assert session_id == "review-session"
                assert state is SessionState.FINAL_REVIEW
                assert timeout == 1
                return final_snapshot

            def get(self, _session_id):
                raise AssertionError("FINAL_REVIEW must use the bounded state wait")

        observed = AutomationRunner._settled_handoff_snapshot(
            Navigator(),
            "review-session",
            "FINAL_REVIEW",
        )
        assert observed is final_snapshot

    def test_non_final_result_uses_immediate_snapshot(self) -> None:
        current_snapshot = object()

        class Navigator:
            def wait_for_state(self, *_args, **_kwargs):
                raise AssertionError("non-final result must not wait for FINAL_REVIEW")

            def get(self, session_id):
                assert session_id == "human-session"
                return current_snapshot

        observed = AutomationRunner._settled_handoff_snapshot(
            Navigator(),
            "human-session",
            "NEEDS_USER",
        )
        assert observed is current_snapshot


class TestResumeTransitions:
    """Every resume walk uses legal edges of the real state machine."""

    def test_resume_from_needs_user(self) -> None:
        transitions: list[tuple[str, str]] = []
        app = _App(ApplicationState.NEEDS_USER)
        AutomationRunner._resume_for_headed_review(
            object.__new__(AutomationRunner), None, app, transition=_recording_transition(transitions)
        )
        assert app.state == ApplicationState.FILLING.value
        assert transitions == [
            ("NEEDS_USER", "PACKAGE_PREPARED"),
            ("PACKAGE_PREPARED", "FILLING"),
        ]

    def test_resume_from_blocked_walks_legal_edges(self) -> None:
        transitions: list[tuple[str, str]] = []
        app = _App(ApplicationState.BLOCKED)
        # Validate every edge against the real state machine as we go.
        from app.domain.states import validate_transition

        def checked_transition(_session, application, target):
            validate_transition(ApplicationState(application.state), target)
            _recording_transition(transitions)(_session, application, target)

        runner = object.__new__(AutomationRunner)
        AutomationRunner._resume_for_headed_review(runner, None, app, transition=checked_transition)
        assert app.state == ApplicationState.FILLING.value
        assert transitions[0] == ("BLOCKED", "ELIGIBILITY_CHECKED")
        assert transitions[-1] == ("PACKAGE_PREPARED", "FILLING")

    def test_resume_from_failed_retryable_is_direct(self) -> None:
        app = _App(ApplicationState.FAILED_RETRYABLE)
        calls: list[int] = []

        def transition(_s, application, target):
            calls.append(1)
            application.state = target.value

        AutomationRunner._resume_for_headed_review(
            object.__new__(AutomationRunner), None, app, transition=transition
        )
        assert app.state == ApplicationState.FILLING.value
        assert len(calls) == 1  # single legal edge FAILED_RETRYABLE -> FILLING

    def test_resume_from_filling_is_noop(self) -> None:
        app = _App(ApplicationState.FILLING)
        called: list[int] = []
        AutomationRunner._resume_for_headed_review(
            object.__new__(AutomationRunner),
            None,
            app,
            transition=lambda *_a, **_k: called.append(1),
        )
        assert not called

    def test_resume_refuses_submitted(self) -> None:
        app = _App(ApplicationState.SUBMITTED)
        with pytest.raises(SubmissionBlocked):
            AutomationRunner._resume_for_headed_review(
                object.__new__(AutomationRunner), None, app, transition=lambda *_a: None
            )

    def test_resume_refuses_oa_pending(self) -> None:
        app = _App(ApplicationState.NEEDS_OA)
        with pytest.raises(SubmissionBlocked):
            AutomationRunner._resume_for_headed_review(
                object.__new__(AutomationRunner), None, app, transition=lambda *_a: None
            )

    def test_resume_refuses_discovered(self) -> None:
        app = _App(ApplicationState.DISCOVERED)
        with pytest.raises(SubmissionBlocked):
            AutomationRunner._resume_for_headed_review(
                object.__new__(AutomationRunner), None, app, transition=lambda *_a: None
            )
