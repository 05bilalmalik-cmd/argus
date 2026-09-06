from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.db import Database
from app.models import Application, Document
from app.security.audit import AuditInput, append_audit
from app.services.applications import CvReselectionChange, reselect_existing_cvs
from app.services.documents import DocumentService
from scripts.phase23_cv_corrections import (
    NON_QUANT_MONOLITH_LINES,
    OLD_MONOLITH_PREFIX,
    QUANT_TAGS,
    QUANT_MONOLITH_LINES,
    UnsupportedSource,
    _EmployerMatcher,
    _database_employer_names,
    _decode_tags,
    _read_package,
    _replace_monolith,
    _replace_pwc,
    _serialize_package,
    extract_docx,
    transform_docx,
)


CONTRADICTORY_NAMES = frozenset(
    {
        "Demo_Candidate_Apollo_PE_-2028.docx",
        "Demo_Candidate_HRT_Quant_-_2028.docx",
    }
)


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    id: str
    name: str
    kind: str
    path: str
    sha256: str
    approved: bool
    tags_json: str
    created_at: str


@dataclass(frozen=True, slots=True)
class SupersessionPlan:
    source_id: str
    source_name: str
    source_sha256: str
    derivative_id: str
    derivative_name: str
    derivative_sha256: str


@dataclass(frozen=True, slots=True)
class SkippedSource:
    source_id: str
    source_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class CorrectionPlan:
    source: DocumentSnapshot
    output_name: str
    output_sha256: str
    content: bytes
    is_quant: bool
    monolith_treatment_applied: bool
    staged_path: str | None = None

    @property
    def source_id(self) -> str:
        return self.source.id


@dataclass(frozen=True, slots=True)
class RetirementPlan:
    db_path: Path
    documents_dir: Path
    employer_names: tuple[str, ...]
    supersessions: tuple[SupersessionPlan, ...]
    skipped: tuple[SkippedSource, ...]
    corrections: tuple[CorrectionPlan, ...]
    affected_application_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApplyReport:
    superseded: int
    corrections_registered: int
    applications_changed: int
    required_cv_missing: int
    application_changes: tuple[CvReselectionChange, ...]
    documents_deleted: int
    rows_deleted: int
    blocked_states_cleared: int
    submissions: int


@dataclass(frozen=True, slots=True)
class ContradictoryCvResult:
    source_content: bytes
    content: bytes
    source_employers: frozenset[str]
    derived_employers: frozenset[str]
    monolith_treatment_applied: bool


def derive_contradictory_cv(
    content: bytes,
    *,
    is_quant: bool,
    employer_names: tuple[str, ...],
) -> ContradictoryCvResult:
    """Apply only the authorised 2028 degree, PwC, and conditional P23 edits."""

    source = extract_docx(content)
    degree_matches = re.findall(
        r"\b(Bachelor of Science: (?:Quant )?Finance) with Year in Industry\b",
        source.text,
    )
    if len(degree_matches) != 1:
        raise UnsupportedSource("contradictory_degree_occurrence")
    if re.findall(r"\b(?:2028|2029)\b", source.text) != ["2028"]:
        raise UnsupportedSource("contradictory_graduation_year")

    employer_matcher = _EmployerMatcher.compile(employer_names)
    source_employers = employer_matcher.named(source.text)
    package = _read_package(content)
    source_degree = f"{degree_matches[0]} with Year in Industry"
    DocumentService._replace_visible_text_once(
        package.root,
        source_degree,
        degree_matches[0],
    )
    _replace_pwc(package.root)
    apply_monolith = OLD_MONOLITH_PREFIX in source.text
    if apply_monolith:
        _replace_monolith(package.root, is_quant=is_quant)
    derived_content = _serialize_package(package)
    derived = extract_docx(derived_content)
    derived_employers = employer_matcher.named(derived.text)

    if derived.paragraph_count != source.paragraph_count:
        raise UnsupportedSource("paragraph_count_changed")
    if "Year in Industry" in derived.text:
        raise UnsupportedSource("year_in_industry_remaining")
    if re.findall(r"\b(?:2028|2029)\b", derived.text) != ["2028"]:
        raise UnsupportedSource("graduation_changed")
    if derived.text.count(degree_matches[0]) != 1:
        raise UnsupportedSource("plain_finance_degree_missing")
    if "Embedded within" in derived.text:
        raise UnsupportedSource("embedded_within_remaining")
    if "Shadowed analysts in PwC" not in derived.text:
        raise UnsupportedSource("pwc_shadowing_missing")
    if source_employers != derived_employers:
        raise UnsupportedSource("employer_names_changed")
    if apply_monolith:
        expected_lines = QUANT_MONOLITH_LINES if is_quant else NON_QUANT_MONOLITH_LINES
        if OLD_MONOLITH_PREFIX in derived.text or any(
            line not in derived.text for line in expected_lines
        ):
            raise UnsupportedSource("monolith_treatment_failed")

    return ContradictoryCvResult(
        source_content=content,
        content=derived_content,
        source_employers=source_employers,
        derived_employers=derived_employers,
        monolith_treatment_applied=apply_monolith,
    )


