"""Tests for the GET /review-queue HTML review-queue dashboard page."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.domain.states import ApplicationState
from app.main import create_app
from app.models import Application, Opportunity


_url_counter = {"n": 0}


def _seed(session, employer: str, state: str, role: str = "Summer Analyst") -> Application:
    _url_counter["n"] += 1
    opportunity = Opportunity(
        employer=employer,
        role_title=role,
        programme_group="summer",
        cycle="2027",
        url=(
            f"https://boards.example.com/"
            f"{employer.casefold().replace(' ', '-')}/{_url_counter['n']}"
        ),
        source="test",
        cv_required=False,
    )
    session.add(opportunity)
    session.flush()
    application = Application(
        opportunity_id=opportunity.id,
        state=state,
        priority=50,
    )
    session.add(application)
    session.flush()
    return application


def test_review_queue_page_returns_200_and_renders_seeded_rows(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings)) as client:
        db = client.app.state.db
        with db.session_scope() as session:
            first = _seed(session, "Acme", ApplicationState.NEEDS_USER.value)
            second = _seed(
                session,
                "Beta",
                ApplicationState.READY_TO_SUBMIT.value,
                role="Winter Associate",
            )
            first_id, second_id = first.id, second.id
        response = client.get("/review-queue")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    # Seeded rows are rendered with employer + role text.
    assert "Acme" in body
    assert "Beta" in body
    assert "Summer Analyst" in body
    assert "Winter Associate" in body
    # One checkbox per row plus a confirm control posting to the JSON API.
    assert body.count('<input type="checkbox" name="application_ids"') == 2
    assert "Confirm selected" in body
    assert "/api/review-queue/confirm" in body
    # Safety UX: the warning, the selected count and the 25 cap are visible.
    assert "may submit real applications to real employers" in body
    assert "maximum 25 per batch" in body
    # Counts summary from the API is shown at the top.
    assert ApplicationState.NEEDS_USER.value.replace("_", " ") in body
    assert ApplicationState.READY_TO_SUBMIT.value.replace("_", " ") in body
    # Each row links to the existing per-application page.
    assert f"/applications/{first_id}" in body
    assert f"/applications/{second_id}" in body


def test_review_queue_page_empty_state(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings)) as client:
        response = client.get("/review-queue")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Review queue is empty" in body
    # No broken table: no row checkboxes and no confirm form when empty.
    assert '<input type="checkbox" name="application_ids"' not in body
    assert "review-confirm-form" not in body


def test_review_queue_page_get_does_not_submit(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings)) as client:
        db = client.app.state.db
        with db.session_scope() as session:
            application = _seed(session, "Acme", ApplicationState.NEEDS_USER.value)
            application_id = application.id
        before = client.get("/review-queue")
        assert before.status_code == 200
        with db.session_scope() as session:
            after = session.scalar(
                select(Application).where(Application.id == application_id)
            )
            assert after is not None
            # GET rendered the page without moving the application anywhere:
            # same row, same review state, nothing submitted.
            assert after.state == ApplicationState.NEEDS_USER.value
            assert after.id == application_id
        queue = client.get("/api/review-queue").json()
        assert any(item["id"] == application_id for item in queue["items"])
