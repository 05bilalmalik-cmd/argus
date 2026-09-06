from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.states import ApplicationState
from app.models import Application, Opportunity


@dataclass(frozen=True, slots=True)
class UrgentAction:
    application_id: str
    employer: str
    role_title: str
    action: str
    deadline: datetime
    state: str


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    total_applications: int
    total_opportunities: int
    needs_user: int
    oa_pending: int
    submitted: int
    pipeline: dict[str, int]
    urgent_actions: tuple[UrgentAction, ...]


class DashboardService:
    def __init__(self, session: Session):
        self.session = session

    def snapshot(self) -> DashboardSnapshot:
        pipeline = {state.value: 0 for state in ApplicationState}
        for state, count in self.session.execute(
            select(Application.state, func.count(Application.id)).group_by(Application.state)
        ).all():
            pipeline[state] = count
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=7)
        rows = self.session.execute(
            select(Application, Opportunity)
            .join(Opportunity, Application.opportunity_id == Opportunity.id)
            .where(
                Application.next_action_deadline.is_not(None),
                Application.next_action_deadline <= horizon,
            )
            .order_by(Application.next_action_deadline.asc())
        ).all()
        urgent = tuple(
            UrgentAction(
                application_id=application.id,
                employer=opportunity.employer,
                role_title=opportunity.role_title,
                action=application.next_action,
                deadline=application.next_action_deadline,
                state=application.state,
            )
            for application, opportunity in rows
            if application.next_action_deadline is not None
        )
        total_opportunities = self.session.scalar(select(func.count(Opportunity.id))) or 0
        return DashboardSnapshot(
            total_applications=sum(pipeline.values()),
            total_opportunities=total_opportunities,
            needs_user=(
                pipeline[ApplicationState.NEEDS_USER.value]
                + pipeline[ApplicationState.NEEDS_OA.value]
                + pipeline[ApplicationState.BLOCKED.value]
                + pipeline[ApplicationState.FAILED_RETRYABLE.value]
            ),
            oa_pending=(
                pipeline[ApplicationState.NEEDS_OA.value]
                + pipeline[ApplicationState.OA_PENDING.value]
            ),
            submitted=pipeline[ApplicationState.CONFIRMATION_VERIFIED.value],
            pipeline=pipeline,
            urgent_actions=urgent,
        )
