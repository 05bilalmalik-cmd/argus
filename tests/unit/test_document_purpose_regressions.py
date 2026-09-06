"""Synthetic browser regressions for document purpose, never real PII."""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from app.automation.adapters.generic import _INSPECT_SCRIPT
from app.automation.classifier import DeterministicClassifier
from app.domain.questions import CanonicalKey, FormQuestion


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())
        yield page
        browser.close()


def inspect_uploads(page, content):
    page.set_content(
        '<form action="/apply/REQ-1" data-role="Summer Analyst">'
        '<input name="first_name"><input name="email" type="email">'
        + content + '<button type="submit">Submit application</button></form>'
    )
    result = page.evaluate(_INSPECT_SCRIPT)
    return {
        row["name"]: DeterministicClassifier().classify(FormQuestion(
            label=row["label"], name=row["name"], field_type="file",
        )).canonical_key
        for row in result["fields"] if row["control_type"] == "file"
    }


def test_unrelated_nested_heading_cannot_override_explicit_document_label(page):
    mappings = inspect_uploads(page, '''
      <section><aside><h3>Resume tips</h3></aside>
      <div><label for="a">Reference letter</label><input id="a" name="document_1" type="file"></div></section>
    ''')
    assert mappings == {"document_1": CanonicalKey.UNKNOWN}


def test_three_attach_controls_keep_their_document_purpose(page):
    mappings = inspect_uploads(page, '''
      <section><h3>Resume/CV</h3><div><label for="a">Attach</label><input id="a" name="upload_1" type="file"></div></section>
      <section><h3>Cover Letter</h3><div><label for="b">Attach</label><input id="b" name="upload_2" type="file"></div></section>
      <section><h3>Most recent transcript</h3><div><label for="c">Attach</label><input id="c" name="upload_3" type="file"></div></section>
    ''')
    assert mappings == {
        "upload_1": CanonicalKey.CV,
        "upload_2": CanonicalKey.COVER_LETTER,
        "upload_3": CanonicalKey.UNKNOWN,
    }


@pytest.mark.parametrize("heading", ["h3", "legend", 'div role="heading"', 'div class="file-upload__label"'])
def test_local_document_heading_is_preserved(page, heading):
    closing = heading.split()[0]
    mappings = inspect_uploads(page, f'''
      <fieldset><{heading}>Cover Letter</{closing}>
      <div><label for="a">Attach</label><input id="a" name="upload_1" type="file"></div></fieldset>
    ''')
    assert mappings == {"upload_1": CanonicalKey.COVER_LETTER}


@pytest.mark.parametrize("markup", [
    '<h3>Resume/CV</h3><div><label for="a">Attach</label><input id="a" name="upload_1" type="file"></div>',
    '<section><h3>Resume/CV</h3><input name="upload_1" type="file"><input name="upload_2" type="file"></section>',
    '<section><h3 style="display:none">Resume/CV</h3><label for="a">Attach</label><input id="a" name="upload_1" type="file"></section>',
])
def test_ambiguous_or_unrelated_heading_never_authorises_cv(page, markup):
    mappings = inspect_uploads(page, markup)
    assert mappings
    assert set(mappings.values()) == {CanonicalKey.UNKNOWN}


@pytest.mark.parametrize(("label", "name", "expected"), [
    ("Attach", "resume", CanonicalKey.CV),
    ("Attach", "upload_1", CanonicalKey.UNKNOWN),
    ("Upload most recent transcript", "resume", CanonicalKey.UNKNOWN),
    ("Cover Letter", "resume", CanonicalKey.UNKNOWN),
    ("Upload certificate", "file", CanonicalKey.UNKNOWN),
    ("Attach", "cover_letter", CanonicalKey.COVER_LETTER),
])
def test_file_classification_requires_unambiguous_purpose(label, name, expected):
    mapping = DeterministicClassifier().classify(FormQuestion(label, "file", name=name))
    assert mapping.canonical_key is expected
