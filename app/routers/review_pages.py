from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.domain.states import ApplicationState
from app.models import Application, Opportunity
from app.routers.pages import _base

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

_REVIEW_STATES = (
    ApplicationState.NEEDS_USER.value,
    ApplicationState.NEEDS_OA.value,
    ApplicationState.READY_TO_SUBMIT.value,
)

_REVIEW_QUEUE_LIMIT = 100
_CONFIRM_BATCH_CAP = 25


@router.get("/review-queue", response_class=HTMLResponse)
def review_queue_page(request: Request):
    """Read-only HTML dashboard for the review queue.

    This route never submits anything: it only projects the same read-only
    data as GET /api/review-queue into the ``pages/review_queue.html``
    template.  Confirmation (which may lead to a real submission) happens
    exclusively through POST /api/review-queue/confirm, driven by the
    page's explicit checkbox + warning controls.
    """
    with request.app.state.db.session_scope() as session:
        state_counts: dict[str, int] = dict(
            session.execute(
                select(Application.state, func.count(Application.id)).group_by(Application.state)
            ).all()
        )
        counts = {state: state_counts.get(state, 0) for state in _REVIEW_STATES}
        rows = (
            session.execute(
                select(Application, Opportunity)
                .join(Opportunity, Application.opportunity_id == Opportunity.id)
                .where(Application.state.in_(_REVIEW_STATES))
                .order_by(Application.updated_at.desc().nulls_last(), Application.id)
                .limit(_REVIEW_QUEUE_LIMIT)
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
        return templates.TemplateResponse(
            request,
            "pages/review_queue.html",
            {
                **_base(request, title="Review Queue", active="review-queue"),
                "counts": counts,
                "items": items,
                "confirm_cap": _CONFIRM_BATCH_CAP,
            },
        )
