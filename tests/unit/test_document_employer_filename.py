# RED: CV filename must not name a different employer.
# Tests for _filename_employer_keys and _cv_permitted_for_employer.
from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Document
from app.services.documents import DocumentService

# ── unit tests for _filename_employer_keys (static, no DB) ───────────────────

KNOWN_KEYS = frozenset({
    "jpmorgan", "goldmansachs", "bloomberg", "ubs", "blackrock",
    "carlyle", "apolloglobalmanagement",
})


def _check(filename: str) -> frozenset[str]:
    return DocumentService._filename_employer_keys(filename, KNOWN_KEYS)


def test_single_word_employer_in_filename():
    keys = _check("Demo_Candidate_JPMorgan_CV.docx")
    assert keys == {"jpmorgan"}


def test_multi_word_employer_in_filename():
    keys = _check("Demo_Candidate_Goldman_Sachs_CV.docx")
    assert keys == {"goldmansachs"}


def test_alias_resolved_in_filename():
    # "JP Morgan" aliases to jpmorgan
    keys = _check("Demo_Candidate_JP_Morgan_CV.docx")
    assert keys == {"jpmorgan"}


def test_no_employer_in_filename():
    keys = _check("Demo_Candidate_Generic_CV.docx")
    assert keys == frozenset()


def test_employer_with_underscores():
    keys = _check("Demo_Candidate_Bank_of_America_CV.docx")
    assert keys == frozenset()  # "Bank of America" is not a static employer key


def test_employer_at_start():
    keys = _check("JPMorgan_2028_CV.docx")
    assert keys == {"jpmorgan"}


def test_employer_at_end():
    keys = _check("CV_for_UBS_Final.docx")
    assert keys == {"ubs"}


# ── integration tests for _cv_permitted_for_employer with real DOCX ─────────

def _service_with_docs(tmp_path: Path) -> tuple[DocumentService, Document]:
    from unittest.mock import MagicMock, Mock
    from app.db import Database
    from app.config import Settings
    from tests.document_helpers import cv_docx_bytes

    settings = Settings.load({
        "ARGUS_DATA_DIR": str(tmp_path / "data"),
        "ARGUS_API_TOKEN": "t",
    })
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        doc = service.store_bytes(
            filename="Demo_Candidate_JPMorgan_CV.docx",
            content=cv_docx_bytes(2028, "Finance CV"),
            kind="cv",
            tags=("jpmorgan",),
            approved=True,
        )
    # Create service outside the session scope so caches are fresh
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        doc = session.merge(doc)
        return service, doc


def test_filename_refused_for_wrong_employer(tmp_path):
    """Filename names JPMorgan; must be refused for Bloomberg."""
    service, doc = _service_with_docs(tmp_path)
    assert service._cv_permitted_for_employer(doc, "bloomberg") is False


def test_filename_permitted_for_correct_employer(tmp_path):
    """Same CV permitted for JPMorgan."""
    service, doc = _service_with_docs(tmp_path)
    assert service._cv_permitted_for_employer(doc, "jpmorgan") is True


def test_generic_filename_unaffected(tmp_path):
    """A filename naming no known employer passes."""
    from app.config import Settings
    from app.db import Database
    from tests.document_helpers import cv_docx_bytes

    settings = Settings.load({
        "ARGUS_DATA_DIR": str(tmp_path / "data2"),
        "ARGUS_API_TOKEN": "t",
    })
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        service.store_bytes(
            filename="Demo_Candidate_Generic_CV.docx",
            content=cv_docx_bytes(2028, "Generic"),
            kind="cv",
            tags=("bloomberg",),
            approved=True,
        )
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        doc = session.scalars(
            __import__("sqlalchemy").select(Document)
        ).first()
        assert doc is not None
        assert service._cv_permitted_for_employer(doc, "bloomberg") is True


def test_filename_select_approved_refuses_conflict(tmp_path):
    """select_approved returns None when filename conflicts with employer."""
    from app.config import Settings
    from app.db import Database
    from tests.document_helpers import cv_docx_bytes

    settings = Settings.load({
        "ARGUS_DATA_DIR": str(tmp_path / "data3"),
        "ARGUS_API_TOKEN": "t",
    })
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        service.store_bytes(
            filename="Demo_Candidate_JPMorgan_CV.docx",
            content=cv_docx_bytes(2028, "Finance"),
            kind="cv",
            tags=("jpmorgan",),
            approved=True,
        )
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        # Request CV for Bloomberg — filename names JPMorgan, not Bloomberg
        result = service.select_approved("cv", employer="bloomberg")
        assert result is None, "should return None when filename conflicts"


def test_existing_tag_and_body_rules_still_work(tmp_path):
    """Regression: tag-based and body-text employer rules unchanged."""
    from app.config import Settings
    from app.db import Database
    from tests.document_helpers import cv_docx_bytes

    settings = Settings.load({
        "ARGUS_DATA_DIR": str(tmp_path / "data4"),
        "ARGUS_API_TOKEN": "t",
    })
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        service.store_bytes(
            filename="Generic_CV.docx",
            content=cv_docx_bytes(2028, "Finance JPMorgan experience"),
            kind="cv",
            tags=("jpmorgan",),
            approved=True,
        )
    with database.session_scope() as session:
        service = DocumentService(session, settings.documents_dir)
        # Body mentions JPMorgan, but filename doesn't — should be permitted
        doc = session.scalars(
            __import__("sqlalchemy").select(Document)
        ).first()
        assert doc is not None
        # Body mentions JPMorgan, employer is jpmorgan → permitted
        assert service._cv_permitted_for_employer(doc, "jpmorgan") is True
        # Body mentions JPMorgan, employer is bloomberg → blocked by body check
        assert service._cv_permitted_for_employer(doc, "bloomberg") is False