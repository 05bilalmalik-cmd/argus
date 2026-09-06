from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, AuditOutbox, Document, Opportunity
from app.security.crypto import CryptoBox
from app.services import applications as application_services
from app.services.applications import ApplicationService
from app.services.documents import DocumentService


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
            + "".join(f"<w:r><w:t>{run}</w:t></w:r>" for run in runs)
            + "</w:p>"
            for runs in paragraph_runs
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{paragraphs}</w:body></w:document>",
        )
    return buffer.getvalue()


def _cv_bytes(body: str = "Evidence-led candidate profile") -> bytes:
    return _docx_bytes(
        ("Bachelor of Science: Finance",),
        ("2025 – 2028",),
        (body,),
    )


def _yii_cv_bytes() -> bytes:
    return _docx_bytes(
        ("Bachelor of Science: Finance with ", "Year in Industry"),
        ("2025 – 20", "29"),
        ("Evidence-led candidate profile",),
    )


def _runtime(tmp_path: Path) -> tuple[Settings, Database, CryptoBox]:
    settings = Settings.load(
        {"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_API_TOKEN": "test-token"}
    )
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return settings, database, CryptoBox.from_path(settings.secret_key_path)


def _opportunity(
    session,
    *,
    employer: str,
    programme_group: str = "summer",
    division: str = "Investment Banking",
) -> Opportunity:
    opportunity = Opportunity(
        employer=employer,
        role_title="Summer Analyst",
        division=division,
        programme_group=programme_group,
        location="London",
        cycle="2028",
        url=f"https://example.test/{len(session.new)}",
        cv_required=True,
    )
    session.add(opportunity)
    session.flush()
    return opportunity


def _application(
    session,
    opportunity: Opportunity,
    *,
    state: ApplicationState = ApplicationState.QUEUED,
    selected_cv_id: str | None = None,
) -> Application:
    application = Application(
        opportunity_id=opportunity.id,
        state=state.value,
        selected_cv_id=selected_cv_id,
    )
    session.add(application)
    session.flush()
    return application


def test_employer_normalisation_resolves_jp_morgan_variants() -> None:
    keys = {
        DocumentService.normalise_employer_name(value)
        for value in ("J.P. Morgan", "JPMorgan", "JP Morgan Chase & Co.")
    }

    assert keys == {"jpmorgan"}
    assert (
        DocumentService.normalise_employer_name("Example Capital Ltd")
        == DocumentService.normalise_employer_name(" example-capital LLP ")
    )
    assert DocumentService.normalise_employer_name(
        "RSM UK"
    ) == DocumentService.normalise_employer_name("RSM")
    assert DocumentService.normalise_employer_name(
        "IMC Trading"
    ) == DocumentService.normalise_employer_name("IMC")


