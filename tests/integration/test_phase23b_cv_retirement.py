from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from sqlalchemy import func, select

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, AuditOutbox, Document, Opportunity
from app.services.applications import reselect_existing_cvs
from app.services.documents import DocumentService
from scripts.phase23_cv_corrections import (
    OLD_MONOLITH_PREFIX,
    QUANT_MONOLITH_LINES,
    extract_docx,
    transform_docx,
)


def _docx_bytes(*paragraph_runs: tuple[str, ...]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            "</Types>",
        )
        paragraphs = "".join(
            "<w:p>"
            + "".join(f"<w:r><w:t>{escape(run)}</w:t></w:r>" for run in runs)
            + "</w:p>"
            for runs in paragraph_runs
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{paragraphs}</w:body></w:document>",
        )
    return buffer.getvalue()


def _summer_cv(body: str, *, contradictory: bool = False) -> bytes:
    degree = (
        ("Bachelor of Science: Finance with ", "Year in Industry")
        if contradictory
        else ("Bachelor of Science: Finance",)
    )
    return _docx_bytes(
        degree,
        ("2025 – 20", "28"),
        (body,),
    )


def _runtime(tmp_path: Path) -> tuple[Settings, Database]:
    settings = Settings.load(
        {"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_API_TOKEN": "test-token"}
    )
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return settings, database


def _opportunity(session, *, employer: str, suffix: str) -> Opportunity:
    opportunity = Opportunity(
        employer=employer,
        role_title="Summer Analyst",
        division="Investment Banking",
        programme_group="summer",
        location="London",
        cycle="2028",
        url=f"https://example.test/{suffix}",
        cv_required=True,
    )
    session.add(opportunity)
    session.flush()
    return opportunity


def _application(
    session,
    opportunity: Opportunity,
    *,
    selected_cv_id: str,
    state: ApplicationState = ApplicationState.BLOCKED,
) -> Application:
    application = Application(
        opportunity_id=opportunity.id,
        state=state.value,
        selected_cv_id=selected_cv_id,
    )
    session.add(application)
    session.flush()
    return application


def test_summer_selection_rejects_year_in_industry_with_2028(tmp_path: Path) -> None:
    """Removing the degree/year coupling check must make this test fail."""

    settings, database = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        contradictory = service.store_bytes(
            filename="contradictory.docx",
            content=_summer_cv("Apollo evidence", contradictory=True),
            kind="cv",
            tags=("apollo", "investment-banking", "summer-cv"),
            approved=True,
        )
        corrected = service.store_bytes(
            filename="corrected.docx",
            content=_summer_cv("Apollo evidence"),
            kind="cv",
            tags=("apollo", "investment-banking", "summer-cv"),
            approved=True,
        )

        selected = service.select_approved(
            "cv",
            ("apollo", "investment-banking"),
            required_tag="summer-cv",
            employer="Apollo",
        )

        assert DocumentService.cv_degree_year_is_consistent(
            Path(contradictory.path).read_bytes()
        ) is False
        assert DocumentService.cv_degree_year_is_consistent(
            Path(corrected.path).read_bytes()
        ) is True
        assert selected is not None and selected.id == corrected.id


