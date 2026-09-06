from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.domain.states import ApplicationState, validate_transition
from app.models import Application, Document, Opportunity
from app.scouting.programmes import resolve_programme_framing
from app.security.audit import AuditInput, append_audit
from app.services.applications import select_cv_for_opportunity
from app.services.documents import DocumentService


CV_VARIANT_TAGS = ("yii-cv", "summer-cv", "apply-cv")
MATRIX_CONTENT_COLUMNS = ("grad-2029", "grad-2028", "unparseable")


@dataclass(frozen=True, slots=True)
class CvDocumentObservation:
    document_id: str
    name: str
    content_tag: str | None
    old_tags: frozenset[str]
    new_tags: frozenset[str]

    @property
    def changed(self) -> bool:
        return self.old_tags != self.new_tags


@dataclass(frozen=True, slots=True)
class CvTagRepairPlan:
    observations: tuple[CvDocumentObservation, ...]
    before: Counter[tuple[str, str]]
    after: Counter[tuple[str, str]]

    @property
    def changes(self) -> tuple[CvDocumentObservation, ...]:
        return tuple(item for item in self.observations if item.changed)

    @property
    def unparseable(self) -> int:
        return sum(item.content_tag is None for item in self.observations)


@dataclass(frozen=True, slots=True)
class CvReselectionReport:
    affected: int
    corrected: int
    missing: int
    state_fail_closed: int


def _tags_for_content(old_tags: set[str], content_tag: str | None) -> set[str]:
    new_tags = set(old_tags)
    new_tags.difference_update(DocumentService.GRADUATION_TAGS)
    if content_tag is None:
        return new_tags
    new_tags.add(content_tag)
    if content_tag == "grad-2028":
        new_tags.discard("yii-cv")
        new_tags.add("summer-cv")
    elif content_tag == "grad-2029":
        new_tags.discard("summer-cv")
        new_tags.add("yii-cv")
    return new_tags


def _matrix(
    observations: tuple[CvDocumentObservation, ...],
    *,
    use_new_tags: bool,
) -> Counter[tuple[str, str]]:
    matrix: Counter[tuple[str, str]] = Counter()
    for observation in observations:
        tags = observation.new_tags if use_new_tags else observation.old_tags
        variant_rows = [tag for tag in CV_VARIANT_TAGS if tag in tags]
        if not variant_rows:
            variant_rows = ["(none)"]
        content_column = observation.content_tag or "unparseable"
        for variant in variant_rows:
            matrix[(variant, content_column)] += 1
    return matrix


def plan_cv_tag_repairs(session: Session) -> CvTagRepairPlan:
    documents = list(
        session.scalars(
            select(Document).where(Document.kind == "cv").order_by(Document.id)
        ).all()
    )
    observations = tuple(
        CvDocumentObservation(
            document_id=document.id,
            name=document.name,
            content_tag=(content_tag := DocumentService.derived_tag_for_document(document)),
            old_tags=frozenset(old_tags := DocumentService.tags(document)),
            new_tags=frozenset(_tags_for_content(old_tags, content_tag)),
        )
        for document in documents
    )
    return CvTagRepairPlan(
        observations=observations,
        before=_matrix(observations, use_new_tags=False),
        after=_matrix(observations, use_new_tags=True),
    )


def planned_invariant_violations(plan: CvTagRepairPlan) -> int:
    violations = 0
    for item in plan.observations:
        tags = item.new_tags
        has_2028 = "grad-2028" in tags
        has_2029 = "grad-2029" in tags
        if (
            int(has_2028) + int(has_2029) != 1
            or ("summer-cv" in tags) != has_2028
            or ("yii-cv" in tags) != has_2029
        ):
            violations += 1
    return violations


_INVARIANT_QUERY = text(
    """
    WITH tag_flags AS (
        SELECT
            d.id,
            MAX(CASE WHEN j.value = 'grad-2028' THEN 1 ELSE 0 END) AS grad_2028,
            MAX(CASE WHEN j.value = 'grad-2029' THEN 1 ELSE 0 END) AS grad_2029,
            MAX(CASE WHEN j.value = 'summer-cv' THEN 1 ELSE 0 END) AS summer_cv,
            MAX(CASE WHEN j.value = 'yii-cv' THEN 1 ELSE 0 END) AS yii_cv
        FROM documents AS d
        LEFT JOIN json_each(
            CASE WHEN json_valid(d.tags_json) THEN d.tags_json ELSE '[]' END
        ) AS j ON 1 = 1
        WHERE d.kind = 'cv'
        GROUP BY d.id
    )
    SELECT COUNT(*)
    FROM tag_flags
    WHERE grad_2028 + grad_2029 != 1
       OR summer_cv != grad_2028
       OR yii_cv != grad_2029
    """
)


def single_query_invariant_violations(session: Session) -> int:
    return int(session.execute(_INVARIANT_QUERY).scalar_one())


