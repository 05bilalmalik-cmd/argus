"""Basic tests for the new /api/review-queue read-only endpoint."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.main import create_app
from app.models import Application, Opportunity


def setup(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    return settings, db


_url_counter = {"n": 0}


def _seed(session, employer: str, state: str) -> Application:
    _url_counter["n"] += 1
    opportunity = Opportunity(
        employer=employer,
        role_title="Summer Analyst",
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


def test_review_queue_endpoint_returns_200_and_expected_shape(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/review-queue")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    assert "counts" in payload
    assert "items" in payload
    assert isinstance(payload["counts"], dict)
    assert isinstance(payload["items"], list)


def test_review_queue_returns_only_awaiting_and_capped_fields(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings)) as client:
        db = client.app.state.db
        with db.session_scope() as session:
            _seed(session, "Acme", ApplicationState.NEEDS_USER.value)
            _seed(session, "Beta", ApplicationState.READY_TO_SUBMIT.value)
            _seed(session, "Gamma", ApplicationState.QUEUED.value)  # should not appear
            _seed(session, "Delta", ApplicationState.NEEDS_OA.value)
        response = client.get("/api/review-queue")

    assert response.status_code == 200
    payload = response.json()
    items = payload["items"]
    assert len(items) == 3
    for item in items:
        assert set(item.keys()) == {"id", "employer", "role", "status", "prefilled_at"}
        assert item["status"] in {
            ApplicationState.NEEDS_USER.value,
            ApplicationState.NEEDS_OA.value,
            ApplicationState.READY_TO_SUBMIT.value,
        }
        assert item["role"] == "Summer Analyst"
    counts = payload["counts"]
    assert counts.get(ApplicationState.NEEDS_USER.value, 0) == 1
    assert counts.get(ApplicationState.READY_TO_SUBMIT.value, 0) == 1
    assert counts.get(ApplicationState.NEEDS_OA.value, 0) == 1
    # non-review not in focused counts or items
    assert "QUEUED" not in counts or counts.get("QUEUED", 0) == 0