def test_supersession_is_audited_and_preserves_every_row_and_file(
    tmp_path: Path,
) -> None:
    """Omitting the deapproval or deleting either artifact must fail this test."""

    settings, database = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        source = service.store_bytes(
            filename="source.docx",
            content=_summer_cv("Embedded within PwC’s Risk Analytics division."),
            kind="cv",
            tags=("apollo", "summer-cv"),
            approved=True,
        )
        derivative = service.store_bytes(
            filename="source_P23.docx",
            content=_summer_cv("Shadowed analysts in PwC’s Risk Analytics division."),
            kind="cv",
            tags=("apollo", "summer-cv"),
            approved=True,
        )
        rows_before = session.scalar(select(func.count()).select_from(Document))
        files_before = {
            Path(document.path): Path(document.path).read_bytes()
            for document in session.scalars(select(Document))
        }

        changed = service.supersede(
            source,
            derivative,
            actor="phase23b-test",
            reason="phase23_corrected_derivative",
        )

        assert changed is True
        assert source.approved is False
        assert derivative.approved is True
        assert session.scalar(select(func.count()).select_from(Document)) == rows_before
        assert all(path.read_bytes() == content for path, content in files_before.items())
        event = session.scalar(
            select(AuditOutbox).where(
                AuditOutbox.event_type == "document.superseded",
                AuditOutbox.entity_id == source.id,
            )
        )
        assert event is not None
        details = json.loads(event.details_json)
        assert details["derivative_document_id"] == derivative.id
        assert details["reason"] == "phase23_corrected_derivative"

        approved_documents = tuple(
            session.scalars(select(Document).where(Document.approved.is_(True)))
        )
        assert all(
            not (
                "Embedded within"
                in (
                    DocumentService.extract_docx_body_text(
                        Path(document.path).read_bytes()
                    )
                    or ""
                )
                and document.id == source.id
            )
            for document in approved_documents
        )
        assert all(
            not (
                "Year in Industry"
                in (
                    DocumentService.extract_docx_body_text(
                        Path(document.path).read_bytes()
                    )
                    or ""
                )
                and DocumentService.derive_graduation_tag(
                    Path(document.path).read_bytes()
                )
                != "grad-2029"
            )
            for document in approved_documents
        )


def test_scoped_reselection_excludes_overstatement_and_preserves_blocked_state(
    tmp_path: Path,
) -> None:
    """Removing either the scope or forbidden-body filter must fail this test."""

    settings, database = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        overstated = service.store_bytes(
            filename="overstated.docx",
            content=_summer_cv("Embedded within PwC’s Risk Analytics division."),
            kind="cv",
            tags=("apollo", "investment-banking", "summer-cv"),
            approved=True,
        )
        corrected = service.store_bytes(
            filename="corrected.docx",
            content=_summer_cv("Shadowed analysts in PwC’s Risk Analytics division."),
            kind="cv",
            tags=("apollo", "investment-banking", "summer-cv"),
            approved=True,
        )
        first_opportunity = _opportunity(session, employer="Apollo", suffix="first")
        second_opportunity = _opportunity(session, employer="Apollo", suffix="second")
        affected = _application(
            session,
            first_opportunity,
            selected_cv_id=overstated.id,
        )
        untouched = _application(
            session,
            second_opportunity,
            selected_cv_id=overstated.id,
        )

        report = reselect_existing_cvs(
            session,
            settings,
            actor="phase23b-test",
            application_ids=frozenset({affected.id}),
            forbidden_cv_body_phrases=("Embedded within",),
        )

        assert report.scanned == 1
        assert report.changed == 1
        assert report.required_cv_missing == 0
        assert affected.selected_cv_id == corrected.id
        assert affected.state == ApplicationState.BLOCKED.value
        assert untouched.selected_cv_id == overstated.id


def test_scoped_reselection_missing_cv_never_clears_blocked(
    tmp_path: Path,
) -> None:
    """Changing BLOCKED to NEEDS_USER must make this test fail."""

    settings, database = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        overstated = service.store_bytes(
            filename="overstated.docx",
            content=_summer_cv("Embedded within PwC’s Risk Analytics division."),
            kind="cv",
            tags=("apollo", "investment-banking", "summer-cv"),
            approved=True,
        )
        opportunity = _opportunity(session, employer="Apollo", suffix="missing")
        application = _application(
            session,
            opportunity,
            selected_cv_id=overstated.id,
        )

        report = reselect_existing_cvs(
            session,
            settings,
            actor="phase23b-test",
            application_ids=frozenset({application.id}),
            forbidden_cv_body_phrases=("Embedded within",),
        )

        assert report.scanned == 1
        assert report.required_cv_missing == 1
        assert application.selected_cv_id is None
        assert application.state == ApplicationState.BLOCKED.value
        assert application.next_action == (
            "Upload and approve an employer-compatible CV "
            "(required_cv_missing; BLOCKED state preserved)"
        )