@pytest.mark.parametrize(
    ("tag", "expected_key"),
    (
        ("mha", "mha"),
        ("loomis-sayles", "loomissayles"),
        ("ing", "ing"),
        ("evelyn-partners", "evelynpartners"),
        ("aviva-investors", "avivainvestors"),
    ),
)
def test_library_employer_markers_remain_exclusive_without_opportunities(
    tmp_path: Path,
    tag: str,
    expected_key: str,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        document = service.store_bytes(
            filename=f"{tag}.docx",
            content=_cv_bytes(),
            kind="cv",
            tags=(tag, "summer-cv"),
            approved=True,
        )

        exclusivity = service.employer_exclusivity(document)

        assert exclusivity.is_exclusive
        assert exclusivity.employer_keys == frozenset({expected_key})


def test_known_employer_identity_set_is_cached_for_bulk_selection(
    tmp_path: Path,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        _opportunity(session, employer="J.P. Morgan")
        _opportunity(session, employer="DRW")

        first = service.known_employer_keys()
        second = service.known_employer_keys()

        assert first is second
        assert {"jpmorgan", "drw"}.issubset(first)


def test_bulk_selection_reads_each_verified_cv_only_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        _opportunity(session, employer="DRW")
        document = service.store_bytes(
            filename="generic.docx",
            content=_cv_bytes(),
            kind="cv",
            tags=("investment-banking", "summer-cv"),
            approved=True,
        )
        original_read_bytes = Path.read_bytes
        reads = 0

        def counted_read_bytes(path: Path) -> bytes:
            nonlocal reads
            if path.resolve() == Path(document.path).resolve():
                reads += 1
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
        for _ in range(2):
            selected = service.select_approved(
                "cv",
                ("drw", "investment-banking", "london"),
                required_tag="summer-cv",
                employer="DRW",
            )
            assert selected is not None and selected.id == document.id

        assert reads == 1


def test_selection_tie_break_handles_legacy_naive_and_new_aware_timestamps(
    tmp_path: Path,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        legacy = service.store_bytes(
            filename="legacy.txt",
            content=b"legacy",
            kind="cover_letter",
            tags=("london",),
            approved=True,
        )
        current = service.store_bytes(
            filename="current.txt",
            content=b"current",
            kind="cover_letter",
            tags=("london",),
            approved=True,
        )
        legacy.created_at = datetime(2026, 1, 1)
        current.created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        session.flush()

        selected = service.select_approved("cover_letter", ("london",))

        assert selected is not None and selected.id == legacy.id


def test_employer_tagged_cv_is_exclusive_but_still_selected_for_own_employer(
    tmp_path: Path,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        _opportunity(session, employer="J.P. Morgan")
        _opportunity(session, employer="DRW", division="Quantitative Trading")
        tailored = service.store_bytes(
            filename="jp-morgan.docx",
            content=_cv_bytes("JPMorgan offers a collaborative platform."),
            kind="cv",
            tags=("jp-morgan", "investment-banking", "summer-cv"),
            approved=True,
        )
        generic = service.store_bytes(
            filename="generic.docx",
            content=_cv_bytes(),
            kind="cv",
            tags=("investment-banking", "summer-cv"),
            approved=True,
        )

        exclusivity = service.employer_exclusivity(tailored)
        own = service.select_approved(
            "cv",
            ("jp-morgan", "investment-banking", "london"),
            required_tag="summer-cv",
            employer="JP Morgan Chase & Co.",
        )
        other = service.select_approved(
            "cv",
            ("drw", "investment-banking", "london"),
            required_tag="summer-cv",
            employer="DRW",
        )

        assert exclusivity.is_exclusive is True
        assert exclusivity.employer_keys == frozenset({"jpmorgan"})
        assert own is not None and own.id == tailored.id
        assert other is not None and other.id == generic.id


def test_untagged_cv_body_naming_another_employer_is_refused(tmp_path: Path) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        _opportunity(session, employer="Wellington Management")
        _opportunity(session, employer="DRW", division="Quantitative Trading")
        service.store_bytes(
            filename="untagged-tailored.docx",
            content=_cv_bytes(
                "Experience complementing Wellington's collaborative, fundamental approach."
            ),
            kind="cv",
            tags=("quantitative-trading", "summer-cv"),
            approved=True,
        )

        selected = service.select_approved(
            "cv",
            ("drw", "quantitative-trading", "london"),
            required_tag="summer-cv",
            employer="DRW",
        )

        assert selected is None


def test_common_lowercase_words_are_not_single_word_employer_evidence(
    tmp_path: Path,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        for employer in ("Accuracy", "Platform_", "DRW"):
            _opportunity(session, employer=employer)
        generic = service.store_bytes(
            filename="generic.docx",
            content=_cv_bytes(
                "Improved execution accuracy on a quantitative research platform."
            ),
            kind="cv",
            tags=("investment-banking", "summer-cv"),
            approved=True,
        )

        selected = service.select_approved(
            "cv",
            ("drw", "investment-banking", "london"),
            required_tag="summer-cv",
            employer="DRW",
        )

        assert selected is not None and selected.id == generic.id


def test_common_candidate_background_employers_do_not_make_blanket_cv_exclusive(
    tmp_path: Path,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        for employer in ("PwC", "IMC Trading", "DRW"):
            _opportunity(session, employer=employer)
        shared_history = "Completed a PwC placement. Competed in IMC Trading's challenge."
        for index, tag in enumerate(("asset-management", "audit"), start=1):
            service.store_bytes(
                filename=f"generic-yii-{index}.docx",
                content=_docx_bytes(
                    ("Bachelor of Science: Finance with Year in Industry",),
                    ("2025 – 2029",),
                    (shared_history, f" Generic profile {index}"),
                ),
                kind="cv",
                tags=(tag, "yii-cv"),
                approved=True,
            )
        summer = service.store_bytes(
            filename="generic-summer.docx",
            content=_cv_bytes(shared_history),
            kind="cv",
            tags=("investment-banking", "summer-cv"),
            approved=True,
        )

        selected = service.select_approved(
            "cv",
            ("drw", "investment-banking", "london"),
            required_tag="summer-cv",
            employer="DRW",
        )

        assert service.candidate_background_employer_keys() == frozenset(
            {"pwc", "imc"}
        )
        assert selected is not None and selected.id == summer.id


def test_no_permitted_cv_fails_closed_to_required_cv_missing(tmp_path: Path) -> None:
    settings, database, crypto = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        _opportunity(session, employer="Wellington Management")
        drw = _opportunity(session, employer="DRW", division="Asset Management")
        tailored = service.store_bytes(
            filename="wellington.docx",
            content=_cv_bytes("Wellington's collaborative investment culture."),
            kind="cv",
            tags=("wellington-management", "asset-management", "summer-cv"),
            approved=True,
        )
        application = _application(session, drw)

        package = ApplicationService(session, settings, crypto).prepare(application.id)

        assert package.ready is False
        assert package.cv_id is None
        assert package.reason_codes == ("required_cv_missing",)
        assert package.application.state == ApplicationState.NEEDS_USER.value
        assert package.application.selected_cv_id != tailored.id


def test_grad_2028_derivation_replaces_split_runs_and_preserves_coupling() -> None:
    derived = DocumentService.derive_grad_2028_cv_bytes(_yii_cv_bytes())
    text = DocumentService.extract_docx_body_text(derived)

    assert text is not None
    assert "Bachelor of Science: Finance" in text
    assert "Year in Industry" not in text
    assert "2029" not in text
    assert text.count("2028") == 1
    assert DocumentService.derive_graduation_tag(derived) == "grad-2028"


def test_blanket_variant_registration_preserves_source_row_and_file(
    tmp_path: Path,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        _opportunity(session, employer="DRW", division="Asset Management")
        source = service.store_bytes(
            filename="Demo_Candidate_Asset_Management.docx",
            content=_yii_cv_bytes(),
            kind="cv",
            tags=("asset-management", "investments", "yii-cv"),
            approved=True,
        )
        source_path = Path(source.path)
        source_bytes = source_path.read_bytes()
        row_count = session.scalar(select(func.count()).select_from(Document))
        file_count = len(tuple(settings.documents_dir.iterdir()))

        derived = service.create_grad_2028_blanket_variant(
            source,
            filename="Demo_Candidate_Asset_Management_2028.docx",
            actor="phase18-test",
        )

        assert session.scalar(select(func.count()).select_from(Document)) == row_count + 1
        assert len(tuple(settings.documents_dir.iterdir())) == file_count + 1
        assert source_path.is_file()
        assert source_path.read_bytes() == source_bytes
        assert Path(derived.path).is_file()
        tags = set(json.loads(derived.tags_json))
        assert tags == {"asset-management", "grad-2028", "investments", "summer-cv"}
        assert "Year in Industry" not in (
            DocumentService.extract_docx_body_text(Path(derived.path).read_bytes()) or ""
        )


def test_reselection_changes_competitor_cv_and_audits_without_deleting_documents(
    tmp_path: Path,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        _opportunity(session, employer="Wellington Management")
        drw = _opportunity(session, employer="DRW", division="Asset Management")
        unsafe = service.store_bytes(
            filename="wellington.docx",
            content=_cv_bytes("Wellington's collaborative investment culture."),
            kind="cv",
            tags=("wellington-management", "asset-management", "summer-cv"),
            approved=True,
        )
        safe = service.store_bytes(
            filename="generic.docx",
            content=_cv_bytes(),
            kind="cv",
            tags=("asset-management", "summer-cv"),
            approved=True,
        )
        application = _application(
            session,
            drw,
            state=ApplicationState.PACKAGE_PREPARED,
            selected_cv_id=unsafe.id,
        )
        rows_before = session.scalar(select(func.count()).select_from(Document))
        paths_before = {Path(row.path) for row in session.scalars(select(Document))}

        repair = getattr(application_services, "reselect_existing_cvs", None)
        assert repair is not None, "selected-CV repair service is missing"
        report = repair(session, settings, actor="phase18-test")

        assert report.scanned == 1
        assert report.changed == 1
        assert report.required_cv_missing == 0
        assert application.selected_cv_id == safe.id
        assert session.scalar(select(func.count()).select_from(Document)) == rows_before
        assert all(path.is_file() for path in paths_before)
        events = list(
            session.scalars(
                select(AuditOutbox).where(
                    AuditOutbox.event_type == "application.cv_reselected"
                )
            )
        )
        assert len(events) == 1
        details = json.loads(events[0].details_json)
        assert details["before_cv_id"] == unsafe.id
        assert details["after_cv_id"] == safe.id
        assert details["reason"] == "employer_exclusivity_reselection"


def test_reselection_without_permitted_cv_moves_to_needs_user_with_reason(
    tmp_path: Path,
) -> None:
    settings, database, _ = _runtime(tmp_path)
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        _opportunity(session, employer="Wellington Management")
        drw = _opportunity(session, employer="DRW", division="Asset Management")
        unsafe = service.store_bytes(
            filename="wellington.docx",
            content=_cv_bytes("Wellington's collaborative investment culture."),
            kind="cv",
            tags=("wellington-management", "asset-management", "summer-cv"),
            approved=True,
        )
        application = _application(
            session,
            drw,
            state=ApplicationState.PACKAGE_PREPARED,
            selected_cv_id=unsafe.id,
        )
        rows_before = session.scalar(select(func.count()).select_from(Document))
        paths_before = {Path(row.path) for row in session.scalars(select(Document))}

        repair = getattr(application_services, "reselect_existing_cvs", None)
        assert repair is not None, "selected-CV repair service is missing"
        report = repair(session, settings, actor="phase18-test")

        assert report.scanned == 1
        assert report.changed == 1
        assert report.required_cv_missing == 1
        assert application.selected_cv_id is None
        assert application.state == ApplicationState.NEEDS_USER.value
        assert application.next_action == (
            "Upload and approve an employer-compatible CV (required_cv_missing)"
        )
        assert session.scalar(select(func.count()).select_from(Document)) == rows_before
        assert all(path.is_file() for path in paths_before)
