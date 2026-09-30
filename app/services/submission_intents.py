"""Durable, one-shot submission-intent lifecycle.

The service is the persistence-side half of the final-click boundary. A
``PREPARED`` row is committed before the browser click; ``CLICKED`` is
committed at the boundary; only a correlated receipt can produce
``CONFIRMED``. Any callback error, timeout, crash window, or missing evidence
becomes terminal ``UNKNOWN``. ``PENDING`` remains accepted as a legacy model
value for databases created before the stricter contract.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Application, SubmissionIntent


PREPARED = "PREPARED"
LEGACY_PREPARED = SubmissionIntent.PENDING
CLICKED = SubmissionIntent.CLICKED
CONFIRMED = SubmissionIntent.CONFIRMED
UNKNOWN = SubmissionIntent.UNKNOWN
FAILED_LOCAL = SubmissionIntent.FAILED_LOCAL
_PREPARED_STATUSES = frozenset({PREPARED, LEGACY_PREPARED})
_TERMINAL_STATUSES = frozenset({CONFIRMED, UNKNOWN})

_T = TypeVar("_T")


class IntentExistsError(RuntimeError):
    """A terminal or in-flight intent already owns this application."""


class IntentStateError(RuntimeError):
    """An intent lifecycle operation was attempted in an illegal state."""


class SubmissionUncertainError(RuntimeError):
    """The click boundary was crossed but its external result is unknown."""

    def __init__(self, message: str, *, intent_id: str = "") -> None:
        super().__init__(message)
        self.intent_id = intent_id


class SubmissionIntentService:
    """Persist and enforce one final-click attempt per application."""

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def fingerprint_manifest(manifest: dict[str, object]) -> str:
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _existing(self, application_id: str) -> SubmissionIntent | None:
        return self.session.scalar(
            select(SubmissionIntent).where(
                SubmissionIntent.application_id == application_id
            )
        )

    @staticmethod
    def _refuse(existing: SubmissionIntent, application_id: str) -> None:
        raise IntentExistsError(
            f"A terminal submission attempt already exists for application "
            f"{application_id} (status {existing.status}); one terminal attempt "
            "per application is enforced"
        )

    def create_intent(
        self,
        *,
        application_id: str,
        attempt_id: str,
        manifest: dict[str, object],
        destination_url: str,
        submission_control_selector: str = "",
        durable: bool = True,
    ) -> SubmissionIntent:
        """Create and durably persist one ``PREPARED`` intent.

        ``create_intent`` is retained as the historical public entry point;
        new runner code should call :meth:`prepare_intent` for readability.
        ``durable=True`` commits the row before the caller can cross the click
        boundary. A prior ``FAILED_LOCAL`` row is the sole reusable case.
        """

        existing = self._existing(application_id)
        if not durable:
            raise IntentStateError(
                "Submission intents must be durable; durable=False is forbidden"
            )
        if existing is not None and existing.status != FAILED_LOCAL:
            self._refuse(existing, application_id)
        manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        intent = SubmissionIntent(
            application_id=application_id,
            attempt_id=attempt_id,
            nonce=str(uuid.uuid4()),
            manifest_fingerprint=self.fingerprint_manifest(manifest),
            manifest_json=manifest_json,
            destination_url=destination_url,
            submission_control_selector=submission_control_selector,
            # ``PREPARED`` is a service-level status. The model's old default
            # is still PENDING for direct legacy construction and migrations.
            status=PREPARED,
        )
        if existing is not None:
            self.session.delete(existing)
            self.session.flush()
        self.session.add(intent)
        try:
            self.session.flush()
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise IntentExistsError(
                f"A concurrent submission intent already owns application {application_id}"
            ) from exc
        return intent

    def prepare_intent(self, **kwargs: object) -> SubmissionIntent:
        """Named strict API for the pre-click durable prepare boundary."""

        return self.create_intent(**kwargs)

    @staticmethod
    def _require_status(intent: SubmissionIntent, allowed: frozenset[str], operation: str) -> None:
        if intent.status not in allowed:
            raise IntentStateError(
                f"Cannot {operation} intent {intent.id}: status is {intent.status}"
            )

    @staticmethod
    def _refresh_after_cas_loss(session: Session, intent: SubmissionIntent) -> None:
        """Rollback local state and expose the winner of a failed CAS."""

        session.rollback()
        try:
            session.refresh(intent)
        except Exception as exc:  # pragma: no cover - row deletion is exceptional
            raise IntentStateError(
                f"Submission intent {intent.id} disappeared during its transition"
            ) from exc

    def _cas_status_pending(
        self,
        intent: SubmissionIntent,
        *,
        target_status: str,
        operation: str,
        resolved: bool = False,
    ) -> None:
        """Compare-and-set one intent row in the caller's transaction.

        Unlike :meth:`_cas_status` this never commits or rolls back. The
        caller owns the commit so application confirmation and intent
        confirmation can persist atomically. On a CAS loss the intent row
        is refreshed (no rollback) to expose the concurrent winner, then
        :class:`IntentStateError` is raised and the caller must roll back
        its transaction before reconciling to uncertainty.
        """

        expected_status = str(intent.status)
        if expected_status not in _PREPARED_STATUSES | {CLICKED}:
            self._require_status(intent, frozenset(_PREPARED_STATUSES | {CLICKED}), operation)
        values: dict[str, object] = {"status": target_status}
        if resolved:
            values["resolved_at"] = datetime.now(timezone.utc)
        with self.session.no_autoflush:
            result = self.session.execute(
                update(SubmissionIntent)
                .where(
                    SubmissionIntent.id == intent.id,
                    SubmissionIntent.status == expected_status,
                )
                .values(**values)
            )
        if result.rowcount != 1:
            try:
                self.session.refresh(intent)
            except Exception as exc:  # pragma: no cover - row deletion is exceptional
                raise IntentStateError(
                    f"Submission intent {intent.id} disappeared during its transition"
                ) from exc
            raise IntentStateError(
                f"Cannot {operation} intent {intent.id}: another session owns status "
                f"{intent.status}"
            )
        self.session.flush()

    def _cas_status(
        self,
        intent: SubmissionIntent,
        *,
        target_status: str,
        operation: str,
        resolved: bool = False,
    ) -> None:
        """Compare-and-set one intent row across sessions/processes."""

        expected_status = str(intent.status)
        if expected_status not in _PREPARED_STATUSES | {CLICKED}:
            self._require_status(intent, frozenset(_PREPARED_STATUSES | {CLICKED}), operation)
        values: dict[str, object] = {"status": target_status}
        if resolved:
            values["resolved_at"] = datetime.now(timezone.utc)
        with self.session.no_autoflush:
            result = self.session.execute(
                update(SubmissionIntent)
                .where(
                    SubmissionIntent.id == intent.id,
                    SubmissionIntent.status == expected_status,
                )
                .values(**values)
            )
        if result.rowcount != 1:
            self._refresh_after_cas_loss(self.session, intent)
            raise IntentStateError(
                f"Cannot {operation} intent {intent.id}: another session owns status "
                f"{intent.status}"
            )
        self.session.commit()
        self.session.refresh(intent)

    def mark_clicked(self, intent: SubmissionIntent, *, durable: bool = True) -> None:
        """Commit ``CLICKED`` immediately at the external-click boundary.

        This method must be called once, immediately before invoking the
        adapter's bound final-control click. Calling it twice is refused and
        therefore cannot create a duplicate browser side effect.
        """

        if not durable:
            raise IntentStateError(
                "Submission intents must be durable; durable=False is forbidden"
            )
        self._require_status(intent, _PREPARED_STATUSES, "mark CLICKED")
        self._cas_status(intent, target_status=CLICKED, operation="mark CLICKED")

    def execute_click(
        self,
        intent: SubmissionIntent,
        click: Callable[[], _T],
    ) -> _T:
        """Cross the durable click boundary exactly once and invoke ``click``.

        The callback is intentionally injected so contract tests can use a
        loopback side effect without Playwright. Any exception after the
        boundary is converted to ``UNKNOWN`` before being re-raised as
        :class:`SubmissionUncertainError`.
        """

        self.mark_clicked(intent)
        try:
            return click()
        except BaseException as exc:  # noqa: BLE001 - all post-click failures are uncertain
            try:
                self.mark_unknown(intent)
            except BaseException:
                # Never turn a persistence failure into a retryable result.
                pass
            raise SubmissionUncertainError(
                "Submission click crossed the boundary but its outcome is unknown",
                intent_id=str(intent.id),
            ) from exc

    def mark_confirmed(self, intent: SubmissionIntent, **_kwargs: object) -> None:
        """Deny confirmation bypasses; use :meth:`confirm_with_receipt`."""

        raise IntentStateError(
            "Direct confirmation is forbidden; provide strict ReceiptEvidence to confirm_with_receipt"
        )

    def confirm_with_receipt(
        self,
        intent: SubmissionIntent,
        before: Any,
        after: Any,
        *,
        bound_target: Any = None,
        bound_intent: Any = None,
        expected_final_url: str = "",
        durable: bool = True,
    ) -> bool:
        """Validate exact receipt evidence and mark the intent confirmed."""

        from app.automation.receipts import receipt_is_correlated

        if not durable:
            raise IntentStateError(
                "Submission intents must be durable; durable=False is forbidden"
            )
        if bound_intent is None:
            bound_intent = intent
        if not receipt_is_correlated(
            before,
            after,
            bound_target=bound_target,
            bound_intent=bound_intent,
            expected_final_url=expected_final_url,
        ):
            raise IntentStateError(
                "Receipt evidence is not correlated to the exact bound submission"
            )
        self._require_status(intent, frozenset({CLICKED}), "mark CONFIRMED")
        self._cas_status(
            intent,
            target_status=CONFIRMED,
            operation="mark CONFIRMED",
            resolved=True,
        )
        return True

    def confirm_with_receipt_pending(
        self,
        intent: SubmissionIntent,
        before: Any,
        after: Any,
        *,
        bound_target: Any = None,
        bound_intent: Any = None,
        expected_final_url: str = "",
        durable: bool = True,
    ) -> bool:
        """Validate exact receipt evidence and stage CONFIRMED without commit.

        The ``CLICKED -> CONFIRMED`` compare-and-set is flushed in the
        caller's transaction so the caller can commit application terminal
        confirmation and intent confirmation atomically. The caller owns
        the commit (and the rollback on failure). No independent session
        is opened here, so this must only be called after browser execution
        has finished; never hold this transaction across a browser click.
        """

        from app.automation.receipts import receipt_is_correlated

        if not durable:
            raise IntentStateError(
                "Submission intents must be durable; durable=False is forbidden"
            )
        if bound_intent is None:
            bound_intent = intent
        if not receipt_is_correlated(
            before,
            after,
            bound_target=bound_target,
            bound_intent=bound_intent,
            expected_final_url=expected_final_url,
        ):
            raise IntentStateError(
                "Receipt evidence is not correlated to the exact bound submission"
            )
        self._require_status(intent, frozenset({CLICKED}), "mark CONFIRMED")
        self._cas_status_pending(
            intent,
            target_status=CONFIRMED,
            operation="mark CONFIRMED",
            resolved=True,
        )
        return True

    def mark_unknown(
        self,
        intent: SubmissionIntent,
        *,
        durable: bool = True,
    ) -> None:
        """Mark every post-click ambiguity terminal and non-retryable."""

        if not durable:
            raise IntentStateError(
                "Submission intents must be durable; durable=False is forbidden"
            )
        if intent.status == UNKNOWN:
            return
        self._require_status(
            intent,
            frozenset((*_PREPARED_STATUSES, CLICKED)),
            "mark SUBMISSION_UNKNOWN",
        )
        self._cas_status(
            intent,
            target_status=UNKNOWN,
            operation="mark SUBMISSION_UNKNOWN",
            resolved=True,
        )
        application = self.session.get(Application, intent.application_id)
        if application is not None:
            current_value = getattr(application.state, "value", application.state)
            if current_value != "SUBMISSION_UNKNOWN":
                from app.domain.states import ApplicationState, validate_transition

                current = ApplicationState(current_value)
                target = ApplicationState.SUBMISSION_UNKNOWN
                try:
                    validate_transition(current, target)
                except Exception:
                    # A post-click ambiguity must not be relabelled as a local
                    # retryable failure. The near-terminal marker is the safe
                    # truth even for a legacy state omitted from the graph.
                    application.state = target.value
                else:
                    application.state = target.value
        self.session.commit()

    def mark_failed_local(
        self,
        intent: SubmissionIntent,
        *,
        click_issued: bool = False,
        durable: bool = True,
    ) -> None:
        """Record only a definite pre-click refusal.

        ``click_issued=True`` is rejected so callers cannot accidentally turn a
        crash window into a retryable local failure.
        """

        if not durable:
            raise IntentStateError(
                "Submission intents must be durable; durable=False is forbidden"
            )
        if click_issued:
            raise IntentStateError(
                "A click that may have been issued must be SUBMISSION_UNKNOWN, not FAILED_LOCAL"
            )
        self._require_status(intent, _PREPARED_STATUSES, "mark FAILED_LOCAL")
        self._cas_status(
            intent,
            target_status=FAILED_LOCAL,
            operation="mark FAILED_LOCAL",
            resolved=True,
        )

    def can_start(self, application_id: str) -> bool:
        """Return whether no in-flight/terminal intent blocks a new click."""

        existing = self._existing(application_id)
        return existing is None or existing.status == FAILED_LOCAL