def test_contradictory_cv_transform_is_run_aware_and_quant_exact() -> None:
    """A paragraph-level or degree-only replacement must fail this test."""

    from scripts.phase23b_retire_cvs import derive_contradictory_cv

    source = _docx_bytes(
        ("Bachelor of Science: Quant Finance with ", "Year in ", "Industry"),
        ("2025 – 20", "28"),
        (
            "Embedded within ",
            "PwC’s Risk Analytics division, gaining exposure to ",
            "quantitative risk modelling.",
        ),
        (OLD_MONOLITH_PREFIX,),
        ("Hudson River Trading evidence",),
    )
    source_text = extract_docx(source).text

    result = derive_contradictory_cv(
        source,
        is_quant=True,
        employer_names=("PwC", "Hudson River Trading"),
    )
    derived = extract_docx(result.content)

    assert source == result.source_content
    assert derived.paragraph_count == extract_docx(source).paragraph_count
    assert "Bachelor of Science: Quant Finance with Year in Industry" not in derived.text
    assert "Bachelor of Science: Quant Finance" in derived.text
    assert "Year in Industry" not in derived.text
    assert derived.text.count("2028") == 1
    assert "2029" not in derived.text
    assert "Embedded within" not in derived.text
    assert "Shadowed analysts in PwC" in derived.text
    assert OLD_MONOLITH_PREFIX not in derived.text
    assert all(line in derived.text for line in QUANT_MONOLITH_LINES)
    assert result.source_employers == result.derived_employers
    assert result.source_employers == frozenset({"PwC", "Hudson River Trading"})
    assert source_text != derived.text


