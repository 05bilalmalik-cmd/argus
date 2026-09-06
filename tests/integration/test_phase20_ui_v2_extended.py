from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import AutomationMode, Settings
from app.main import create_app
from app.models import Application, Opportunity


def _app(tmp_path: Path, env: dict[str, str] | None = None) -> TestClient:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path), **(env or {})})
    return TestClient(create_app(settings))


def _seed(
    client: TestClient,
    *,
    employer: str,
    role: str,
    programme: str,
    state: str = "UNKNOWN",
    closing: date | None = None,
    application_state: str | None = None,
) -> tuple[str, str | None]:
    with client.app.state.db.session_scope() as session:
        opportunity = Opportunity(
            employer=employer,
            role_title=role,
            programme_group=programme,
            location="London",
            cycle="2027",
            url=f"https://example.test/{employer.casefold().replace(' ', '-')}",
            source="phase20-test",
            deadline=closing,
        )
        if hasattr(opportunity, "application_window_status"):
            opportunity.application_window_status = state
        session.add(opportunity)
        session.flush()
        application_id = None
        if application_state:
            application = Application(
                opportunity_id=opportunity.id,
                state=application_state,
                priority=60,
                risk_level=1,
                eligibility_json='{"eligible": true, "reason_codes": []}',
                conflict_json='{"blocked": false, "reason_codes": []}',
            )
            session.add(application)
            session.flush()
            application_id = application.id
        return opportunity.id, application_id


