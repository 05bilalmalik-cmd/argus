from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import (
    AnswerEntry,
    Application,
    AuditEvent,
    AutomationRun,
    CandidateProfile,
    ConflictRule,
    Document,
    EmailMessage,
    Opportunity,
    QuestionRecord,
)

ModelT = TypeVar("ModelT")


class Repository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: Session):
        self.session = session

    def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        self.session.flush()
        return entity

    def get(self, entity_id):
        return self.session.get(self.model, entity_id)

    def list(self) -> list[ModelT]:
        return list(self.session.scalars(select(self.model)).all())


class CandidateRepository(Repository[CandidateProfile]):
    model = CandidateProfile
    _FIELDS = (
        "first_name",
        "last_name",
        "preferred_name",
        "email",
        "phone",
        "address_line1",
        "city",
        "postcode",
        "country",
        "linkedin_url",
        "university",
        "degree",
        "graduation_year",
        "current_study_year",
        "preferred_locations_json",
        "work_authorisation_ciphertext",
        "sponsorship_required_ciphertext",
        "work_authorisation_approved",
    )

    def get(self, entity_id: int = 1) -> CandidateProfile | None:
        return super().get(entity_id)

    def get_or_create(self) -> CandidateProfile:
        """Return the singleton profile, atomically creating id=1 if needed.

        The initial read is only an optimization.  The SQLite upsert is the
        ownership boundary: concurrent sessions that both observe an empty
        table converge on the same primary-key row, while unrelated database
        errors still propagate normally.
        """

        existing = self.get(1)
        if existing is None:
            self.session.execute(
                sqlite_insert(CandidateProfile)
                .values(id=1)
                .on_conflict_do_nothing(index_elements=[CandidateProfile.id])
            )
            existing = self.get(1)
        if existing is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("Candidate profile singleton could not be created")
        return existing

    def upsert(self, candidate: CandidateProfile) -> CandidateProfile:
        existing = self.get_or_create()
        for field in self._FIELDS:
            value = getattr(candidate, field)
            if value is not None:
                setattr(existing, field, value)
        self.session.flush()
        return existing


class AnswerRepository(Repository[AnswerEntry]):
    model = AnswerEntry

    def by_key(self, canonical_key: str) -> AnswerEntry | None:
        return self.session.scalar(
            select(AnswerEntry).where(AnswerEntry.canonical_key == canonical_key)
        )


class DocumentRepository(Repository[Document]):
    model = Document

    def approved_by_kind(self, kind: str) -> list[Document]:
        statement: Select[tuple[Document]] = (
            select(Document)
            .where(Document.kind == kind, Document.approved.is_(True))
            .order_by(Document.created_at.desc())
        )
        return list(self.session.scalars(statement).all())


class OpportunityRepository(Repository[Opportunity]):
    model = Opportunity

    def by_url(self, url: str) -> Opportunity | None:
        return self.session.scalar(select(Opportunity).where(Opportunity.url == url))

    def by_source_fingerprint(self, fingerprint: str) -> Opportunity | None:
        return self.session.scalars(
            select(Opportunity)
            .where(Opportunity.source_fingerprint == fingerprint)
            .order_by(Opportunity.created_at, Opportunity.id)
        ).first()

    def all_by_source_fingerprint(self, fingerprint: str) -> list[Opportunity]:
        return list(
            self.session.scalars(
                select(Opportunity).where(
                    Opportunity.source_fingerprint == fingerprint
                )
            ).all()
        )

    def source_identity_candidates(
        self,
        *,
        fingerprint: str,
        employer: str,
        role_title: str,
        cycle: str,
    ) -> list[Opportunity]:
        """Return hash candidates plus legacy rows with matching text fields."""

        normalised_employer = employer.strip().casefold()
        normalised_role = role_title.strip().casefold()
        normalised_cycle = cycle.strip().casefold()
        statement = select(Opportunity).where(
            or_(
                Opportunity.source_fingerprint == fingerprint,
                and_(
                    func.lower(func.trim(Opportunity.employer)) == normalised_employer,
                    func.lower(func.trim(Opportunity.role_title)) == normalised_role,
                    func.lower(func.trim(Opportunity.cycle)) == normalised_cycle,
                ),
            )
        )
        return list(self.session.scalars(statement).all())


class ApplicationRepository(Repository[Application]):
    model = Application

    def by_opportunity(self, opportunity_id: str) -> Application | None:
        return self.session.scalar(
            select(Application).where(Application.opportunity_id == opportunity_id)
        )

    def get_or_create_for_opportunity(
        self,
        opportunity_id: str,
        *,
        state: str,
        priority: int,
    ) -> tuple[Application, bool]:
        """Atomically get the one application bound to an opportunity.

        The boolean reports whether this call inserted the row, allowing the
        service to retain its single ``application.created`` audit event.
        The conflict target is exactly the existing opportunity uniqueness
        constraint; unrelated integrity errors are not swallowed.
        """

        existing = self.by_opportunity(opportunity_id)
        if existing is not None:
            return existing, False
        result = self.session.execute(
            sqlite_insert(Application)
            .values(
                opportunity_id=opportunity_id,
                state=state,
                priority=priority,
            )
            .on_conflict_do_nothing(index_elements=[Application.opportunity_id])
        )
        existing = self.by_opportunity(opportunity_id)
        if existing is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("Application could not be bound to opportunity")
        return existing, result.rowcount == 1

    def unassessed(self) -> list[Application]:
        """Return applications with no persisted risk assessment metadata."""

        statement: Select[tuple[Application]] = (
            select(Application)
            .where(Application.risk_assessed_at.is_(None))
            .order_by(Application.priority, Application.created_at)
        )
        return list(self.session.scalars(statement).all())

    def mark_risk_assessed(
        self,
        application_id: str,
        level: int,
        *,
        source: str,
    ) -> Application:
        application = self.get(application_id)
        if application is None:
            raise KeyError(application_id)
        application.mark_risk_assessed(level, source=source)
        self.session.flush()
        return application


class ConflictRuleRepository(Repository[ConflictRule]):
    model = ConflictRule


class RunRepository(Repository[AutomationRun]):
    model = AutomationRun


class QuestionRepository(Repository[QuestionRecord]):
    model = QuestionRecord


class EmailRepository(Repository[EmailMessage]):
    model = EmailMessage


class AuditRepository(Repository[AuditEvent]):
    model = AuditEvent
