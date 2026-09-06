"""Pipeline funnel telemetry: per-firm conversion from scrape to outcome.

Complements DashboardService (global state counts) with the per-employer view
the operator needs: where each firm's applications stall, and what the
scrape-to-submission conversion actually is.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.states import ApplicationState
from app.models import Application, Opportunity


@dataclass(frozen=True, slots=True)
class FirmFunnel:
    employer: str
    discovered: int = 0
    eligible: int = 0
    queued: int = 0  # QUEUED + PACKAGE_PREPARED + FILLING + READY_TO_SUBMIT
    submitted: int = 0  # SUBMITTED + CONFIRMATION_VERIFIED
    needs_user: int = 0  # NEEDS_USER + NEEDS_OA
    blocked: int = 0  # BLOCKED + FAILED_RETRYABLE
    oa_pending: int = 0
    open_roles: int = 0  # distinct opportunities not past deadline
    next_deadline: date | None = None

    @property
    def in_flight(self) -> int:
        return self.queued

    @property
    def conversion(self) -> float | None:
        total = (
            self.discovered
            + self.eligible
            + self.queued
            + self.submitted
            + self.needs_user
            + self.blocked
        )
        if total == 0:
            return None
        return round(self.submitted / total, 4)


_ELIGIBLE_STATES = {ApplicationState.ELIGIBILITY_CHECKED.value}
_QUEUED_STATES = {
    ApplicationState.QUEUED.value,
    ApplicationState.PACKAGE_PREPARED.value,
    ApplicationState.FILLING.value,
    ApplicationState.READY_TO_SUBMIT.value,
}
_SUBMITTED_STATES = {
    ApplicationState.SUBMITTED.value,
    ApplicationState.CONFIRMATION_VERIFIED.value,
}
_NEEDS_USER_STATES = {
    ApplicationState.NEEDS_USER.value,
    ApplicationState.NEEDS_OA.value,
}
_BLOCKED_STATES = {
    ApplicationState.BLOCKED.value,
    ApplicationState.FAILED_RETRYABLE.value,
}
_OA_STATES = {ApplicationState.NEEDS_OA.value, ApplicationState.OA_PENDING.value}


class FunnelService:
    def __init__(self, session: Session):
        self.session = session

    def per_firm(self) -> list[FirmFunnel]:
        rows = self.session.execute(
            select(
                Opportunity.employer,
                Application.state,
                func.count(Application.id),
            )
            .join(Application, Application.opportunity_id == Opportunity.id)
            .group_by(Opportunity.employer, Application.state)
        ).all()

        buckets: dict[str, dict[str, int]] = {}
        for employer, state, count in rows:
            firm = buckets.setdefault(employer.casefold(), {})
            firm[state] = count

        # deadlines + open role counts per employer - only roles whose
        # deadline has not passed count as open
        today = date.today()
        deadline_rows = self.session.execute(
            select(
                Opportunity.employer,
                func.count(Opportunity.id),
                func.min(Opportunity.deadline),
            )
            .where(Opportunity.deadline.is_not(None))
            .where(Opportunity.deadline >= today)
            .group_by(Opportunity.employer)
        ).all()
        deadlines: dict[str, tuple[int, date]] = {}
        for employer, count, earliest in deadline_rows:
            if earliest is not None:
                deadlines[employer.casefold()] = (count, earliest)

        funnels: list[FirmFunnel] = []
        for employer_key, states in buckets.items():
            funnel = FirmFunnel(
                employer=employer_key,
                discovered=states.get(ApplicationState.DISCOVERED.value, 0),
                eligible=sum(states.get(s, 0) for s in _ELIGIBLE_STATES),
                queued=sum(states.get(s, 0) for s in _QUEUED_STATES),
                submitted=sum(states.get(s, 0) for s in _SUBMITTED_STATES),
                needs_user=sum(states.get(s, 0) for s in _NEEDS_USER_STATES),
                blocked=sum(states.get(s, 0) for s in _BLOCKED_STATES),
                oa_pending=sum(states.get(s, 0) for s in _OA_STATES),
                open_roles=deadlines.get(employer_key, (0, None))[0],
                next_deadline=deadlines.get(employer_key, (0, None))[1],
            )
            funnels.append(funnel)
        funnels.sort(key=lambda f: (-f.submitted, -f.in_flight, f.employer))
        return funnels

    def totals(self) -> dict[str, object]:
        """Global funnel row across all firms."""
        counts = dict(
            self.session.execute(
                select(Application.state, func.count(Application.id)).group_by(
                    Application.state
                )
            ).all()
        )

        def pick(states: set[str]) -> int:
            return sum(counts.get(state, 0) for state in states)

        scraped = self.session.scalar(select(func.count(Opportunity.id))) or 0
        submitted = pick(_SUBMITTED_STATES)
        processed = scraped  # everything scraped is a potential application
        return {
            "scraped": scraped,
            "discovered": counts.get(ApplicationState.DISCOVERED.value, 0),
            "eligible": pick(_ELIGIBLE_STATES),
            "queued": pick(_QUEUED_STATES),
            "submitted": submitted,
            "needs_user": pick(_NEEDS_USER_STATES),
            "blocked": pick(_BLOCKED_STATES),
            "oa_pending": pick(_OA_STATES),
            "conversion": round(submitted / processed, 4) if processed else None,
        }
