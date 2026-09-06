from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Application, Opportunity
from app.routers import pages


LEGACY_REDIRECTS = {
    "/opportunities": "/pipeline",
    "/applications": "/pipeline?stage=applications",
    "/profile": "/library?tab=profile",
    "/answers": "/library?tab=answers",
    "/documents": "/library?tab=documents",
    "/mail": "/activity?tab=mail",
    "/audit": "/activity?tab=audit",
    "/settings": "/control",
    "/scout": "/pipeline",
    "/lab": "/control?tab=lab",
}


def _client(tmp_path: Path, env: dict[str, str] | None = None, *, v2: bool = True) -> TestClient:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path), **(env or {})})
    app = create_app(settings)
    app.state.ui_v2_enabled = v2
    return TestClient(app)


def _seed_opportunity(
    client: TestClient,
    *,
    employer: str,
    role: str,
    programme: str = "summer",
    window_state: str = "UNKNOWN",
    opening_date: date | None = None,
    closing_date: date | None = None,
    application_state: str | None = None,
    next_action: str = "",
    target_status: str = "UNRESOLVED",
) -> tuple[str, str | None]:
    with client.app.state.db.session_scope() as session:
        opportunity = Opportunity(
            employer=employer,
            role_title=role,
            programme_group=programme,
            cycle="2027",
            location="London",
            url=f"https://example.test/{employer.casefold().replace(' ', '-')}",
            source="test",
            deadline=closing_date,
            target_status=target_status,
        )
        if hasattr(opportunity, "application_window_status"):
            opportunity.application_window_status = window_state
        if hasattr(opportunity, "opening_date"):
            opportunity.opening_date = opening_date
        session.add(opportunity)
        session.flush()
        application_id = None
        if application_state:
            application = Application(
                opportunity_id=opportunity.id,
                state=application_state,
                priority=50,
                risk_level=1,
                next_action=next_action,
                eligibility_json=json.dumps({"eligible": True, "reason_codes": []}),
                conflict_json=json.dumps({"blocked": False, "reason_codes": []}),
            )
            session.add(application)
            session.flush()
            application_id = application.id
        return opportunity.id, application_id


def test_flag_off_preserves_v1_rendering_contract(tmp_path: Path) -> None:
    with _client(tmp_path, v2=False) as client:
        first = client.get("/")
        client.app.state.ui_v2_enabled = False
        second = client.get("/")

    assert first.status_code == 200
    assert first.content == second.content
    assert b"Command Centre" in first.content
    assert b'/static/css/argus.css' in first.content
    assert b'data-ui-version="2"' not in first.content

    with _client(tmp_path / "new-routes-off", v2=False) as client:
        for path in ("/today", "/calendar", "/pipeline", "/library", "/activity", "/control"):
            assert client.get(path, follow_redirects=False).status_code == 404


