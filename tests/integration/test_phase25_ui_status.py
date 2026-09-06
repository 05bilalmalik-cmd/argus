from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.states import ApplicationState, UserApplicationStatus
from app.main import create_app
from app.models import Application, Opportunity


def _client(tmp_path: Path) -> TestClient:
    app = create_app(
        Settings.load(
            {
                "ARGUS_DATA_DIR": str(tmp_path),
                "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
                "ARGUS_ENABLE_NOTIFICATIONS": "false",
            }
        )
    )
    app.state.ui_v2_enabled = True
    return TestClient(app)


def _seed(
    client: TestClient,
    *,
    opportunity_id: str,
    employer: str,
    status: UserApplicationStatus,
    with_application: bool = False,
) -> str | None:
    with client.app.state.db.session_scope() as session:
        opportunity = Opportunity(
            id=opportunity_id,
            employer=employer,
            role_title="Summer Analyst",
            programme_group="summer",
            cycle="2027",
            location="London",
            url=f"https://example.test/{opportunity_id}",
            application_url=f"https://example.test/{opportunity_id}/apply",
            source="test",
            application_window_status="OPEN",
            user_status=status.value,
            user_status_actor="import",
        )
        session.add(opportunity)
        if not with_application:
            return None
        application = Application(
            id=f"application-{opportunity_id}",
            opportunity=opportunity,
            state=ApplicationState.PACKAGE_PREPARED.value,
            eligibility_json=json.dumps({"eligible": True, "reason_codes": []}),
            conflict_json=json.dumps({"blocked": False, "reason_codes": []}),
        )
        session.add(application)
        return application.id


def test_pipeline_status_is_visible_editable_filterable_and_post_application_rows_remain(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        application_id = _seed(
            client,
            opportunity_id="post-application-role",
            employer="Post Application Firm",
            status=UserApplicationStatus.HIREVUE,
            with_application=True,
        )
        _seed(
            client,
            opportunity_id="interested-role",
            employer="Interested Firm",
            status=UserApplicationStatus.INTERESTED,
        )

        pipeline = client.get("/pipeline")
        filtered_pipeline = client.get("/pipeline?user_status=HIREVUE")
        filtered_calendar = client.get("/calendar?user_status=HIREVUE")
        role_detail = client.get("/pipeline/post-application-role")
        application_detail = client.get(f"/applications/{application_id}")

    assert pipeline.status_code == 200
    assert "Post Application Firm" in pipeline.text
    assert "Interested Firm" in pipeline.text
    assert 'data-user-status-select' in pipeline.text
    for status in UserApplicationStatus:
        assert f'value="{status.value}"' in pipeline.text

    assert filtered_pipeline.status_code == 200
    assert 'name="user_status"' in filtered_pipeline.text
    assert 'value="HIREVUE" selected' in filtered_pipeline.text
    assert "Post Application Firm" in filtered_pipeline.text
    assert "Interested Firm" not in filtered_pipeline.text
    assert "User status HIREVUE excludes this opportunity from automation" in filtered_pipeline.text

    assert filtered_calendar.status_code == 200
    assert 'name="user_status"' in filtered_calendar.text
    assert 'value="HIREVUE" selected' in filtered_calendar.text
    assert "Post Application Firm" in filtered_calendar.text
    assert "Interested Firm" not in filtered_calendar.text

    for detail in (role_detail, application_detail):
        assert detail.status_code == 200
        assert 'data-user-status-select' in detail.text
        assert 'value="HIREVUE" selected' in detail.text
        assert "User status HIREVUE excludes this opportunity from automation" in detail.text


def test_today_has_exact_status_breakdown_and_visible_active_process_lane(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _seed(
            client,
            opportunity_id="assessment-role",
            employer="Assessment Firm",
            status=UserApplicationStatus.ONLINE_ASSESSMENT,
            with_application=True,
        )
        _seed(
            client,
            opportunity_id="not-applied-role",
            employer="Not Applied Firm",
            status=UserApplicationStatus.NOT_APPLIED,
        )
        response = client.get("/")

    assert response.status_code == 200
    assert 'aria-label="Candidate-owned status breakdown"' in response.text
    for status in UserApplicationStatus:
        assert f'data-user-status="{status.value}"' in response.text
    assert "Active application process" in response.text
    assert "Assessment Firm" in response.text
    assert "ONLINE ASSESSMENT" in response.text


def test_status_editor_uses_audited_api_without_page_reload() -> None:
    script = Path("app/static/js/argus-v2.js").read_text(encoding="utf-8")

    assert "[data-user-status-select]" in script
    assert "/user-status`" in script
    assert "method: 'PUT'" in script
    assert "data-user-status-reason" in script
    assert "statusSelect.addEventListener('change'" in script
    handler_start = script.index("statusSelect.addEventListener('change'")
    handler_end = script.index("\n    });\n  });", handler_start)
    status_handler = script[handler_start:handler_end]
    assert "location.reload" not in status_handler
    assert "location.assign" not in status_handler