def _is_relative_to(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _snapshot(row: sqlite3.Row) -> DocumentSnapshot:
    return DocumentSnapshot(
        id=str(row["id"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        path=str(Path(str(row["path"])).resolve()),
        sha256=str(row["sha256"]),
        approved=bool(row["approved"]),
        tags_json=str(row["tags_json"]),
        created_at=str(row["created_at"]),
    )


def _verified_content(
    document: DocumentSnapshot,
    documents_dir: Path,
) -> bytes:
    path = Path(document.path).resolve()
    if not _is_relative_to(path, documents_dir):
        raise UnsupportedSource("document_path_outside_library")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise UnsupportedSource("document_file_missing") from exc
    if hashlib.sha256(content).hexdigest() != document.sha256:
        raise UnsupportedSource("document_file_drift")
    return content


def _safe_output_name(source_name: str, digest: str) -> str:
    basename = Path(source_name.replace("\\", "/")).name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(basename).stem).strip("._")
    return f"{stem or 'document'}_P23B_{digest[:8]}.docx"


def build_plan(db_path: Path, documents_dir: Path) -> RetirementPlan:
    """Reconstruct Phase 23 lineage and the exact affected live scope read-only."""

    resolved_db = Path(db_path).resolve()
    resolved_documents = Path(documents_dir).resolve()
    connection = sqlite3.connect(
        f"file:{resolved_db.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = list(
            connection.execute(
                "SELECT id,name,kind,path,sha256,approved,tags_json,created_at "
                "FROM documents ORDER BY created_at,id"
            )
        )
        applications = list(
            connection.execute(
                "SELECT a.id,a.selected_cv_id "
                "FROM applications a WHERE a.selected_cv_id IS NOT NULL "
                "ORDER BY a.created_at,a.id"
            )
        )
        employer_names = _database_employer_names(connection)
    finally:
        connection.close()

    documents = tuple(_snapshot(row) for row in rows)
    by_id = {document.id: document for document in documents}
    by_sha = {document.sha256: document for document in documents}
    contents: dict[str, bytes] = {}
    texts: dict[str, str] = {}
    for document in documents:
        try:
            content = _verified_content(document, resolved_documents)
        except UnsupportedSource:
            continue
        text = DocumentService.extract_docx_body_text(content)
        if text is None:
            continue
        contents[document.id] = content
        texts[document.id] = text

    corrections: list[CorrectionPlan] = []
    contradiction_ids: set[str] = set()
    for target_name in sorted(CONTRADICTORY_NAMES):
        matches = [
            document
            for document in documents
            if document.approved
            and document.kind.casefold() == "cv"
            and document.name == target_name
            and "Year in Industry" in texts.get(document.id, "")
            and re.findall(r"\b(?:2028|2029)\b", texts.get(document.id, ""))
            == ["2028"]
        ]
        if not matches:
            target_stem = Path(target_name).stem
            already_corrected = [
                document
                for document in documents
                if document.approved
                and document.name.startswith(f"{target_stem}_P23B_")
                and "Year in Industry" not in texts.get(document.id, "")
                and "Embedded within" not in texts.get(document.id, "")
                and re.findall(
                    r"\b(?:2028|2029)\b", texts.get(document.id, "")
                )
                == ["2028"]
            ]
            if len(already_corrected) == 1:
                continue
        if len(matches) != 1:
            raise UnsupportedSource(
                f"contradictory_target_count:{target_name}:{len(matches)}"
            )
        source = matches[0]
        is_quant = source.name == "Demo_Candidate_HRT_Quant_-_2028.docx"
        result = derive_contradictory_cv(
            contents[source.id],
            is_quant=is_quant,
            employer_names=employer_names,
        )
        digest = hashlib.sha256(result.content).hexdigest()
        corrections.append(
            CorrectionPlan(
                source=source,
                output_name=_safe_output_name(source.name, digest),
                output_sha256=digest,
                content=result.content,
                is_quant=is_quant,
                monolith_treatment_applied=result.monolith_treatment_applied,
            )
        )
        contradiction_ids.add(source.id)

    supersessions: list[SupersessionPlan] = []
    skipped: list[SkippedSource] = []
    for source in documents:
        source_text = texts.get(source.id, "")
        if (
            not source.approved
            or source.kind.casefold() != "cv"
            or "Embedded within" not in source_text
            or source.id in contradiction_ids
        ):
            continue
        try:
            tags, _ = _decode_tags(source.tags_json)
            transformed = transform_docx(
                contents[source.id],
                is_quant=bool(tags.intersection(QUANT_TAGS)),
                tags=tags,
                employer_names=employer_names,
            )
            derivative = by_sha.get(hashlib.sha256(transformed.content).hexdigest())
            if derivative is None:
                raise UnsupportedSource("phase23_derivative_missing")
            if not derivative.approved:
                raise UnsupportedSource("phase23_derivative_not_approved")
            if "Shadowed analysts in PwC" not in texts.get(derivative.id, ""):
                raise UnsupportedSource("phase23_derivative_invalid")
            supersessions.append(
                SupersessionPlan(
                    source_id=source.id,
                    source_name=source.name,
                    source_sha256=source.sha256,
                    derivative_id=derivative.id,
                    derivative_name=derivative.name,
                    derivative_sha256=derivative.sha256,
                )
            )
        except UnsupportedSource as exc:
            skipped.append(SkippedSource(source.id, source.name, str(exc)))

    affected_application_ids = tuple(
        str(row["id"])
        for row in applications
        if "Embedded within" in texts.get(str(row["selected_cv_id"]), "")
    )
    return RetirementPlan(
        db_path=resolved_db,
        documents_dir=resolved_documents,
        employer_names=employer_names,
        supersessions=tuple(supersessions),
        skipped=tuple(skipped),
        corrections=tuple(corrections),
        affected_application_ids=affected_application_ids,
    )


def stage_plan(plan: RetirementPlan, stage_dir: Path) -> RetirementPlan:
    resolved_stage = Path(stage_dir).resolve()
    resolved_stage.mkdir(parents=True, exist_ok=True)
    staged: list[CorrectionPlan] = []
    for item in plan.corrections:
        target = resolved_stage / item.output_name
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != item.output_sha256:
                raise UnsupportedSource("staged_target_mismatch")
        else:
            with target.open("xb") as handle:
                handle.write(item.content)
        if hashlib.sha256(target.read_bytes()).hexdigest() != item.output_sha256:
            raise UnsupportedSource("staged_hash_mismatch")
        staged.append(replace(item, staged_path=str(target)))
    return replace(plan, corrections=tuple(staged))


def _ensure_live_output(item: CorrectionPlan, documents_dir: Path) -> Path:
    if item.staged_path is None:
        raise UnsupportedSource("correction_not_staged")
    staged_content = Path(item.staged_path).read_bytes()
    if hashlib.sha256(staged_content).hexdigest() != item.output_sha256:
        raise UnsupportedSource("staged_hash_mismatch")
    target = documents_dir / item.output_name
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != item.output_sha256:
            raise UnsupportedSource("live_target_mismatch")
    else:
        with target.open("xb") as handle:
            handle.write(staged_content)
    return target.resolve()


def _verified_render_qa(item: CorrectionPlan, render_root: Path) -> None:
    output_dir = render_root / Path(item.output_name).stem
    pages = sorted(output_dir.glob("page-*.png"))
    if len(pages) != 1 or pages[0].name != "page-1.png" or pages[0].stat().st_size == 0:
        raise UnsupportedSource(f"render_qa_missing:{item.output_name}")


T = TypeVar("T")


def _retry_locked(operation: Callable[[], T], *, attempts: int = 8) -> T:
    for attempt in range(attempts):
        try:
            return operation()
        except OperationalError as exc:
            if "database is locked" not in str(exc).casefold() or attempt + 1 >= attempts:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError("database lock retry loop exhausted")


def _row_keys(db_path: Path) -> dict[str, frozenset[tuple[object, ...]]]:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        result: dict[str, frozenset[tuple[object, ...]]] = {}
        for table in tables:
            quoted = table.replace('"', '""')
            columns = list(connection.execute(f'PRAGMA table_info("{quoted}")'))
            primary = [str(row[1]) for row in sorted(columns, key=lambda row: row[5]) if row[5]]
            if primary:
                selection = ",".join(f'"{name.replace(chr(34), chr(34) * 2)}"' for name in primary)
                keys = frozenset(tuple(row) for row in connection.execute(f'SELECT {selection} FROM "{quoted}"'))
            else:
                keys = frozenset((row[0],) for row in connection.execute(f'SELECT rowid FROM "{quoted}"'))
            result[table] = keys
        return result
    finally:
        connection.close()


def _submission_markers(db_path: Path) -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return tuple(
            connection.execute(
                "SELECT id,submission_reference,applied_at,"
                "CASE WHEN state IN ('SUBMITTED','CONFIRMATION_VERIFIED','OA_PENDING',"
                "'INTERVIEW','REJECTED','OFFER') THEN state ELSE '' END "
                "FROM applications ORDER BY id"
            )
        )
    finally:
        connection.close()


def _application_states(db_path: Path) -> dict[str, str]:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT id,state FROM applications")
        }
    finally:
        connection.close()


def _settings_for(plan: RetirementPlan) -> Settings:
    if plan.db_path != plan.documents_dir.parent / "argus.db":
        raise ValueError("Phase 23b requires the canonical ARGUS data-directory layout")
    return Settings.load(
        {
            "ARGUS_DATA_DIR": str(plan.documents_dir.parent),
            "ARGUS_API_TOKEN": "phase23b-local-only",
            "ARGUS_AUTOMATION_MODE": "OFF",
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
        }
    )


def apply_plan(
    plan: RetirementPlan,
    *,
    actor: str = "phase23b",
    require_render_qa: bool = True,
    render_root: Path | None = None,
) -> ApplyReport:
    """Apply short, retryable, audited transactions without any delete path."""

    if require_render_qa:
        if render_root is None:
            raise ValueError("render_root is required")
        for item in plan.corrections:
            _verified_render_qa(item, Path(render_root).resolve())

    rows_before = _row_keys(plan.db_path)
    files_before = frozenset(path.resolve() for path in plan.documents_dir.rglob("*") if path.is_file())
    submissions_before = _submission_markers(plan.db_path)
    states_before = _application_states(plan.db_path)
    settings = _settings_for(plan)
    database = Database(settings)
    corrections_registered = 0
    superseded = 0
    application_changes: list[CvReselectionChange] = []
    missing = 0
    try:
        for item in plan.corrections:
            target = _ensure_live_output(item, plan.documents_dir)

            def register_correction() -> tuple[bool, bool]:
                with database.session_scope() as session:
                    source = session.get(Document, item.source.id)
                    if source is None or source.sha256 != item.source.sha256:
                        raise UnsupportedSource("correction_source_drift")
                    derivative = session.scalar(
                        select(Document).where(Document.sha256 == item.output_sha256)
                    )
                    created = False
                    if derivative is None:
                        if not source.approved:
                            raise UnsupportedSource("correction_source_drift")
                        derivative = Document(
                            name=item.output_name,
                            kind=source.kind,
                            path=str(target),
                            sha256=item.output_sha256,
                            approved=True,
                            tags_json=source.tags_json,
                        )
                        session.add(derivative)
                        session.flush()
                        append_audit(
                            session,
                            AuditInput(
                                actor,
                                "document.stored",
                                "document",
                                derivative.id,
                                {
                                    "approved": True,
                                    "kind": source.kind,
                                    "sha256": item.output_sha256,
                                },
                            ),
                        )
                        created = True
                    elif (
                        not derivative.approved
                        or Path(derivative.path).resolve() != target
                        or derivative.tags_json != source.tags_json
                    ):
                        raise UnsupportedSource("correction_derivative_conflict")
                    changed = DocumentService(session, plan.documents_dir).supersede(
                        source,
                        derivative,
                        actor=actor,
                        reason="phase23b_degree_and_pwc_correction",
                    )
                    return created, changed

            created, changed = _retry_locked(register_correction)
            corrections_registered += int(created)
            superseded += int(changed)

        for item in plan.supersessions:

            def retire_existing() -> bool:
                with database.session_scope() as session:
                    source = session.get(Document, item.source_id)
                    derivative = session.get(Document, item.derivative_id)
                    if (
                        source is None
                        or derivative is None
                        or source.sha256 != item.source_sha256
                        or derivative.sha256 != item.derivative_sha256
                    ):
                        raise UnsupportedSource("supersession_row_drift")
                    return DocumentService(session, plan.documents_dir).supersede(
                        source,
                        derivative,
                        actor=actor,
                        reason="phase23_corrected_derivative",
                    )

            superseded += int(_retry_locked(retire_existing))

        for application_id in plan.affected_application_ids:

            def reselect_one() -> tuple[CvReselectionChange | None, int]:
                with database.session_scope() as session:
                    report = reselect_existing_cvs(
                        session,
                        settings,
                        actor=actor,
                        application_ids=frozenset({application_id}),
                        forbidden_cv_body_phrases=("Embedded within",),
                    )
                    return (report.changes[0] if report.changes else None, report.required_cv_missing)

            change, item_missing = _retry_locked(reselect_one)
            if change is not None:
                application_changes.append(change)
            missing += item_missing
    finally:
        database.engine.dispose()

    rows_after = _row_keys(plan.db_path)
    files_after = frozenset(path.resolve() for path in plan.documents_dir.rglob("*") if path.is_file())
    submissions_after = _submission_markers(plan.db_path)
    states_after = _application_states(plan.db_path)
    rows_deleted = sum(
        len(keys.difference(rows_after.get(table, frozenset())))
        for table, keys in rows_before.items()
    )
    documents_deleted = len(files_before.difference(files_after))
    blocked_cleared = sum(
        state == "BLOCKED" and states_after.get(application_id) != "BLOCKED"
        for application_id, state in states_before.items()
    )
    submissions = int(submissions_after != submissions_before)
    if documents_deleted or rows_deleted or blocked_cleared or submissions:
        raise RuntimeError(
            "Phase 23b safety reconciliation failed: "
            f"files_deleted={documents_deleted}, rows_deleted={rows_deleted}, "
            f"blocked_cleared={blocked_cleared}, submissions={submissions}"
        )
    return ApplyReport(
        superseded=superseded,
        corrections_registered=corrections_registered,
        applications_changed=len(application_changes),
        required_cv_missing=missing,
        application_changes=tuple(application_changes),
        documents_deleted=documents_deleted,
        rows_deleted=rows_deleted,
        blocked_states_cleared=blocked_cleared,
        submissions=submissions,
    )
