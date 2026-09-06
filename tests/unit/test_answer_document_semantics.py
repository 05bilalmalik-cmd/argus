# TDD: R12 — answer and document semantics.
# CV selection needs positive tag evidence; referral source must not map to
# motivation; broad "school"/"start date"/"end date" matches must not be
# silently forced into a single education record.
from __future__ import annotations

import pytest

from app.domain.questions import FormQuestion
from app.automation.classifier import DeterministicClassifier


def test_referral_source_is_not_motivation():
    classifier = DeterministicClassifier()
    mapping = classifier.classify(
        FormQuestion(
            label="How did you hear about us?",
            field_type="select",
            options=("LinkedIn", "Careers site", "Referral"),
        )
    )
    # Marketing metadata is factual; auto-filling it from the motivation
    # answer bank would misrepresent the candidate.
    assert mapping.canonical_key.value != "answer.motivation"


def test_bare_school_label_is_ambiguous_not_university():
    classifier = DeterministicClassifier()
    mapping = classifier.classify(FormQuestion(label="School", field_type="text"))
    # Multiple education records make a bare "school" match ambiguous; it
    # must not silently become THE university answer.
    assert mapping.canonical_key.value != "education.university" or (
        mapping.confidence < 0.75
    )


def test_explicit_university_still_maps():
    classifier = DeterministicClassifier()
    mapping = classifier.classify(
        FormQuestion(label="University / institution", field_type="text")
    )
    assert mapping.canonical_key.value == "education.university"


def test_start_date_alone_is_ambiguous():
    classifier = DeterministicClassifier()
    mapping = classifier.classify(FormQuestion(label="Start date", field_type="month"))
    assert mapping.canonical_key.value != "education.graduation_year"


def test_document_selection_requires_positive_tag_evidence(tmp_path):
    from app.db import Database
    from app.config import Settings
    from app.services.documents import DocumentService

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path / "d"), "ARGUS_API_TOKEN": "t"})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        service.store_bytes(
            filename="unrelated.pdf",
            content=b"%PDF-1.4 unrelated",
            kind="cv",
            tags=(),
            approved=True,
        )
        # No desired tags overlap at all -> selection must refuse rather than
        # silently pick the unrelated document.
        assert service.select_approved("cv", ("london",)) is None


def test_document_selection_with_overlap_still_works(tmp_path):
    from app.db import Database
    from app.config import Settings
    from app.services.documents import DocumentService

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path / "d"), "ARGUS_API_TOKEN": "t"})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        service.store_bytes(
            filename="london-cv.pdf",
            content=b"%PDF-1.4 london",
            kind="cv",
            tags=("london",),
            approved=True,
        )
        assert service.select_approved("cv", ("london",)) is not None


def test_no_tags_requested_returns_none_when_only_tagged_docs(tmp_path):
    from app.db import Database
    from app.config import Settings
    from app.services.documents import DocumentService

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path / "d"), "ARGUS_API_TOKEN": "t"})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        service.store_bytes(
            filename="tagged.pdf",
            content=b"%PDF-1.4 tagged",
            kind="cv",
            tags=("spring",),
            approved=True,
        )
        assert service.select_approved("cv", ()) is None
