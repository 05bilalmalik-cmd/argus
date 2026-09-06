from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.automation.runner import AutomationRunner, SubmissionBlocked
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, Opportunity
from app.scouting.programmes import resolve_programme_framing
from app.security.crypto import CryptoBox
from app.services.applications import ApplicationService
from app.services.cv_integrity import apply_cv_tag_repairs, plan_cv_tag_repairs
from app.services.documents import DocumentService


def _docx_bytes(*ranges: str, include_document_xml: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            "</Types>",
        )
        if include_document_xml:
            paragraphs = "".join(
                f"<w:p><w:r><w:t>Education {year_range}</w:t></w:r></w:p>"
                for year_range in ranges
            )
            archive.writestr(
                "word/document.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document '
                'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body>{paragraphs}</w:body></w:document>",
            )
    return buffer.getvalue()


def _docx_run_bytes(*runs: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        body = "".join(f"<w:r><w:t>{run}</w:t></w:r>" for run in runs)
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p>{body}</w:p></w:body></w:document>",
        )
    return buffer.getvalue()


def _runtime(tmp_path: Path) -> tuple[Settings, Database, CryptoBox]:
    settings = Settings.load(
        {"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_API_TOKEN": "test-token"}
    )
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return settings, database, CryptoBox.from_path(settings.secret_key_path)


def _queued_application(
    session,
    *,
    employer: str,
    programme_group: str,
    division: str = "Finance",
    location: str = "London",
) -> Application:
    opportunity = Opportunity(
        employer=employer,
        role_title="Analyst",
        division=division,
        programme_group=programme_group,
        location=location,
        cycle="2027",
        url=f"https://example.test/{employer.casefold().replace(' ', '-')}",
        cv_required=True,
    )
    session.add(opportunity)
    session.flush()
    application = Application(
        opportunity_id=opportunity.id,
        state=ApplicationState.QUEUED.value,
    )
    session.add(application)
    session.flush()
    return application


