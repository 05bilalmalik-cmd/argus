from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_javascript_never_injects_api_messages_as_html() -> None:
    source = (ROOT / "app/static/js/argus.js").read_text(encoding="utf-8")

    assert "innerHTML" not in source
    assert "messageNode.textContent = message" in source


def test_ui_does_not_overstate_full_vault_encryption() -> None:
    base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "app/templates/pages/dashboard.html").read_text(encoding="utf-8")
    combined = f"{base}\n{dashboard}".casefold()

    assert "encrypted vault" not in combined
    assert "· encrypted" not in combined
    assert "protected vault" in combined


def test_mutating_ui_actions_are_confirmed_and_handoff_is_identity_bound() -> None:
    source = (ROOT / "app/static/js/argus.js").read_text(encoding="utf-8")
    applications = (ROOT / "app/templates/pages/applications.html").read_text(
        encoding="utf-8"
    )
    detail = (ROOT / "app/templates/pages/application_detail.html").read_text(
        encoding="utf-8"
    )

    assert "window.confirm(confirmation)" in source
    assert "blocked_reasons" in source
    assert "confirmation_required" in source
    assert "body: JSON.stringify({application_id: applicationId})" in source
    assert 'data-confirm="Queue this exact application for review?"' in applications
    assert 'data-confirm="Run a dry-run for this exact application?"' in detail


def test_source_resolution_handoff_is_persisted_and_polled_without_a_url() -> None:
    source = (ROOT / "app/static/js/argus.js").read_text(encoding="utf-8")

    assert "bindTargetResolutionHandoff" in source
    assert "pollTargetResolution" in source
    assert "sessionStorage.setItem" in source
    assert "body: JSON.stringify({application_id: applicationId, confirmed: true})" in source
    assert "location.reload()" in source
    assert "sourceResolution" in source
    assert "const flowEpoch = epoch ?? beginNavigatorFlow(panel, applicationId, sessionId);" in source
    assert "if (binding.sourceResolution) {" in source
    assert "pollTargetResolution(panel, panel.dataset.applicationId || '', binding.sessionId, null, 0, epoch);" in source
