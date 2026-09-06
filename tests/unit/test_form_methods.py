# TDD: every data-* form in the UI must carry a valid HTML method attribute
# (or a data-method override).  Background: the Profile form used
# method="PUT" — invalid for HTML forms, normalised to "get" by browsers —
# which made argus.js send a JSON body with a GET and throw
# "Request with GET/HEAD method cannot have body".
from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"
VALID_METHODS = {"get", "post"}

FORM_TAG = re.compile(r"<form[^>]*>", re.IGNORECASE)
DATA_FORM = re.compile(r"data-(api|upload)-form", re.IGNORECASE)
METHOD_ATTR = re.compile(r'method\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def _iter_template_files():
    yield from TEMPLATES.rglob("*.html")


def test_every_data_form_has_valid_method_or_data_method():
    offenders: list[str] = []
    for path in _iter_template_files():
        content = path.read_text(encoding="utf-8", errors="replace")
        for match in FORM_TAG.finditer(content):
            tag = match.group(0)
            if not DATA_FORM.search(tag):
                continue
            explicit = METHOD_ATTR.search(tag)
            has_override = "data-method" in tag
            if explicit and explicit.group(1).strip().lower() in VALID_METHODS:
                continue
            if has_override:
                continue  # JS handler reads data-method verbatim (e.g. PUT)
            offenders.append(f"{path.name}: {tag[:120]}")
    assert not offenders, (
        "data-api-form/data-upload-form without a valid method attribute "
        f"(browsers normalise invalid methods to GET): {offenders}"
    )


def test_argus_js_reads_raw_method_attribute():
    js = (
        Path(__file__).resolve().parents[2] / "app" / "static" / "js" / "argus.js"
    ).read_text(encoding="utf-8")
    # The api-form handler must consult getAttribute('method')/dataset.method
    # rather than only form.method (which is normalised to 'get').
    assert "getAttribute('method')" in js or 'getAttribute("method")' in js


def test_profile_form_uses_post_with_put_override():
    profile = (TEMPLATES / "pages" / "profile.html").read_text(encoding="utf-8")
    assert 'method="post"' in profile.lower()
    assert "PUT" in profile.upper()  # via data-method