def test_store_bytes_stamps_content_year_and_removes_spoofed_derived_tag(
    tmp_path: Path,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        document = DocumentService(session, settings.documents_dir).store_bytes(
            filename="summer.docx",
            content=_docx_bytes("2025 – 2028"),
            kind="cv",
            tags=("summer-cv", "grad-2029"),
            approved=True,
        )

        tags = set(json.loads(document.tags_json))
        assert "grad-2028" in tags
        assert "grad-2029" not in tags


def test_store_bytes_recognises_a_year_split_across_docx_text_runs(
    tmp_path: Path,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        document = DocumentService(session, settings.documents_dir).store_bytes(
            filename="split-year.docx",
            content=_docx_run_bytes("Education 2025 – 20", "28"),
            kind="cv",
            tags=("summer-cv",),
            approved=True,
        )

        assert "grad-2028" in set(json.loads(document.tags_json))


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        pytest.param("not-docx.pdf", b"2025 - 2028", id="non-docx"),
        pytest.param("corrupt.docx", b"not a zip", id="corrupt-zip"),
        pytest.param(
            "missing-xml.docx",
            _docx_bytes(include_document_xml=False),
            id="missing-document-xml",
        ),
        pytest.param("missing-year.docx", _docx_bytes(), id="missing-year"),
        pytest.param(
            "ambiguous.docx",
            _docx_bytes("2025 – 2028", "2025 – 2029"),
            id="ambiguous-year",
        ),
    ],
)
def test_unparseable_or_ambiguous_cv_has_no_derived_tag_and_is_refused(
    tmp_path: Path,
    filename: str,
    content: bytes,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        document = service.store_bytes(
            filename=filename,
            content=content,
            kind="cv",
            tags=("summer-cv", "london"),
            approved=True,
        )

        tags = set(json.loads(document.tags_json))
        assert not tags.intersection({"grad-2028", "grad-2029"})
        assert (
            service.select_approved(
                "cv", ("london",), required_tag="summer-cv"
            )
            is None
        )


def test_required_variant_is_a_hard_filter_before_preference_scoring(
    tmp_path: Path,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        service.store_bytes(
            filename="firm-match-wrong-year.docx",
            content=_docx_bytes("2025 – 2029"),
            kind="cv",
            tags=("example-employer", "finance", "london", "yii-cv"),
            approved=True,
        )
        correct = service.store_bytes(
            filename="correct-summer.docx",
            content=_docx_bytes("2025–2028"),
            kind="cv",
            tags=("london", "summer-cv"),
            approved=True,
        )

        selected = service.select_approved(
            "cv",
            ("example-employer", "finance", "london"),
            required_tag="summer-cv",
        )

        assert selected is not None
        assert selected.id == correct.id


@pytest.mark.parametrize(
    ("programme_group", "variant_tag", "wrong_range"),
    [
        pytest.param("summer", "summer-cv", "2025 – 2029", id="summer-with-2029"),
        pytest.param(
            "year_in_industry",
            "yii-cv",
            "2025 – 2028",
            id="yii-with-2028",
        ),
        pytest.param(
            "spring_week",
            "yii-cv",
            "2025 – 2028",
            id="spring-with-2028",
        ),
    ],
)
def test_prepare_refuses_variant_tag_when_document_content_disagrees(
    tmp_path: Path,
    programme_group: str,
    variant_tag: str,
    wrong_range: str,
) -> None:
    settings, database, crypto = _runtime(tmp_path)
    with database.session_scope() as session:
        DocumentService(session, settings.documents_dir).store_bytes(
            filename="wrong-content.docx",
            content=_docx_bytes(wrong_range),
            kind="cv",
            tags=("example-employer", "finance", "london", variant_tag),
            approved=True,
        )
        application = _queued_application(
            session,
            employer="Example Employer",
            programme_group=programme_group,
        )

        package = ApplicationService(session, settings, crypto).prepare(application.id)

        assert package.ready is False
        assert package.cv_id is None
        assert package.reason_codes == ("required_cv_missing",)
        assert package.application.state == ApplicationState.NEEDS_USER.value


def test_financial_data_analytics_untagged_2029_regression_fails_closed(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _runtime(tmp_path)
    with database.session_scope() as session:
        culprit = DocumentService(session, settings.documents_dir).store_bytes(
            filename="Financial_Data_Analytics.docx",
            content=_docx_bytes("2025 – 2029"),
            kind="cv",
            tags=("financial-data-analytics", "finance", "london"),
            approved=True,
        )
        application = _queued_application(
            session,
            employer="Financial Data Analytics",
            programme_group="summer",
        )

        package = ApplicationService(session, settings, crypto).prepare(application.id)

        assert package.ready is False
        assert package.cv_id is None
        assert package.application.selected_cv_id != culprit.id
        assert package.reason_codes == ("required_cv_missing",)


def test_runner_revalidates_cv_content_not_only_human_variant_tag(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _runtime(tmp_path)
    with database.session_scope() as session:
        document = DocumentService(session, settings.documents_dir).store_bytes(
            filename="spoofed-summer.docx",
            content=_docx_bytes("2025 – 2029"),
            kind="cv",
            tags=("summer-cv",),
            approved=True,
        )
        opportunity = Opportunity(
            employer="Example Employer",
            role_title="Summer Analyst",
            programme_group="summer",
            cycle="2027",
            url="https://example.test/runner-content-check",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.PACKAGE_PREPARED.value,
            selected_cv_id=document.id,
        )
        session.add(application)
        session.flush()
        framing = resolve_programme_framing("summer")
        assert framing is not None

        with pytest.raises(SubmissionBlocked) as error:
            AutomationRunner(database, settings, crypto)._approved_inputs(
                session, application, framing
            )

        assert error.value.code == "programme_framing_mismatch"


def test_repair_refuses_if_document_content_changes_after_planning(
    tmp_path: Path,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        document = DocumentService(session, settings.documents_dir).store_bytes(
            filename="planned.docx",
            content=_docx_bytes("2025 – 2028"),
            kind="cv",
            tags=("summer-cv",),
            approved=True,
        )
        document.tags_json = json.dumps(["summer-cv"])
        session.flush()
        plan = plan_cv_tag_repairs(session)
        Path(document.path).write_bytes(_docx_bytes("2025 – 2029"))

        with pytest.raises(RuntimeError, match="changed after repair planning"):
            apply_cv_tag_repairs(session, plan)
