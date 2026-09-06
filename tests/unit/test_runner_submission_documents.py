"""Final-click document revalidation must never silently drop a selection."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

import app.automation.runner as runner_module
from app.automation.runner import AutomationRunner, SubmissionBlocked
from app.models import Document
from app.scouting.programmes import resolve_programme_framing


class _Session:
    def __init__(self, document):
        self.document = document

    def get(self, _model, document_id):
        if self.document is None or str(self.document.id) != str(document_id):
            return None
        return self.document


def _runner(tmp_path):
    runner = object.__new__(AutomationRunner)
    runner.settings = SimpleNamespace(documents_dir=tmp_path)
    return runner


def _application(document_id="cv-1"):
    return SimpleNamespace(
        selected_cv_id=document_id,
        selected_cover_letter_id=None,
    )


def _document(path, *, approved=True, digest=None, tags=()):
    content = path.read_bytes()
    return Document(
        id="cv-1",
        name="cv.pdf",
        kind="document.cv",
        path=str(path),
        sha256=digest or hashlib.sha256(content).hexdigest(),
        approved=approved,
        tags_json=json.dumps(list(tags)),
    )


def test_selected_document_is_rederived_with_exact_hash(tmp_path):
    path = tmp_path / "cv.pdf"
    path.write_bytes(b"reviewed-cv")
    document = _document(path)

    manifest = _runner(tmp_path)._document_manifest(
        _Session(document),
        _application(),
    )

    assert manifest == {
        "document.cv": {
            "id": "cv-1",
            "kind": "document.cv",
            "sha256": hashlib.sha256(b"reviewed-cv").hexdigest(),
            "approved": True,
        }
    }


@pytest.mark.parametrize("failure", ["missing", "unapproved", "db_hash", "file_bytes"])
def test_selected_document_mutation_refuses_before_authority_consume(tmp_path, failure):
    path = tmp_path / "cv.pdf"
    path.write_bytes(b"reviewed-cv")
    document = _document(path)
    if failure == "missing":
        document = None
    elif failure == "unapproved":
        document.approved = False
    elif failure == "db_hash":
        document.sha256 = hashlib.sha256(b"different-database-hash").hexdigest()
    elif failure == "file_bytes":
        path.write_bytes(b"mutated-after-review")

    with pytest.raises(SubmissionBlocked) as exc:
        _runner(tmp_path)._document_manifest(
            _Session(document),
            _application(),
        )

    assert exc.value.code == "submission_document_changed"


@pytest.mark.parametrize(
    ("selected_tag", "programme_group"),
    [
        pytest.param("yii-cv", "summer", id="yii-cv-with-summer-framing"),
        pytest.param("summer-cv", "year_in_industry", id="summer-cv-with-yii-framing"),
    ],
)
def test_fill_inputs_refuse_mixed_cv_and_graduation_pairs(
    tmp_path, monkeypatch, selected_tag, programme_group
):
    path = tmp_path / "cv.pdf"
    path.write_bytes(b"reviewed-cv")
    document = _document(path, tags=(selected_tag,))
    framing = resolve_programme_framing(programme_group)
    assert framing is not None
    seen_framings = []

    class _ProfileService:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_automation_data(self, resolved_framing):
            seen_framings.append(resolved_framing)
            return {"education.graduation_year": resolved_framing.graduation_year}

    runner = _runner(tmp_path)
    runner.crypto = object()
    runner._approved_answers = lambda _session: ({}, [])
    monkeypatch.setattr(runner_module, "ProfileService", _ProfileService)

    with pytest.raises(SubmissionBlocked) as exc:
        runner._approved_inputs(
            _Session(document),
            _application(),
            framing,
        )

    assert exc.value.code == "programme_framing_mismatch"
    assert seen_framings == [framing]
    assert seen_framings[0] is framing
