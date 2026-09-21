from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    inspect as sa_inspect,
    text as sql_text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    preferred_name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(320), default="")
    phone: Mapped[str] = mapped_column(String(80), default="")
    address_line1: Mapped[str] = mapped_column(String(240), default="")
    city: Mapped[str] = mapped_column(String(120), default="")
    postcode: Mapped[str] = mapped_column(String(32), default="")
    country: Mapped[str] = mapped_column(String(120), default="United Kingdom")
    linkedin_url: Mapped[str] = mapped_column(String(500), default="")
    university: Mapped[str] = mapped_column(String(240), default="")
    degree: Mapped[str] = mapped_column(String(240), default="")
    graduation_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    current_study_year: Mapped[str] = mapped_column(String(80), default="")
    preferred_locations_json: Mapped[str] = mapped_column(Text, default="[]")
    admissible_graduation_years_json: Mapped[str] = mapped_column(Text, default="[]")
    work_authorisation_ciphertext: Mapped[str] = mapped_column(Text, default="")
    sponsorship_required_ciphertext: Mapped[str] = mapped_column(Text, default="")
    work_authorisation_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class AnswerEntry(Base):
    __tablename__ = "answer_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    canonical_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    answer_ciphertext: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(80), default="general")
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence: Mapped[str] = mapped_column(Text, default="")
    max_characters: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(80), index=True)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    employer: Mapped[str] = mapped_column(String(240), index=True)
    role_title: Mapped[str] = mapped_column(String(320))
    division: Mapped[str] = mapped_column(String(240), default="")
    programme_group: Mapped[str] = mapped_column(String(120), default="")
    location: Mapped[str] = mapped_column(String(240), default="")
    cycle: Mapped[str] = mapped_column(String(40), index=True)
    # Legacy compatibility: ``url`` is the discovery/source URL.  It is not
    # proof of an application form and is never a global identity key.
    url: Mapped[str] = mapped_column(Text)
    # Lookup hint only. Exact normalized identity is checked in the service;
    # digest collisions must remain representable.
    source_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    # Sole authoritative identity for a live Trackr API programme.  Other
    # sources remain null and retain their existing fingerprint behavior.
    trackr_programme_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    __table_args__ = (
        Index(
            "uq_opportunities_trackr_programme_id",
            "trackr_programme_id",
            unique=True,
            sqlite_where=sql_text("trackr_programme_id IS NOT NULL"),
        ),
    )
    application_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Provenance of the application_url: "RESOLVER_SUPPLIED" when set by the
    # automated target resolver, "OPERATOR_SUPPLIED" when the user corrected it.
    # An empty/null value means no URL was ever stored.
    application_url_provenance: Mapped[str] = mapped_column(String(40), default="")
    target_status: Mapped[str] = mapped_column(
        String(40), default="UNRESOLVED", index=True
    )
    resolved_ats_type: Mapped[str] = mapped_column(String(80), default="")
    resolution_evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution_attempted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[str] = mapped_column(String(120), default="manual")
    ats_type: Mapped[str] = mapped_column(String(80), default="unknown")
    opening_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    deadline: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    rolling: Mapped[bool] = mapped_column(Boolean, default=False)
    application_window_status: Mapped[str] = mapped_column(
        String(40), default="UNKNOWN", index=True
    )
    min_graduation_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_graduation_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sponsorship_supported: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    cv_required: Mapped[bool] = mapped_column(Boolean, default=True)
    cover_letter_required: Mapped[bool] = mapped_column(Boolean, default=False)
    written_answers_required: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    user_status: Mapped[str] = mapped_column(
        String(40),
        default="NOT_APPLIED",
        server_default=sql_text("'NOT_APPLIED'"),
        index=True,
    )
    user_status_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=sql_text("CURRENT_TIMESTAMP"),
    )
    user_status_actor: Mapped[str] = mapped_column(
        String(20),
        default="default",
        server_default=sql_text("'default'"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    application: Mapped[Optional["Application"]] = relationship(
        back_populates="opportunity", uselist=False
    )
    archive_record: Mapped[Optional["OpportunityArchive"]] = relationship(
        back_populates="opportunity", uselist=False
    )

    def __init__(self, **kwargs: object) -> None:
        if not kwargs.get("application_window_status"):
            from app.scouting.application_window import derive_application_window

            source_url = str(kwargs.get("url") or "")
            candidate_url = str(kwargs.get("application_url") or source_url or "")
            kwargs["application_window_status"] = derive_application_window(
                opening_date=kwargs.get("opening_date") if isinstance(kwargs.get("opening_date"), date) else None,
                closing_date=kwargs.get("deadline") if isinstance(kwargs.get("deadline"), date) else None,
                application_url=candidate_url or None,
                link_signal_known=bool(source_url),
            ).value
        if not kwargs.get("source_fingerprint"):
            from app.domain.targets import source_fingerprint

            kwargs["source_fingerprint"] = source_fingerprint(
                employer=str(kwargs.get("employer") or ""),
                role_title=str(kwargs.get("role_title") or ""),
                cycle=str(kwargs.get("cycle") or ""),
                source_url=str(kwargs.get("url") or ""),
                location=str(kwargs.get("location") or ""),
                division=str(kwargs.get("division") or ""),
            )
        super().__init__(**kwargs)

    @property
    def navigation_url(self) -> str:
        from app.domain.targets import validate_navigation_url

        return validate_navigation_url(self.url)

    @property
    def automation_url(self) -> str | None:
        from app.domain.states import (
            UserApplicationStatus,
            user_status_is_automation_eligible,
        )
        from app.domain.targets import TargetKind, validate_navigation_url

        status = self.user_status
        state = sa_inspect(self)
        if status is None and (state.transient or state.pending):
            # SQLAlchemy column defaults are installed at flush time.  Preserve
            # the declared NOT_APPLIED default for new in-memory records while
            # keeping any persisted/detached null or invalid value fail-closed.
            status = UserApplicationStatus.NOT_APPLIED.value
        if not user_status_is_automation_eligible(status):
            return None
        if not self.is_open_for_applications:
            return None
        try:
            kind = TargetKind(self.target_status)
        except ValueError:
            return None
        if not kind.automation_eligible or not self.application_url:
            return None
        try:
            return validate_navigation_url(self.application_url)
        except ValueError:
            return None

    @property
    def archived_at(self) -> datetime | None:
        return self.archive_record.archived_at if self.archive_record is not None else None

    @property
    def archived_reason(self) -> str | None:
        return (
            self.archive_record.archived_reason
            if self.archive_record is not None
            else None
        )

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_open_for_applications(self) -> bool:
        from app.scouting.application_window import ApplicationWindowStatus

        return self.application_window_status == ApplicationWindowStatus.OPEN.value


class OpportunityArchive(Base):
    """Reversible opportunity exclusion metadata; the opportunity stays intact."""

    __tablename__ = "opportunity_archives"
    __table_args__ = (
        CheckConstraint(
            "(archived_at IS NULL AND archived_reason IS NULL) OR "
            "(archived_at IS NOT NULL AND archived_reason IS NOT NULL)",
            name="ck_opportunity_archive_pair",
        ),
    )

    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    archived_reason: Mapped[Optional[str]] = mapped_column(
        String(240), nullable=True
    )

    opportunity: Mapped[Opportunity] = relationship(back_populates="archive_record")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(60), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    # ``risk_level`` remains a non-null legacy field so databases created by
    # ARGUS 0.1.x continue to load.  A zero in that column is not evidence
    # that assessment happened; callers must use ``risk_is_assessed`` or
    # ``effective_risk_level`` for truthfully nullable risk state.
    risk_level: Mapped[int] = mapped_column(Integer, default=0)
    risk_assessed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    risk_assessment_source: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True
    )
    selected_cv_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("documents.id"), nullable=True
    )
    selected_cover_letter_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("documents.id"), nullable=True
    )
    eligibility_json: Mapped[str] = mapped_column(Text, default="{}")
    conflict_json: Mapped[str] = mapped_column(Text, default="{}")
    next_action: Mapped[str] = mapped_column(String(240), default="")
    next_action_deadline: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submission_reference: Mapped[str] = mapped_column(String(240), default="")
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    opportunity: Mapped[Opportunity] = relationship(back_populates="application")
    selected_cv: Mapped[Optional[Document]] = relationship(foreign_keys=[selected_cv_id])
    selected_cover_letter: Mapped[Optional[Document]] = relationship(
        foreign_keys=[selected_cover_letter_id]
    )
    runs: Mapped[list["AutomationRun"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )

    @property
    def risk_is_assessed(self) -> bool:
        """Whether a persisted risk assessment exists for this application."""

        return self.risk_assessed_at is not None

    @property
    def effective_risk_level(self) -> int | None:
        """Return risk only when it has actually been assessed.

        ``risk_level`` intentionally remains readable for old records, but a
        legacy default of zero must not be mistaken for a completed review.
        """

        return self.risk_level if self.risk_is_assessed else None

    def mark_risk_assessed(
        self,
        level: int,
        *,
        source: str,
        assessed_at: datetime | None = None,
    ) -> None:
        if not 0 <= level <= 4:
            raise ValueError("Risk level must be between 0 and 4")
        if not source.strip():
            raise ValueError("Risk assessment source is required")
        self.risk_level = level
        self.risk_assessed_at = assessed_at or _utcnow()
        self.risk_assessment_source = source[:120]


class ConflictRule(Base):
    __tablename__ = "conflict_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    employer_pattern: Mapped[str] = mapped_column(String(240), index=True)
    cycle: Mapped[str] = mapped_column(String(40), index=True)
    max_applications: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    exclusive_groups_json: Mapped[str] = mapped_column(Text, default="[]")
    notes: Mapped[str] = mapped_column(Text, default="")


class AutomationRun(Base):
    __tablename__ = "automation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(60), default="QUEUED", index=True)
    adapter: Mapped[str] = mapped_column(String(80), default="unknown")
    risk_level: Mapped[int] = mapped_column(Integer, default=0)
    risk_assessed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    risk_assessment_source: Mapped[Optional[str]] = mapped_column(
        String(120), nullable=True
    )
    trace_path: Mapped[str] = mapped_column(Text, default="")
    screenshot_path: Mapped[str] = mapped_column(Text, default="")
    receipt_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    application: Mapped[Application] = relationship(back_populates="runs")
    questions: Mapped[list["QuestionRecord"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    @property
    def risk_is_assessed(self) -> bool:
        return self.risk_assessed_at is not None

    @property
    def effective_risk_level(self) -> int | None:
        return self.risk_level if self.risk_is_assessed else None

    def mark_risk_assessed(
        self,
        level: int,
        *,
        source: str,
        assessed_at: datetime | None = None,
    ) -> None:
        if not 0 <= level <= 4:
            raise ValueError("Risk level must be between 0 and 4")
        if not source.strip():
            raise ValueError("Risk assessment source is required")
        self.risk_level = level
        self.risk_assessed_at = assessed_at or _utcnow()
        self.risk_assessment_source = source[:120]


class QuestionRecord(Base):
    __tablename__ = "question_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("automation_runs.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(Text)
    field_type: Mapped[str] = mapped_column(String(80))
    canonical_key: Mapped[str] = mapped_column(String(160), default="unknown")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    sensitivity: Mapped[str] = mapped_column(String(80), default="standard")
    answer_source: Mapped[str] = mapped_column(String(120), default="")
    answer_preview: Mapped[str] = mapped_column(String(240), default="")
    mapping_status: Mapped[str] = mapped_column(String(80), default="unmapped")
    reason: Mapped[str] = mapped_column(Text, default="")

    run: Mapped[AutomationRun] = relationship(back_populates="questions")


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    message_id: Mapped[Optional[str]] = mapped_column(String(500), unique=True, nullable=True)
    sender: Mapped[str] = mapped_column(String(500), default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    body_text: Mapped[str] = mapped_column(Text, default="")
    classification: Mapped[str] = mapped_column(String(80), default="other", index=True)
    application_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("applications.id"), nullable=True, index=True
    )
    action_deadline: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SubmissionIntent(Base):
    """Durable, one-per-application record of an imminent final submission.

    Written and committed BEFORE the final click.  If anything goes wrong
    after the click (receipt loss, local persistence failure), this row is
    what tells us a side effect may have reached the employer — which is why
    SUBMISSION_UNKNOWN must never be automatically retried: the intent
    already proves one terminal attempt was made.
    """

    __tablename__ = "submission_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), unique=True, index=True
    )
    attempt_id: Mapped[str] = mapped_column(String(36))
    nonce: Mapped[str] = mapped_column(String(64), unique=True)
    # SHA-256 over the canonical manifest shown to/verified for the user:
    # employer, role, requisition, destination, form identity.
    manifest_fingerprint: Mapped[str] = mapped_column(String(64))
    manifest_json: Mapped[str] = mapped_column(Text)
    destination_url: Mapped[str] = mapped_column(Text)
    submission_control_selector: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __init__(self, **kwargs: object) -> None:
        manifest = kwargs.get("manifest_json")
        if kwargs.get("manifest_fingerprint") is None and isinstance(manifest, str):
            kwargs["manifest_fingerprint"] = hashlib.sha256(
                manifest.encode("utf-8")
            ).hexdigest()
        super().__init__(**kwargs)

    PENDING = "PENDING"                # persisted, not yet clicked
    CLICKED = "CLICKED"                # click issued; awaiting evidence
    CONFIRMED = "CONFIRMED"            # receipt verified + persisted
    UNKNOWN = "UNKNOWN"                # click may have landed; evidence lost
    FAILED_LOCAL = "FAILED_LOCAL"      # click never issued (guard failed)


class SubmissionAuthority(Base):
    """One durable, single-use authorization for one exact application."""

    __tablename__ = "submission_authorities"
    # A Navigator session may mint at most one authority.  This is deliberately
    # unconditional (rather than a partial ``consumed_at IS NULL`` index): a
    # consumed/expired session must never silently mint a second click token.
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "session_id",
            name="uq_submission_authority_application_session",
        ),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(120), index=True)
    manifest_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    destination_origin: Mapped[str] = mapped_column(String(500), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class SubmissionReviewBinding(Base):
    """Durable copy of the exact final manifest displayed to the user.

    The browser owner remains the authority for discovering the live form,
    but confirmation happens in a later HTTP request.  Persisting this stable
    projection closes that display/confirm gap: a same-origin form, selector,
    control, document, or answer change cannot silently replace what the user
    reviewed before the single-use authority is issued.
    """

    __tablename__ = "submission_review_bindings"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "session_id",
            name="uq_submission_review_application_session",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(120), index=True)
    manifest_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    destination_origin: Mapped[str] = mapped_column(String(500), index=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Epoch 0 contains the legacy chain.  A repaired/future chain may start a
    # later epoch without rewriting the historical rows that document the
    # production fork.
    epoch: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(120))
    event_type: Mapped[str] = mapped_column(String(160), index=True)
    entity_type: Mapped[str] = mapped_column(String(120), index=True)
    entity_id: Mapped[str] = mapped_column(String(120), index=True)
    details_json: Mapped[str] = mapped_column(Text)
    previous_hash: Mapped[str] = mapped_column(String(64), default="")
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)


