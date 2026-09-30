from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.automation.runner import AutomationRunner
from app.domain.states import ApplicationState
from app.models import Application, Opportunity
from app.routers.deps import SessionDep
from app.scouting.service import ScoutService

router = APIRouter(prefix="/api", tags=["api"])


class ConfirmReviewQueueBody(BaseModel):
    """Human confirmation of a reviewed batch of applications."""

    model_config = ConfigDict(extra="forbid")

    application_ids: list[str] = Field(default_factory=list)


def _shared_navigator(request: Request):  # noqa: ANN001 - FastAPI app state
    """Return the one navigator owned by ``create_app``.

    Scout/autopilot runs may pause for a CAPTCHA, MFA challenge, declaration,
    or another human-only boundary.  Those pauses must remain visible to the
    handoff routes, so these entry points must never create or silently fall
    back to a second handoff manager.  A missing navigator is a wiring error;
    fail closed instead of running automation with an unobservable session.
    """

    try:
        navigator = request.app.state.navigator
    except AttributeError as exc:
        raise RuntimeError(
            "ARGUS application navigator is not installed on app.state"
        ) from exc
    if navigator is None:
        raise RuntimeError("ARGUS application navigator is unavailable")
    return navigator


def _scout(session: Session, request: Request) -> ScoutService:
    return ScoutService(session, request.app.state.settings, request.app.state.crypto)


def _runner_factory(request: Request):
    def run(application_id: str, mode, headed: bool) -> dict[str, object]:
        runner = AutomationRunner(
            request.app.state.db,
            request.app.state.settings,
            request.app.state.crypto,
            handoff_manager=_shared_navigator(request),
        )
        try:
            return _outcome_dict(
                runner.run(application_id, mode, headed=headed)
            )
        except Exception:
            raise

    return run


def _outcome_dict(outcome) -> dict[str, object]:  # noqa: ANN001
    from dataclasses import asdict, is_dataclass

    if is_dataclass(outcome):
        payload = asdict(outcome)
        receipt = payload.get("receipt")
        if receipt is not None:
            payload["receipt"] = {k: v for k, v in receipt.items() if v}
        else:
            payload["receipt"] = None
        return payload
    return dict(outcome)


@router.get("/review-queue")
def get_review_queue(session: SessionDep) -> dict[str, object]:
    """Read-only review queue: counts by review status + capped list of awaiting items."""
    state_counts: dict[str, int] = dict(
        session.execute(
            select(Application.state, func.count(Application.id)).group_by(Application.state)
        ).all()
    )
    review_states = (
        ApplicationState.NEEDS_USER.value,
        ApplicationState.NEEDS_OA.value,
        ApplicationState.READY_TO_SUBMIT.value,
    )
    counts = {state: state_counts.get(state, 0) for state in review_states}
    rows = (
        session.execute(
            select(Application, Opportunity)
            .join(Opportunity, Application.opportunity_id == Opportunity.id)
            .where(Application.state.in_(review_states))
            .order_by(Application.updated_at.desc().nulls_last(), Application.id)
            .limit(100)
        )
        .all()
    )
    items = [
        {
            "id": application.id,
            "employer": opportunity.employer,
            "role": opportunity.role_title,
            "status": application.state,
            "prefilled_at": (
                application.updated_at.isoformat() if application.updated_at else None
            ),
        }
        for application, opportunity in rows
    ]
    return {"counts": counts, "items": items}


@router.post("/review-queue/confirm")
def confirm_review_queue(
    body: ConfirmReviewQueueBody,
    session: SessionDep,
    request: Request,
) -> dict[str, object]:
    """Confirm a reviewed batch and run it through the autopilot.

    The supplied ids are passed through as ``confirmed_application_ids`` with
    ``submit=True``.  Submission itself remains gated by the service: a Risk-0
    outcome proceeds to SUBMIT only when live mode is armed
    (``self.settings.submission_armed``).  This handler never touches, sets,
    or works around any settings flag.
    """
    seen: set[str] = set()
    unique_ids: list[str] = []
    for raw in body.application_ids or []:
        cleaned = raw.strip() if isinstance(raw, str) else ""
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        unique_ids.append(cleaned)
    if not unique_ids:
        raise HTTPException(
            status_code=400,
            detail="application_ids must be a non-empty list",
        )
    if len(unique_ids) > 25:
        raise HTTPException(
            status_code=400,
            detail="application_ids must contain at most 25 ids",
        )
    scout = _scout(session, request)
    return scout.run_autopilot(
        _runner_factory(request),
        max_runs=len(unique_ids),
        submit=True,
        confirmed_application_ids=unique_ids,
        application_scope=unique_ids,
    )