def apply_cv_tag_repairs(session: Session, plan: CvTagRepairPlan) -> int:
    current_documents: dict[str, Document] = {}
    for item in plan.observations:
        document = session.get(Document, item.document_id)
        if document is None:
            raise RuntimeError(
                f"document disappeared during CV tag repair: {item.document_id}"
            )
        if (
            frozenset(DocumentService.tags(document)) != item.old_tags
            or DocumentService.derived_tag_for_document(document) != item.content_tag
        ):
            raise RuntimeError(
                f"document changed after repair planning: {item.document_id}"
            )
        current_documents[item.document_id] = document

    changed = 0
    for item in plan.changes:
        document = current_documents[item.document_id]
        document.tags_json = json.dumps(sorted(item.new_tags))
        append_audit(
            session,
            AuditInput(
                "maintenance",
                "document.cv_tags_repaired",
                "document",
                document.id,
                {
                    "content_tag": item.content_tag,
                    "old_tags": sorted(item.old_tags),
                    "new_tags": sorted(item.new_tags),
                },
            ),
        )
        changed += 1
    session.flush()
    return changed


def contradictory_selected_application_ids(session: Session) -> tuple[str, ...]:
    rows = session.execute(
        select(Application, Opportunity, Document)
        .join(Opportunity, Application.opportunity_id == Opportunity.id)
        .join(Document, Application.selected_cv_id == Document.id)
        .order_by(Application.id)
    ).all()
    mismatches: list[str] = []
    for application, opportunity, document in rows:
        framing = resolve_programme_framing(opportunity.programme_group)
        if framing is None:
            continue
        expected = DocumentService.VARIANT_GRADUATION_TAG[framing.cv_variant_tag]
        actual = DocumentService.derived_tag_for_document(document)
        if actual != expected:
            mismatches.append(application.id)
    return tuple(mismatches)


_FAIL_CLOSED_STATES = frozenset(
    {
        ApplicationState.QUEUED,
        ApplicationState.PACKAGE_PREPARED,
        ApplicationState.FILLING,
        ApplicationState.READY_TO_SUBMIT,
    }
)


def reselect_applications(
    session: Session,
    documents_dir: Path,
    application_ids: tuple[str, ...],
) -> CvReselectionReport:
    service = DocumentService(session, documents_dir)
    corrected = 0
    missing = 0
    state_fail_closed = 0
    for application_id in application_ids:
        application = session.get(Application, application_id)
        if application is None:
            raise RuntimeError(
                f"application disappeared during CV reselection: {application_id}"
            )
        opportunity = session.get(Opportunity, application.opportunity_id)
        if opportunity is None:
            raise RuntimeError(
                f"opportunity disappeared during CV reselection: {application.opportunity_id}"
            )
        old_cv_id = application.selected_cv_id
        selected = select_cv_for_opportunity(service, opportunity)
        if selected is None:
            missing += 1
            application.selected_cv_id = None
            append_audit(
                session,
                AuditInput(
                    "maintenance",
                    "application.cv_reselection_missing",
                    "application",
                    application.id,
                    {"old_cv_id": old_cv_id, "new_cv_id": None},
                ),
            )
            current_state = ApplicationState(application.state)
            if current_state in _FAIL_CLOSED_STATES:
                validate_transition(current_state, ApplicationState.NEEDS_USER)
                application.state = ApplicationState.NEEDS_USER.value
                application.next_action = "Upload and approve a programme-compatible CV"
                append_audit(
                    session,
                    AuditInput(
                        "maintenance",
                        "application.state_changed",
                        "application",
                        application.id,
                        {
                            "from": current_state.value,
                            "to": ApplicationState.NEEDS_USER.value,
                            "reason": "required_cv_missing_after_integrity_repair",
                        },
                    ),
                )
                state_fail_closed += 1
            continue
        if selected.id == old_cv_id:
            continue
        application.selected_cv_id = selected.id
        corrected += 1
        append_audit(
            session,
            AuditInput(
                "maintenance",
                "application.cv_reselected",
                "application",
                application.id,
                {"old_cv_id": old_cv_id, "new_cv_id": selected.id},
            ),
        )
    session.flush()
    return CvReselectionReport(
        affected=len(application_ids),
        corrected=corrected,
        missing=missing,
        state_fail_closed=state_fail_closed,
    )


def create_verified_cv_backup(database_path: Path, backup_directory: Path) -> Path:
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = backup_directory / (
        f"{database_path.name}.pre-cv-tag-repair-{uuid4().hex}.bak"
    )
    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(descriptor)
        with closing(
            sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
        ) as source:
            with closing(sqlite3.connect(backup_path)) as destination:
                source.backup(destination)
                destination.commit()
                integrity = destination.execute("PRAGMA integrity_check").fetchall()
                if not integrity or any(
                    str(row[0]).casefold() != "ok" for row in integrity
                ):
                    raise RuntimeError(
                        f"CV repair backup integrity check failed: {integrity!r}"
                    )
                foreign_keys = destination.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if foreign_keys:
                    raise RuntimeError(
                        f"CV repair backup foreign key check failed: {foreign_keys!r}"
                    )
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path