def test_unset_flag_and_explicit_false_are_byte_identical_for_every_legacy_page(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ARGUS_UI_V2", raising=False)
    app = create_app(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
    paths = (
        "/",
        "/opportunities",
        "/applications",
        "/needs-you",
        "/profile",
        "/answers",
        "/documents",
        "/mail",
        "/audit",
        "/settings",
        "/scout",
        "/lab",
    )
    with TestClient(app) as client:
        baseline = {path: client.get(path, follow_redirects=False) for path in paths}
        app.state.ui_v2_enabled = False
        explicit_false = {path: client.get(path, follow_redirects=False) for path in paths}

    for path in paths:
        assert explicit_false[path].status_code == baseline[path].status_code, path
        assert explicit_false[path].content == baseline[path].content, path


def test_v2_legacy_routes_redirect_without_breaking_bookmarks(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        for path, expected in LEGACY_REDIRECTS.items():
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 307, path
            assert response.headers["location"] == expected, path

        _, application_id = _seed_opportunity(
            client,
            employer="Bookmark Firm",
            role="Bookmark Placement",
            application_state="NEEDS_USER",
        )
        assert application_id is not None
        needs_existing = client.get(
            f"/needs-you/{application_id}", follow_redirects=False
        )
        assert needs_existing.status_code == 303
        assert needs_existing.headers["location"] == f"/applications/{application_id}"
        detail_existing = client.get(needs_existing.headers["location"])
        assert detail_existing.status_code == 200
        assert "Bookmark Firm" in detail_existing.text

        detail_missing = client.get("/applications/not-a-real-id", follow_redirects=False)
        needs_missing = client.get("/needs-you/not-a-real-id", follow_redirects=False)

    assert detail_missing.status_code == 404
    # Notification bookmarks resolve only applications that still exist.
    assert needs_missing.status_code == 404
    assert needs_missing.json() == {"detail": "Application not found"}
    assert "location" not in needs_missing.headers


def test_calendar_keeps_zero_date_inventory_in_an_honest_lane(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        opportunity_id, _ = _seed_opportunity(
            client,
            employer="Undated Capital",
            role="Undated Summer Analyst",
            window_state="UNKNOWN",
        )

        response = client.get("/calendar")

    assert response.status_code == 200
    assert "No date captured" in response.text
    assert "Undated Capital" in response.text
    assert "Undated Summer Analyst" in response.text
    assert f'href="/pipeline/{opportunity_id}"' in response.text
    assert "0 dated" in response.text
    assert "date inferred" not in response.text.casefold()


def test_window_state_filter_returns_only_the_requested_rows(tmp_path: Path) -> None:
    assert callable(getattr(pages, "_window_state_v2", None))
    assert pages._window_state_v2(SimpleNamespace(application_window_status="OPEN")) == "OPEN"
    assert (
        pages._window_state_v2(SimpleNamespace(application_window_status="NOT_YET_OPEN"))
        == "NOT_YET_OPEN"
    )
    assert pages._window_state_v2(SimpleNamespace(application_window_status="garbage")) == "UNKNOWN"

    with _client(tmp_path) as client:
        _seed_opportunity(client, employer="Open Firm", role="Open Role", window_state="OPEN")
        _seed_opportunity(
            client,
            employer="Future Firm",
            role="Future Role",
            window_state="NOT_YET_OPEN",
        )
        response = client.get("/pipeline?window_state=OPEN")

    assert response.status_code == 200
    if hasattr(Opportunity, "application_window_status"):
        assert "Open Firm" in response.text
        assert "Future Firm" not in response.text


def test_status_bar_truthfully_reflects_mode_live_flag_and_dry_run(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        {"ARGUS_AUTOMATION_MODE": "REVIEW_ONLY", "ARGUS_ENABLE_NOTIFICATIONS": "true"},
    ) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Automation: REVIEW ONLY" in response.text
    assert "will not submit" in response.text
    assert "dry-run default" in response.text
    assert "notifications on" in response.text


def test_armed_mode_is_refused_without_live_submit_environment_authority(tmp_path: Path) -> None:
    with _client(tmp_path, {"ARGUS_AUTOMATION_MODE": "REVIEW_ONLY"}) as client:
        response = client.post(
            "/control/mode",
            json={"mode": "ARMED", "confirmation": "ARM ARGUS", "dry_run_default": True},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "live_submit_environment_locked"
    assert "ARGUS_ENABLE_LIVE_SUBMIT" in response.json()["message"]
    assert response.json()["submitted"] is False


def test_environment_flags_are_read_only_and_not_browser_mutable(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        {
            "ARGUS_ENABLE_ROLE_MATCH_V2": "true",
            "ARGUS_BROWSER_HEADLESS": "false",
            "ARGUS_APPLY_CLICK_RUN_CAP": "17",
        },
    ) as client:
        control = client.get("/control")
        refused = client.post(
            "/control/environment",
            json={"ARGUS_ENABLE_ROLE_MATCH_V2": False},
        )

    assert control.status_code == 200
    assert "Set via environment" in control.text
    assert "ARGUS_ENABLE_ROLE_MATCH_V2" in control.text
    assert "ARGUS_BROWSER_HEADLESS" in control.text
    assert "ARGUS_APPLY_CLICK_RUN_CAP" in control.text
    assert "readonly" in control.text or "disabled" in control.text
    assert refused.status_code in {404, 405}


def test_needs_you_groups_partition_rows_once_by_real_human_work(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _, captcha_id = _seed_opportunity(
            client,
            employer="Captcha Firm",
            role="Captcha Role",
            application_state="NEEDS_USER",
            next_action="CAPTCHA requires human completion",
            target_status="HUMAN_CHALLENGE",
        )
        _, auth_id = _seed_opportunity(
            client,
            employer="Login Firm",
            role="Login Role",
            application_state="NEEDS_USER",
            next_action="Login required",
            target_status="AUTH_WALL",
        )
        _, question_id = _seed_opportunity(
            client,
            employer="Question Firm",
            role="Question Role",
            application_state="NEEDS_USER",
            next_action="Sensitive legal declaration has no stored answer",
        )
        _, review_id = _seed_opportunity(
            client,
            employer="Review Firm",
            role="Review Role",
            application_state="READY_TO_SUBMIT",
            next_action="Review exact manifest",
        )
        _, blocked_id = _seed_opportunity(
            client,
            employer="Blocked Firm",
            role="Blocked Role",
            application_state="BLOCKED",
            next_action="Target resolution failed",
            target_status="BLOCKED",
        )

        responses = {
            key: client.get(f"/needs-you?group={key}")
            for key in ("captcha", "login", "question", "review", "blocked")
        }

    assert all(response.status_code == 200 for response in responses.values())
    combined = "\n".join(response.text for response in responses.values())
    for label in ("Captcha", "Login required", "Question ARGUS refused to answer", "Review", "Blocked / cannot resolve"):
        assert label in responses["captcha"].text
    expected_groups = {
        captcha_id: "captcha",
        auth_id: "login",
        question_id: "question",
        review_id: "review",
        blocked_id: "blocked",
    }
    for application_id, group_key in expected_groups.items():
        assert application_id is not None
        assert combined.count(f'href="/applications/{application_id}"') == 1
        assert f'href="/applications/{application_id}"' in responses[group_key].text
    assert "No action available yet" in responses["blocked"].text
    assert "Record resolution &amp; rerun checks" not in combined
    assert 'aria-current="page"' in responses["review"].text


def test_v2_page_titles_are_generic_and_do_not_render_profile_identity(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        for path in ("/", "/calendar", "/pipeline", "/needs-you", "/library", "/activity", "/control"):
            response = client.get(path)
            assert response.status_code == 200, path
            title = response.text.split("<title>", 1)[1].split("</title>", 1)[0]
            assert title.endswith(" · ARGUS")
            assert "@" not in title
            assert "127.0.0.1" not in title


def test_calendar_day_deep_link_keeps_local_timezone_explicit(tmp_path: Path) -> None:
    today = datetime.now(timezone.utc).date()
    with _client(tmp_path) as client:
        _seed_opportunity(
            client,
            employer="Deadline Firm",
            role="Deadline Role",
            window_state="OPEN",
            closing_date=today + timedelta(days=2),
        )
        response = client.get(f"/calendar?d={today.isoformat()}&view=month")

    assert response.status_code == 200
    assert f'data-selected-day="{today.isoformat()}"' in response.text
    assert "Europe/London" in response.text
    assert "Dates are stored and compared in UTC" in response.text
