from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_lab_exposes_js_apply_button_without_get_destination_and_bound_form(
    tmp_path: Path,
) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings)) as client:
        detail = client.get("/lab/resolution/js-apply")
        script = client.get("/lab/resolution/js-apply.js")
        form = client.get("/lab/resolution/js-application")

    assert detail.status_code == 200
    assert 'data-source-listing="true"' in detail.text
    assert 'data-employer="ARGUS Test Capital"' in detail.text
    assert 'data-role="Summer Analyst"' in detail.text
    assert '<button id="phase13-js-apply" type="button">Apply now</button>' in detail.text
    assert '<script src="/lab/resolution/js-apply.js"></script>' in detail.text
    assert 'href="/lab/resolution/js-application"' not in detail.text
    assert script.status_code == 200
    assert "phase13-js-apply\").addEventListener" in script.text
    assert 'window.location.assign("/lab/resolution/js-application")' in script.text

    assert form.status_code == 200
    assert 'data-ats="greenhouse"' in form.text
    assert 'data-employer="ARGUS Test Capital"' in form.text
    assert 'data-role="Summer Analyst"' in form.text
    assert 'data-requisition="ARGUS-PHASE13-001"' in form.text
    assert 'data-argus-form-identity="phase13-application"' in form.text
    assert '<form id="phase13-application"' in form.text
    assert '<button type="submit">Submit application</button>' in form.text
