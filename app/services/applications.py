from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date

from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.conflicts import (
    CandidateApplication,
    ConflictRuleSnapshot,
    ExistingApplication,
    evaluate_conflicts,
)
from app.domain.eligibility import OpportunitySnapshot, evaluate_eligibility
from app.domain.opportunity_scope import out_of_scope_programme_reason
from app.domain.states import (
    ApplicationState,
    user_status_exclusion_reason,
    validate_transition,
)
from app.models import Application, ConflictRule, Document, Opportunity
from app.repositories import ApplicationRepository
from app.scouting.programmes import resolve_programme_framing
from app.security.audit import AuditInput, append_audit
from app.security.crypto import CryptoBox
from app.services.documents import DocumentService
from app.services.profile import ProfileService


class ApplicationBlockedError(RuntimeError):
    pass


_PROTECTED_REEVALUATION_STATES = frozenset(
    {
        ApplicationState.NEEDS_OA,
        ApplicationState.SUBMITTED,
        ApplicationState.CONFIRMATION_VERIFIED,
        ApplicationState.OA_PENDING,
        ApplicationState.INTERVIEW,
        ApplicationState.REJECTED,
        ApplicationState.OFFER,
    }
)


@dataclass(frozen=True, slots=True)
class ApplicationDecision:
    application: Application
    eligible: bool
    requires_review: bool
    conflict_blocked: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedPackage:
    application: Application
    ready: bool
    cv_id: str | None
    cover_letter_id: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CvReselectionChange:
    application_id: str
    employer: str
    before_cv_id: str
    after_cv_id: str | None
    reason: str
    before_state: str
    after_state: str


@dataclass(frozen=True, slots=True)
class CvReselectionReport:
    scanned: int
    changed: int
    unchanged: int
    required_cv_missing: int
    changes: tuple[CvReselectionChange, ...]