def test_environment_flag_defaults_off_and_enables_v2(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ARGUS_UI_V2", raising=False)
    with _app(tmp_path / "off") as client:
        assert 'data-ui-version="2"' not in client.get("/").text

    monkeypatch.setenv("ARGUS_UI_V2", "true")
    with _app(tmp_path / "on") as client:
        assert 'data-ui-version="2"' in client.get("/").text


def test_v2_shell_has_exactly_seven_primary_destinations_and_status_everywhere(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    expected = ["Today", "Calendar", "Pipeline", "Needs You", "Library", "Activity", "Control"]
    with _app(tmp_path) as client:
        root = client.get("/").text
        primary = root.split('aria-label="Primary navigation"', 1)[1].split("</nav>", 1)[0]
        labels = re.findall(r'<span class="nav-label">([^<]+)</span>', primary)
        assert labels == expected
        for path in ("/", "/calendar", "/pipeline", "/needs-you", "/library", "/activity", "/control"):
            response = client.get(path)
            assert response.status_code == 200
            assert 'data-mode-status' in response.text
            assert "will not submit" in response.text


def test_calendar_ships_all_views_and_filters_employer_and_programme(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    closing = date.today() + timedelta(days=3)
    with _app(tmp_path) as client:
        _seed(
            client,
            employer="Filter Capital",
            role="Spring Insight",
            programme="spring_week",
            state="OPEN",
            closing=closing,
        )
        _seed(
            client,
            employer="Other Capital",
            role="Summer Analyst",
            programme="summer",
            state="OPEN",
            closing=closing,
        )
        response = client.get(
            "/calendar?view=closing&programme=spring_week&employer=Filter%20Capital"
        )

    assert response.status_code == 200
    for label in ("Agenda", "Month", "Opens soon", "Closing soon"):
        assert label in response.text
    assert "Filter Capital" in response.text
    assert "Other Capital" not in response.text
    assert "No date captured" in response.text


def test_pipeline_combines_opportunities_and_applications_with_filters(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    with _app(tmp_path) as client:
        opportunity_id, application_id = _seed(
            client,
            employer="Unified Firm",
            role="Placement Analyst",
            programme="year_in_industry",
            application_state="ELIGIBILITY_CHECKED",
        )
        _seed(
            client,
            employer="Hidden Firm",
            role="Summer Analyst",
            programme="summer",
        )
        response = client.get(
            "/pipeline?q=unified&programme=year_in_industry&application_state=ELIGIBILITY_CHECKED"
        )

    assert response.status_code == 200
    assert "Unified Firm" in response.text
    assert "Hidden Firm" not in response.text
    assert opportunity_id in response.text
    assert application_id in response.text
    assert "1 matching role" in response.text


def test_safe_mode_changes_are_server_held_and_armed_requires_exact_phrase(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    with _app(tmp_path, {"ARGUS_ENABLE_LIVE_SUBMIT": "true"}) as client:
        off = client.post(
            "/control/mode",
            json={"mode": "OFF", "confirmation": "", "dry_run_default": False},
        )
        assert off.status_code == 200
        assert client.app.state.settings.automation_mode is AutomationMode.OFF
        assert client.app.state.ui_v2_runtime["dry_run_default"] is False

        refused = client.post(
            "/control/mode",
            json={"mode": "ARMED", "confirmation": "arm argus", "dry_run_default": True},
        )
        assert refused.status_code == 409
        assert refused.json()["code"] == "typed_confirmation_required"

        armed = client.post(
            "/control/mode",
            json={"mode": "ARMED", "confirmation": "ARM ARGUS", "dry_run_default": True},
        )
        assert armed.status_code == 200
        assert client.app.state.settings.automation_mode is AutomationMode.ARMED
        assert armed.json()["submitted"] is False


def test_bulk_archive_unarchive_and_requeue_are_exact_count_and_reversible(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    with _app(tmp_path) as client:
        opportunity_id, application_id = _seed(
            client,
            employer="Bulk Firm",
            role="Bulk Role",
            programme="summer",
            application_state="ELIGIBILITY_CHECKED",
        )
        mismatch = client.post(
            "/pipeline/bulk",
            json={
                "action": "archive",
                "opportunity_ids": [opportunity_id],
                "expected_count": 2,
                "confirmed": True,
            },
        )
        assert mismatch.status_code == 409

        archived = client.post(
            "/pipeline/bulk",
            json={
                "action": "archive",
                "opportunity_ids": [opportunity_id],
                "expected_count": 1,
                "confirmed": True,
            },
        )
        assert archived.status_code == 200
        assert archived.json()["affected"] == 1
        assert archived.json()["submitted"] is False
        with client.app.state.db.session_scope() as session:
            assert session.get(Opportunity, opportunity_id).is_archived is True

        unarchived = client.post(
            "/pipeline/bulk",
            json={
                "action": "unarchive",
                "opportunity_ids": [opportunity_id],
                "expected_count": 1,
                "confirmed": True,
            },
        )
        assert unarchived.status_code == 200

        requeued = client.post(
            "/pipeline/bulk",
            json={
                "action": "requeue",
                "opportunity_ids": [opportunity_id],
                "expected_count": 1,
                "confirmed": True,
            },
        )
        assert requeued.status_code == 200
        with client.app.state.db.session_scope() as session:
            assert session.get(Application, application_id).state == "QUEUED"


def test_v2_application_detail_preserves_evidence_and_non_submitting_actions(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    with _app(tmp_path) as client:
        _opportunity_id, application_id = _seed(
            client,
            employer="Detail Firm",
            role="Evidence Analyst",
            programme="summer",
            application_state="ELIGIBILITY_CHECKED",
        )
        response = client.get(f"/applications/{application_id}")

    assert response.status_code == 200
    assert "Detail Firm" in response.text
    assert "Evidence Analyst" in response.text
    assert "ELIGIBILITY CHECKED" in response.text
    assert "Queue for review" in response.text
    assert "Submit application" not in response.text


def test_pipeline_role_detail_is_a_real_deep_link_and_keeps_undated_truth(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    with _app(tmp_path) as client:
        opportunity_id, _application_id = _seed(
            client,
            employer="Deep Link Firm",
            role="Undated Evidence Role",
            programme="spring_week",
        )
        response = client.get(f"/pipeline/{opportunity_id}")
        missing = client.get("/pipeline/not-a-real-id")

    assert response.status_code == 200
    assert "Deep Link Firm" in response.text
    assert "Undated Evidence Role" in response.text
    assert response.text.count("No date captured") >= 2
    assert "Submission authority remains enforced by the server" in response.text
    assert missing.status_code == 404


def test_needs_you_legacy_deep_link_keeps_the_exact_application(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    with _app(tmp_path) as client:
        _opportunity_id, application_id = _seed(
            client,
            employer="Exact Link Firm",
            role="Exact Link Role",
            programme="summer",
            application_state="NEEDS_USER",
        )
        response = client.get(
            f"/needs-you/{application_id}", follow_redirects=False
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/applications/{application_id}"


def test_v2_subordinate_tool_links_open_scout_and_lab_without_redirect_loops(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    with _app(tmp_path) as client:
        pipeline = client.get("/pipeline")
        control = client.get("/control")
        scout = client.get("/scout?legacy=1", follow_redirects=False)
        lab = client.get("/lab?legacy=1", follow_redirects=False)

    assert 'href="/scout?legacy=1"' in pipeline.text
    assert 'href="/lab?legacy=1"' in control.text
    assert scout.status_code == 200
    assert lab.status_code == 200


def test_closing_soon_never_falls_back_to_closed_roles(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    closing = date.today() + timedelta(days=3)
    with _app(tmp_path) as client:
        _seed(
            client,
            employer="Closed Evidence Firm",
            role="Closed Evidence Role",
            programme="summer",
            state="CLOSED",
            closing=closing,
        )
        response = client.get("/calendar?view=closing")

    agenda = response.text.split('<section class="agenda-view">', 1)[1].split(
        "</section>", 1
    )[0]
    assert "Closed Evidence Firm" not in agenda
    assert "No evidenced dates match this view" in agenda


def test_pipeline_paginates_the_complete_filtered_inventory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    with _app(tmp_path) as client:
        for employer in ("Paged A", "Paged B", "Paged C"):
            _seed(
                client,
                employer=employer,
                role="Pagination Role",
                programme="summer",
            )
        response = client.get("/pipeline?page=2&per_page=1")

    results = response.text.split(
        '<section class="pipeline-list" aria-label="Pipeline results">', 1
    )[1].split("</section>", 1)[0]
    assert "Paged B" in results
    assert "Paged A" not in results
    assert "Paged C" not in results
    assert "Page 2 of 3" in response.text
    assert "page=1" in response.text
    assert "page=3" in response.text
    assert "per_page=1" in response.text


def test_pipeline_filters_keep_search_and_applications_only_stage(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    with _app(tmp_path) as client:
        _seed(
            client,
            employer="Needle Firm",
            role="Needle Role",
            programme="summer",
            application_state="ELIGIBILITY_CHECKED",
        )
        response = client.get("/pipeline?stage=applications&q=needle")

    assert 'data-global-search name="q" type="search" value="needle"' in response.text
    assert '<input type="hidden" name="stage" value="applications">' in response.text


def test_calendar_paginates_every_undated_role_without_hiding_the_inventory(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARGUS_UI_V2", "true")
    with _app(tmp_path) as client:
        for employer in ("Undated A", "Undated B", "Undated C"):
            _seed(
                client,
                employer=employer,
                role="No Captured Date",
                programme="summer",
            )
        response = client.get(
            "/calendar?view=agenda&undated_page=2&undated_per_page=1"
        )

    lane = response.text.split('<section class="undated-lane">', 1)[1].split(
        "</section>", 1
    )[0]
    assert "Undated B" in lane
    assert "Undated A" not in lane
    assert "Undated C" not in lane
    assert "3 roles without captured dates" in lane
    assert "Page 2 of 3" in lane
    assert "undated_page=1" in lane
    assert "undated_page=3" in lane
