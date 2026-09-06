"""Scout control centre page: sweep status, queue summary, one-click actions."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from app.models import Application, Opportunity
from app.routers.pages import _base, templates
from app.scouting.programmes import AUTO_APPLY_PRIORITY

router = APIRouter(tags=["scout-pages"])


@router.get("/scout", response_class=HTMLResponse)
def scout_page(request: Request):
    from app.routers.pages_v2 import ui_v2_enabled

    if ui_v2_enabled(request) and request.query_params.get("legacy") != "1":
        return RedirectResponse("/pipeline", status_code=307)
    with request.app.state.db.session_scope() as session:
        # programme mix of everything tracked
        rows = session.execute(
            select(Opportunity.programme_group, func.count()).group_by(
                Opportunity.programme_group
            )
        ).all()
        mix = {group or "unknown": count for group, count in rows}

        pending = session.scalar(
            select(func.count())
            .select_from(Opportunity)
            .where(Opportunity.url.like("trackr-pending://%"))
        )

        needs_user = session.execute(
            select(Application, Opportunity)
            .join(Opportunity, Application.opportunity_id == Opportunity.id)
            .where(Application.state.in_(("NEEDS_USER", "NEEDS_OA")))
            .order_by(Application.priority)
            .limit(50)
        ).all()

        blocked = session.execute(
            select(Application, Opportunity)
            .join(Opportunity, Application.opportunity_id == Opportunity.id)
            .where(Application.state == "BLOCKED")
            .order_by(Application.priority)
            .limit(30)
        ).all()

    return templates.TemplateResponse(
        request,
        "pages/scout.html",
        {
            **_base(request, title="Scout", active="scout"),
            "mix": mix,
            "pending_count": pending or 0,
            "needs_user": needs_user,
            "blocked": blocked,
            "auto_priority": AUTO_APPLY_PRIORITY,
            "trackr_live_enabled": bool(
                getattr(request.app.state.settings, "trackr_live_enabled", False)
            ),
        },
    )