@dataclass(frozen=True, slots=True)
class ApplicationPage:
    """A server-filtered application queue page with truthful totals."""

    records: tuple[Application, ...]
    total_count: int
    page: int
    per_page: int
    sort: str
    direction: str
    query: str
    state: str

    @property
    def total_pages(self) -> int:
        return max(1, (self.total_count + self.per_page - 1) // self.per_page)


def cv_variant_tag(programme_group: str | None) -> str | None:
    """Return the CV tag from the coupled programme-framing contract."""

    framing = resolve_programme_framing(programme_group)
    return framing.cv_variant_tag if framing is not None else None


def cv_preference_tags(opportunity: Opportunity) -> tuple[str, ...]:
    """Return contextual evidence used only after the CV variant hard filter."""

    employer_slug = (
        opportunity.employer.strip().casefold().replace(" ", "-").replace(".", "")
        if opportunity.employer
        else ""
    )
    return tuple(
        value
        for value in (
            employer_slug,
            *(
                value.strip().casefold().replace(" ", "-")
                for value in (opportunity.division, opportunity.location)
                if value.strip()
            ),
        )
        if value
    )


def select_cv_for_opportunity(
    document_service: DocumentService,
    opportunity: Opportunity,
    *,
    forbidden_body_phrases: tuple[str, ...] = (),
) -> Document | None:
    """Select a content-verified CV or return ``None`` without a fallback."""

    framing = resolve_programme_framing(opportunity.programme_group)
    if framing is None or not opportunity.cv_required:
        return None
    return document_service.select_approved(
        "cv",
        cv_preference_tags(opportunity),
        required_tag=framing.cv_variant_tag,
        employer=opportunity.employer,
        forbidden_body_phrases=forbidden_body_phrases,
    )


def reselect_existing_cvs(
    session: Session,
    settings: Settings,
    *,
    actor: str = "system",
    application_ids: frozenset[str] | None = None,
    forbidden_cv_body_phrases: tuple[str, ...] = (),
) -> CvReselectionReport:
    """Re-evaluate every existing CV binding under the current safety rules."""

    statement = (
        select(Application, Opportunity)
        .join(Opportunity, Application.opportunity_id == Opportunity.id)
        .where(Application.selected_cv_id.is_not(None))
        .order_by(Application.created_at, Application.id)
    )
    if application_ids is not None:
        statement = statement.where(Application.id.in_(application_ids))
    rows = session.execute(statement).all()
    document_service = DocumentService(session, settings.documents_dir)
    changes: list[CvReselectionChange] = []
    missing = 0
    for application, opportunity in rows:
        before_cv_id = application.selected_cv_id
        if before_cv_id is None:  # pragma: no cover - guarded by SQL predicate
            continue
        selected = select_cv_for_opportunity(
            document_service,
            opportunity,
            forbidden_body_phrases=forbidden_cv_body_phrases,
        )
        after_cv_id = selected.id if selected is not None else None
        if after_cv_id == before_cv_id:
            continue

        before_state = application.state
        reason = (
            "required_cv_missing"
            if after_cv_id is None
            else (
                "content_safety_reselection"
                if forbidden_cv_body_phrases
                else "employer_exclusivity_reselection"
            )
        )
        application.selected_cv_id = after_cv_id
        if after_cv_id is None:
            missing += 1
            if before_state == ApplicationState.BLOCKED.value:
                application.next_action = (
                    "Upload and approve an employer-compatible CV "
                    "(required_cv_missing; BLOCKED state preserved)"
                )
            else:
                application.state = ApplicationState.NEEDS_USER.value
                application.next_action = (
                    "Upload and approve an employer-compatible CV "
                    "(required_cv_missing)"
                )
        session.flush()

        if application.state != before_state:
            append_audit(
                session,
                AuditInput(
                    actor,
                    "application.state_changed",
                    "application",
                    application.id,
                    {
                        "from": before_state,
                        "to": application.state,
                        "reason": "required_cv_missing_after_employer_exclusivity_repair",
                    },
                ),
            )
        change = CvReselectionChange(
            application_id=application.id,
            employer=opportunity.employer,
            before_cv_id=before_cv_id,
            after_cv_id=after_cv_id,
            reason=reason,
            before_state=before_state,
            after_state=application.state,
        )
        changes.append(change)
        append_audit(
            session,
            AuditInput(
                actor,
                "application.cv_reselected",
                "application",
                application.id,
                {
                    "employer": opportunity.employer,
                    "before_cv_id": before_cv_id,
                    "after_cv_id": after_cv_id,
                    "reason": reason,
                    "before_state": before_state,
                    "after_state": application.state,
                },
            ),
        )

    session.flush()
    return CvReselectionReport(
        scanned=len(rows),
        changed=len(changes),
        unchanged=len(rows) - len(changes),
        required_cv_missing=missing,
        changes=tuple(changes),
    )


class ApplicationService:
    def __init__(self, session: Session, settings: Settings, crypto: CryptoBox):
        self.session = session
        self.settings = settings
        self.crypto = crypto
        self.repository = ApplicationRepository(session)

    def _transition(
        self,
        application: Application,
        target: ApplicationState,
        *,
        actor: str = "system",
        details: dict[str, object] | None = None,
    ) -> None:
        current = ApplicationState(application.state)
        if current == target:
            return
        validate_transition(current, target)
        application.state = target.value
        self.session.flush()
        append_audit(
            self.session,
            AuditInput(
                actor,
                "application.state_changed",
                "application",
                application.id,
                {"from": current.value, "to": target.value, **(details or {})},
            ),
        )

    def list_page(
        self,
        *,
        query: str = "",
        state: str = "",
        sort: str = "priority",
        direction: str = "desc",
        page: int = 1,
        per_page: int = 20,
    ) -> ApplicationPage:
        """Return one bounded application page, filtering in SQL."""

        page = max(1, int(page))
        per_page = min(100, max(1, int(per_page)))
        sort = sort if sort in {
            "priority",
            "updated_at",
            "created_at",
            "employer",
            "role_title",
            "state",
            "risk_level",
        } else "priority"
        direction = "asc" if direction.casefold() == "asc" else "desc"
        query = query.strip()
        state = state.strip().upper()

        filters = []
        if query:
            pattern = f"%{query}%"
            filters.append(
                or_(
                    Opportunity.employer.ilike(pattern),
                    Opportunity.role_title.ilike(pattern),
                    Opportunity.division.ilike(pattern),
                    Opportunity.location.ilike(pattern),
                )
            )
        if state:
            filters.append(Application.state == state)

        count = self.session.scalar(
            select(func.count(Application.id)).join(Opportunity).where(*filters)
        ) or 0
        total_pages = max(1, (int(count) + per_page - 1) // per_page)
        page = min(page, total_pages)
        sort_column = {
            "priority": Application.priority,
            "updated_at": Application.updated_at,
            "created_at": Application.created_at,
            "employer": Opportunity.employer,
            "role_title": Opportunity.role_title,
            "state": Application.state,
            "risk_level": Application.risk_level,
        }[sort]
        ordering = asc(sort_column) if direction == "asc" else desc(sort_column)
        statement = (
            select(Application)
            .join(Opportunity)
            .where(*filters)
            .order_by(ordering.nulls_last(), asc(Application.id))
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        records = tuple(self.session.scalars(statement).all())
        return ApplicationPage(
            records=records,
            total_count=int(count),
            page=page,
            per_page=per_page,
            sort=sort,
            direction=direction,
            query=query,
            state=state,
        )

    @staticmethod
    def _priority(opportunity: Opportunity) -> int:
        score = 50
        if opportunity.rolling:
            score += 15
        if opportunity.deadline:
            days = (opportunity.deadline - date.today()).days
            if days <= 7:
                score += 25
            elif days <= 21:
                score += 15
        division = f"{opportunity.division} {opportunity.programme_group}".casefold()
        if any(term in division for term in ("investment banking", "private credit", "credit")):
            score += 10
        return min(score, 100)

    def _get_or_create(self, opportunity: Opportunity) -> Application:
        application, created = self.repository.get_or_create_for_opportunity(
            opportunity.id,
            state=ApplicationState.DISCOVERED.value,
            priority=self._priority(opportunity),
        )
        if created:
            append_audit(
                self.session,
                AuditInput(
                    "system",
                    "application.created",
                    "application",
                    application.id,
                    {"opportunity_id": opportunity.id},
                ),
            )
        return application

    def _existing_applications(self, application_id: str) -> list[ExistingApplication]:
        rows = self.session.execute(
            select(Application, Opportunity)
            .join(Opportunity, Application.opportunity_id == Opportunity.id)
            .where(Application.id != application_id)
        ).all()
        return [
            ExistingApplication(
                employer=opportunity.employer,
                cycle=opportunity.cycle,
                division=opportunity.division,
                programme_group=opportunity.programme_group or None,
                state=ApplicationState(application.state),
            )
            for application, opportunity in rows
        ]

    def _conflict_rules(self) -> list[ConflictRuleSnapshot]:
        records = list(self.session.scalars(select(ConflictRule)).all())
        snapshots: list[ConflictRuleSnapshot] = []
        for record in records:
            groups_data = json.loads(record.exclusive_groups_json or "[]")
            snapshots.append(
                ConflictRuleSnapshot(
                    employer_pattern=record.employer_pattern,
                    cycle=record.cycle,
                    max_applications=record.max_applications,
                    mutually_exclusive_groups=tuple(frozenset(group) for group in groups_data),
                )
            )
        return snapshots

    def evaluate(self, opportunity_id: str) -> ApplicationDecision:
        opportunity = self.session.get(Opportunity, opportunity_id)
        if opportunity is None:
            raise KeyError(f"Opportunity not found: {opportunity_id}")
        application = self._get_or_create(opportunity)
        profile = ProfileService(self.session, self.crypto).get_snapshot()
        eligibility = evaluate_eligibility(
            profile,
            OpportunitySnapshot(
                opportunity_id=opportunity.id,
                url=opportunity.url,
                deadline=opportunity.deadline,
                min_graduation_year=opportunity.min_graduation_year,
                max_graduation_year=opportunity.max_graduation_year,
                sponsorship_supported=opportunity.sponsorship_supported,
                location=opportunity.location,
                already_applied=False,
            ),
            date.today(),
        )
        conflict = evaluate_conflicts(
            CandidateApplication(
                opportunity.employer,
                opportunity.cycle,
                opportunity.division,
                opportunity.programme_group or None,
            ),
            self._existing_applications(application.id),
            self._conflict_rules(),
        )
        application.eligibility_json = json.dumps(asdict(eligibility), sort_keys=True)
        application.conflict_json = json.dumps(asdict(conflict), sort_keys=True)
        state = ApplicationState(application.state)
        preserve_execution_state = state in _PROTECTED_REEVALUATION_STATES
        if not preserve_execution_state:
            assessed_risk = 3 if eligibility.requires_review or conflict.blocked else 0
            if not eligibility.eligible:
                assessed_risk = 4
            mark_risk_assessed = getattr(application, "mark_risk_assessed", None)
            if callable(mark_risk_assessed):
                mark_risk_assessed(assessed_risk, source="eligibility_conflict")
            else:
                application.risk_level = assessed_risk
        if not preserve_execution_state:
            if state == ApplicationState.DISCOVERED:
                self._transition(application, ApplicationState.ELIGIBILITY_CHECKED)
                state = ApplicationState.ELIGIBILITY_CHECKED
            if not eligibility.eligible or conflict.blocked:
                if state != ApplicationState.BLOCKED:
                    self._transition(application, ApplicationState.BLOCKED)
                application.next_action = "Resolve eligibility or employer conflict"
            else:
                if state == ApplicationState.BLOCKED:
                    self._transition(application, ApplicationState.ELIGIBILITY_CHECKED)
                if eligibility.requires_review:
                    application.next_action = "Review eligibility answers"
                else:
                    application.next_action = "Queue application"
        self.session.flush()
        reasons = eligibility.reason_codes + conflict.reason_codes
        append_audit(
            self.session,
            AuditInput(
                "system",
                "application.evaluated",
                "application",
                application.id,
                {
                    "eligible": eligibility.eligible,
                    "requires_review": eligibility.requires_review,
                    "conflict_blocked": conflict.blocked,
                    "reason_codes": list(reasons),
                    "execution_state_preserved": preserve_execution_state,
                },
            ),
        )
        return ApplicationDecision(
            application=application,
            eligible=eligibility.eligible,
            requires_review=eligibility.requires_review,
            conflict_blocked=conflict.blocked,
            reason_codes=reasons,
        )

    def queue(self, application_id: str) -> Application:
        application = self.repository.get(application_id)
        if application is None:
            raise KeyError(f"Application not found: {application_id}")
        status_reason = user_status_exclusion_reason(application.opportunity.user_status)
        if status_reason is not None:
            raise ApplicationBlockedError(status_reason)
        eligibility = json.loads(application.eligibility_json or "{}")
        conflict = json.loads(application.conflict_json or "{}")
        if eligibility.get("eligible") is False or conflict.get("blocked") is True:
            raise ApplicationBlockedError(
                "Application has unresolved eligibility or conflict blocks"
            )
        state = ApplicationState(application.state)
        if state in {
            ApplicationState.NEEDS_USER,
            ApplicationState.ELIGIBILITY_CHECKED,
            ApplicationState.FAILED_RETRYABLE,
        }:
            self._transition(application, ApplicationState.QUEUED, actor="user")
        elif state != ApplicationState.QUEUED:
            raise ApplicationBlockedError(f"Cannot queue application from {state.value}")
        application.next_action = "Prepare documents"
        self.session.flush()
        return application

    def prepare(self, application_id: str) -> PreparedPackage:
        application = self.repository.get(application_id)
        if application is None:
            raise KeyError(f"Application not found: {application_id}")
        opportunity = application.opportunity
        if user_status_exclusion_reason(opportunity.user_status) is not None:
            return PreparedPackage(
                application,
                False,
                application.selected_cv_id,
                application.selected_cover_letter_id,
                ("user_status_excludes_automation",),
            )
        refusal_reason: str | None = None
        if opportunity.is_archived:
            refusal_reason = "opportunity_archived"
        elif not opportunity.is_open_for_applications:
            refusal_reason = "application_window_not_open"
        elif out_of_scope_programme_reason(
            opportunity.role_title,
            opportunity.programme_group,
        ) is not None:
            refusal_reason = "opportunity_out_of_scope"
        if refusal_reason is not None:
            state = ApplicationState(application.state)
            if state in {
                ApplicationState.ELIGIBILITY_CHECKED,
                ApplicationState.QUEUED,
                ApplicationState.PACKAGE_PREPARED,
                ApplicationState.FILLING,
                ApplicationState.NEEDS_USER,
                ApplicationState.FAILED_RETRYABLE,
                ApplicationState.READY_TO_SUBMIT,
            }:
                self._transition(application, ApplicationState.BLOCKED)
            elif state is not ApplicationState.BLOCKED:
                raise ApplicationBlockedError(
                    f"Cannot refuse out-of-scope preparation from {state.value}"
                )
            application.selected_cv_id = None
            application.selected_cover_letter_id = None
            application.next_action = (
                "Unarchive opportunity before preparation"
                if refusal_reason == "opportunity_archived"
                else "Opportunity is outside the supported programme scope"
            )
            self.session.flush()
            return PreparedPackage(
                application,
                False,
                None,
                None,
                (refusal_reason,),
            )
        framing = resolve_programme_framing(opportunity.programme_group)
        if framing is None:
            state = ApplicationState(application.state)
            if state in {
                ApplicationState.QUEUED,
                ApplicationState.PACKAGE_PREPARED,
                ApplicationState.FILLING,
                ApplicationState.READY_TO_SUBMIT,
            }:
                self._transition(application, ApplicationState.NEEDS_USER)
            elif state != ApplicationState.NEEDS_USER:
                raise ApplicationBlockedError(
                    f"Cannot request programme framing from {state.value}"
                )
            application.selected_cv_id = None
            application.selected_cover_letter_id = None
            application.next_action = "Resolve programme framing"
            self.session.flush()
            return PreparedPackage(
                application,
                False,
                None,
                None,
                ("programme_framing_required",),
            )
        document_service = DocumentService(self.session, self.settings.documents_dir)

        variant_tag = framing.cv_variant_tag
        preference_tags = cv_preference_tags(opportunity)
        cv = select_cv_for_opportunity(document_service, opportunity)
        cover_letter = (
            document_service.select_approved(
                "cover_letter", (*preference_tags, variant_tag)
            )
            if opportunity.cover_letter_required
            else None
        )
        reasons: list[str] = []
        if opportunity.cv_required and cv is None:
            reasons.append("required_cv_missing")
        if opportunity.cover_letter_required and cover_letter is None:
            reasons.append("required_cover_letter_missing")
        if reasons:
            if "required_cv_missing" in reasons:
                application.selected_cv_id = None
            if "required_cover_letter_missing" in reasons:
                application.selected_cover_letter_id = None
            state = ApplicationState(application.state)
            if state == ApplicationState.QUEUED:
                self._transition(application, ApplicationState.NEEDS_USER)
            application.next_action = (
                "Upload and approve a CV"
                if "required_cv_missing" in reasons
                else "Upload and approve a cover letter"
            )
            self.session.flush()
            return PreparedPackage(application, False, None, None, tuple(reasons))

        application.selected_cv_id = cv.id if cv else None
        application.selected_cover_letter_id = cover_letter.id if cover_letter else None
        state = ApplicationState(application.state)
        if state in {ApplicationState.QUEUED, ApplicationState.NEEDS_USER}:
            self._transition(application, ApplicationState.PACKAGE_PREPARED)
        elif state != ApplicationState.PACKAGE_PREPARED:
            raise ApplicationBlockedError(f"Cannot prepare application from {state.value}")
        application.next_action = "Run application"
        self.session.flush()
        append_audit(
            self.session,
            AuditInput(
                "system",
                "application.package_prepared",
                "application",
                application.id,
                {
                    "cv_id": application.selected_cv_id,
                    "cover_letter_id": application.selected_cover_letter_id,
                },
            ),
        )
        return PreparedPackage(
            application,
            True,
            application.selected_cv_id,
            application.selected_cover_letter_id,
            (),
        )
