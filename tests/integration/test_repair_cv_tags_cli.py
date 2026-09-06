from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from sqlalchemy import func, select

from app.cli import main
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, AuditEvent, Document, Opportunity
from app.services.documents import DocumentService


def _docx_bytes(year: int, marker: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{marker} Education 2025 – {year}</w:t>"
            "</w:r></w:p></w:body>"
            "</w:document>",
        )
    return buffer.getvalue()


def _legacy_document(
    service: DocumentService,
    *,
    filename: str,
    year: int,
    tags: tuple[str, ...],
) -> Document:
    document = service.store_bytes(
        filename=filename,
        content=_docx_bytes(year, filename),
        kind="cv",
        tags=tags,
        approved=True,
        actor="test-fixture",
    )
    # Simulate a pre-Phase-7 row even after store_bytes learns to derive tags.
    document.tags_json = json.dumps(sorted(tags))
    service.session.flush()
    return document


def _file_snapshot(documents: list[Document]) -> dict[str, str]:
    return {
        item.path: hashlib.sha256(Path(item.path).read_bytes()).hexdigest()
        for item in documents
    }


def test_repair_cv_tags_defaults_to_dry_run_then_applies_idempotently_without_deletion(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_API_TOKEN", "test-token")
    settings = Settings.load()
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()

    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        wrong_yii = _legacy_document(
            service,
            filename="wrong-yii.docx",
            year=2028,
            tags=("example-employer", "yii-cv"),
        )
        untagged_yii = _legacy_document(
            service,
            filename="untagged-yii.docx",
            year=2029,
            tags=("another-employer",),
        )
        legacy_summer = _legacy_document(
            service,
            filename="legacy-summer.docx",
            year=2028,
            tags=("apply-cv", "legacy-employer"),
        )
        correct_summer = _legacy_document(
            service,
            filename="correct-summer.docx",
            year=2028,
            tags=("example-employer", "summer-cv"),
        )
        missing_firm_wrong = _legacy_document(
            service,
            filename="missing-firm-wrong.docx",
            year=2029,
            tags=("no-cv-firm", "summer-cv"),
        )

        corrected_opportunity = Opportunity(
            employer="Example Employer",
            role_title="Summer Analyst",
            division="Private Equity",
            programme_group="summer",
            location="London",
            cycle="2027",
            url="https://example.test/corrected",
        )
        missing_opportunity = Opportunity(
            employer="No CV Firm",
            role_title="Summer Analyst",
            division="Infrastructure",
            programme_group="summer",
            location="Manchester",
            cycle="2027",
            url="https://example.test/missing",
        )
        session.add_all([corrected_opportunity, missing_opportunity])
        session.flush()
        corrected_application = Application(
            opportunity_id=corrected_opportunity.id,
            state=ApplicationState.BLOCKED.value,
            selected_cv_id=untagged_yii.id,
        )
        missing_application = Application(
            opportunity_id=missing_opportunity.id,
            state=ApplicationState.QUEUED.value,
            selected_cv_id=missing_firm_wrong.id,
        )
        session.add_all([corrected_application, missing_application])
        session.flush()
        document_ids = {
            wrong_yii.id,
            untagged_yii.id,
            legacy_summer.id,
            correct_summer.id,
            missing_firm_wrong.id,
        }
        corrected_application_id = corrected_application.id
        missing_application_id = missing_application.id
        before_files = _file_snapshot(
            [wrong_yii, untagged_yii, legacy_summer, correct_summer, missing_firm_wrong]
        )

    assert main(["repair-cv-tags"]) == 0
    dry_run_output = capsys.readouterr().out
    assert "Mode: DRY RUN" in dry_run_output
    assert "Before" in dry_run_output
    assert "After" in dry_run_output
    assert "Invariant violations: 0" in dry_run_output
    assert not list((settings.data_dir / "backups").glob("*.pre-cv-tag-repair-*.bak"))

    with database.session_scope() as session:
        assert set(json.loads(session.get(Document, wrong_yii.id).tags_json)) == {
            "example-employer",
            "yii-cv",
        }
        assert (
            session.get(Application, corrected_application_id).selected_cv_id
            == untagged_yii.id
        )
        assert (
            session.get(Application, missing_application_id).state
            == ApplicationState.QUEUED.value
        )

    assert main(["repair-cv-tags", "--apply"]) == 0
    applied_output = capsys.readouterr().out
    assert "Mode: APPLIED" in applied_output
    assert "Invariant violations: 0" in applied_output
    assert "Contradictory selected CVs: 0" in applied_output
    backups = list((settings.data_dir / "backups").glob("*.pre-cv-tag-repair-*.bak"))
    assert len(backups) == 1

    with database.session_scope() as session:
        wrong_tags = set(json.loads(session.get(Document, wrong_yii.id).tags_json))
        untagged_tags = set(json.loads(session.get(Document, untagged_yii.id).tags_json))
        legacy_tags = set(json.loads(session.get(Document, legacy_summer.id).tags_json))
        assert {"summer-cv", "grad-2028"} <= wrong_tags
        assert "yii-cv" not in wrong_tags
        assert {"yii-cv", "grad-2029"} <= untagged_tags
        assert {"apply-cv", "summer-cv", "grad-2028"} <= legacy_tags
        corrected = session.get(Application, corrected_application_id)
        assert corrected.selected_cv_id is not None
        assert corrected.selected_cv_id != untagged_yii.id
        corrected_cv = session.get(Document, corrected.selected_cv_id)
        assert DocumentService.derived_tag_for_document(corrected_cv) == "grad-2028"
        assert DocumentService.cv_matches_variant(corrected_cv, "summer-cv") is True
        assert corrected.state == ApplicationState.BLOCKED.value
        assert session.get(Application, missing_application_id).selected_cv_id is None
        assert (
            session.get(Application, missing_application_id).state
            == ApplicationState.NEEDS_USER.value
        )
        assert set(session.scalars(select(Document.id)).all()) == document_ids
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "document.cv_tags_repaired")
            )
            == 5
        )
        after_documents = list(session.scalars(select(Document).order_by(Document.id)).all())
        assert _file_snapshot(after_documents) == before_files

    assert main(["repair-cv-tags", "--apply"]) == 0
    second_output = capsys.readouterr().out
    assert "Rows changed: 0" in second_output
    assert "Applications reselected: 0" in second_output
    assert "Contradictory selected CVs: 0" in second_output

    with database.session_scope() as session:
        assert set(session.scalars(select(Document.id)).all()) == document_ids
        assert _file_snapshot(list(session.scalars(select(Document)).all())) == before_files