class AuditChainState(Base):
    """Durable serialized head used by the SQLite audit drain.

    Exactly one row is used.  The row is updated in the same transaction as
    each emitted event, so another process cannot observe a new head without
    its corresponding event (or vice versa).
    """

    __tablename__ = "audit_chain_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    head_event_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    head_hash: Mapped[str] = mapped_column(String(64), default="")


class AuditOutbox(Base):
    """Transactional audit intent waiting for a short serialized drain.

    An intent is inserted in the caller's transaction alongside business
    mutations.  It remains durable until the drain commits the corresponding
    :class:`AuditEvent`; a crash therefore leaves evidence to recover instead
    of silently dropping the audit record.
    """

    __tablename__ = "audit_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(120))
    event_type: Mapped[str] = mapped_column(String(160), index=True)
    entity_type: Mapped[str] = mapped_column(String(120), index=True)
    entity_id: Mapped[str] = mapped_column(String(120), index=True)
    details_json: Mapped[str] = mapped_column(Text)
    # Fingerprint of the immutable intent returned by ``append_audit``.  If a
    # compatibility object is tampered with before commit, the drain preserves
    # the original hash so verification reports the mutation instead of
    # silently re-signing it.
    intent_fingerprint: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    emitted_event_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    emitted_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    PENDING = "PENDING"
    EMITTED = "EMITTED"
    FAILED = "FAILED"


# Semantic name used by callers that want to distinguish the durable intent
# from the eventual hash-chained event.  It is deliberately the same mapped
# class so no second table or migration is introduced.
AuditIntent = AuditOutbox


class LabSubmission(Base):
    __tablename__ = "lab_submissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scenario: Mapped[str] = mapped_column(String(80), index=True)
    reference: Mapped[str] = mapped_column(String(80), unique=True)
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


Index("ix_applications_state_priority", Application.state, Application.priority)
Index("ix_opportunities_employer_cycle", Opportunity.employer, Opportunity.cycle)