def test_phase23b_plan_and_apply_are_scoped_non_destructive_and_idempotent(
    tmp_path: Path,
) -> None:
    """Any untracked write, deletion, unsafe fallback, or BLOCKED clear must fail."""

    from scripts.phase23b_retire_cvs import apply_plan, build_plan, stage_plan

    settings, database = _runtime(tmp_path)
    tags = ("pwc", "investment-banking", "summer-cv")
    supported_source = _docx_bytes(
        ("Bachelor of Science: Finance",),
        ("2025 – 2028",),
        (
            "Embedded within PwC’s Risk Analytics division, gaining exposure to ",
            "financial controls.",
        ),
        (OLD_MONOLITH_PREFIX,),
    )
    supported_derivative = transform_docx(
        supported_source,
        is_quant=False,
        tags=frozenset({*tags, "grad-2028"}),
        employer_names=("PwC",),
    ).content
    unsupported_source = _docx_bytes(
        ("Bachelor of Science: Finance",),
        ("2025 – 2028",),
        (
            "Embedded within PwC’s Risk Analytics division, gaining practical exposure ",
            "to financial controls.",
        ),
        (OLD_MONOLITH_PREFIX,),
    )
    apollo_contradictory = _docx_bytes(
        ("Bachelor of Science: Finance with ", "Year in Industry"),
        ("2025 – 2028",),
        (
            "Embedded within PwC’s Risk Analytics division, gaining exposure to ",
            "Apollo evidence.",
        ),
        (OLD_MONOLITH_PREFIX,),
    )
    hrt_contradictory = _docx_bytes(
        ("Bachelor of Science: Quant Finance with ", "Year in Industry"),
        ("2025 – 2028",),
        (
            "Embedded within PwC’s Risk Analytics division, gaining exposure to ",
            "Hudson River Trading evidence.",
        ),
        ("Existing HRT-specific Monolith evidence.",),
    )

    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        source = service.store_bytes(
            filename="supported-source.docx",
            content=supported_source,
            kind="cv",
            tags=tags,
            approved=True,
        )
        derivative = service.store_bytes(
            filename="supported-source_P23.docx",
            content=supported_derivative,
            kind="cv",
            tags=tags,
            approved=True,
        )
        unsupported = service.store_bytes(
            filename="unsupported.docx",
            content=unsupported_source,
            kind="cv",
            tags=tags,
            approved=True,
        )
        apollo = service.store_bytes(
            filename="Demo_Candidate_Apollo_PE_-2028.docx",
            content=apollo_contradictory,
            kind="cv",
            tags=("apollo", "private-equity", "summer-cv"),
            approved=True,
        )
        hrt = service.store_bytes(
            filename="Demo_Candidate_HRT_Quant_-_2028.docx",
            content=hrt_contradictory,
            kind="cv",
            tags=("quant", "summer-cv"),
            approved=True,
        )
        opportunity = _opportunity(session, employer="PwC", suffix="affected")
        application = _application(
            session,
            opportunity,
            selected_cv_id=source.id,
        )
        source_id = source.id
        derivative_id = derivative.id
        unsupported_id = unsupported.id
        apollo_id = apollo.id
        hrt_id = hrt.id
        application_id = application.id

    with database.SessionLocal() as session:
        rows_before = set(session.scalars(select(Document.id)))
    files_before = {path: path.read_bytes() for path in settings.documents_dir.iterdir()}

    plan = build_plan(settings.data_dir / "argus.db", settings.documents_dir)

    assert len(plan.supersessions) == 1
    assert plan.supersessions[0].source_id == source_id
    assert plan.supersessions[0].derivative_id == derivative_id
    assert [(item.source_id, item.reason) for item in plan.skipped] == [
        (unsupported_id, "unsupported_pwc_opening")
    ]
    assert {item.source_id for item in plan.corrections} == {apollo_id, hrt_id}
    assert plan.affected_application_ids == (application_id,)

    staged = stage_plan(plan, tmp_path / "stage")
    report = apply_plan(staged, actor="phase23b-test", require_render_qa=False)

    assert report.superseded == 3
    assert report.corrections_registered == 2
    assert report.applications_changed == 1
    assert report.required_cv_missing == 0
    assert report.documents_deleted == 0
    assert report.rows_deleted == 0
    assert report.blocked_states_cleared == 0
    assert report.submissions == 0
    assert len(report.application_changes) == 1
    assert report.application_changes[0].before_cv_id == source_id
    assert report.application_changes[0].after_cv_id == derivative_id

    with database.SessionLocal() as session:
        assert source.approved is True  # detached pre-apply snapshot is unchanged
        assert session.get(Document, source_id).approved is False
        assert session.get(Document, derivative_id).approved is True
        assert session.get(Document, unsupported_id).approved is True
        assert session.get(Document, apollo_id).approved is False
        assert session.get(Document, hrt_id).approved is False
        live_application = session.get(Application, application_id)
        assert live_application.selected_cv_id == derivative_id
        assert live_application.state == ApplicationState.BLOCKED.value
        rows_after = set(session.scalars(select(Document.id)))
        approved = tuple(
            session.scalars(select(Document).where(Document.approved.is_(True)))
        )
        assert rows_before <= rows_after
        assert len(rows_after - rows_before) == 2
        for document in approved:
            content = Path(document.path).read_bytes()
            text = DocumentService.extract_docx_body_text(content) or ""
            assert not (
                "Year in Industry" in text
                and DocumentService.derive_graduation_tag(content) != "grad-2029"
            )
        assert all(
            path.is_file() and path.read_bytes() == content
            for path, content in files_before.items()
        )

    post_plan = build_plan(settings.data_dir / "argus.db", settings.documents_dir)
    assert post_plan.supersessions == ()
    assert post_plan.corrections == ()
    assert post_plan.affected_application_ids == ()
    assert [(item.source_id, item.reason) for item in post_plan.skipped] == [
        (unsupported_id, "unsupported_pwc_opening")
    ]
