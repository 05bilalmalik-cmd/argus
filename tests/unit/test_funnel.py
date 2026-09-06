"""Tests for FunnelService telemetry (totals + per-firm) and the
/api/dashboard/funnel endpoint."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.main import create_app
from app.models import Application, Opportunity
from app.services.funnel import FunnelService


def setup(tmp_path: Path):
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    return settings, db


_url_counter = {"n": 0}


def _seed(
    session,
    employer: str,
    state: str,
    *,
    deadline: date | None = None,
) -> Application:
    _url_counter["n"] += 1
    opportunity = Opportunity(
        employer=employer,
        role_title="Summer Analyst",
        programme_group="summer",
        cycle="2027",
        url=(
            f"https://boards.greenhouse.io/"
            f"{employer.casefold().replace(' ', '-')}/{_url_counter['n']}"
        ),
        source="test",
        deadline=deadline,
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


# ------------------------------------------------------------------ totals


def test_totals_buckets_states_and_counts_scraped(tmp_path: Path) -> None:
    _, db = setup(tmp_path)
    with db.session_scope() as session:
        _seed(session, "Alpha Bank", ApplicationState.DISCOVERED.value)
        _seed(session, "Alpha Bank", ApplicationState.QUEUED.value)
        _seed(session, "Alpha Bank", ApplicationState.PACKAGE_PREPARED.value)
        _seed(session, "Alpha Bank", ApplicationState.SUBMITTED.value)
        _seed(session, "Beta Bank", ApplicationState.CONFIRMATION_VERIFIED.value)
        _seed(session, "Beta Bank", ApplicationState.NEEDS_OA.value)
        _seed(session, "Beta Bank", ApplicationState.OA_PENDING.value)
        _seed(session, "Beta Bank", ApplicationState.BLOCKED.value)

        totals = FunnelService(session).totals()

    assert totals["scraped"] == 8
    assert totals["discovered"] == 1
    assert totals["eligible"] == 0
    assert totals["queued"] == 2  # QUEUED + PACKAGE_PREPARED
    assert totals["submitted"] == 2  # SUBMITTED + CONFIRMATION_VERIFIED
    assert totals["needs_user"] == 1
    assert totals["blocked"] == 1
    assert totals["oa_pending"] == 2  # NEEDS_OA + OA_PENDING


def test_totals_conversion_is_submitted_over_scraped(tmp_path: Path) -> None:
    _, db = setup(tmp_path)
    with db.session_scope() as session:
        _seed(session, "Alpha Bank", ApplicationState.DISCOVERED.value)
        _seed(session, "Alpha Bank", ApplicationState.SUBMITTED.value)
        _seed(session, "Alpha Bank", ApplicationState.CONFIRMATION_VERIFIED.value)

        totals = FunnelService(session).totals()

    assert totals["conversion"] == round(2 / 3, 4)


def test_totals_conversion_none_when_nothing_scraped(tmp_path: Path) -> None:
    _, db = setup(tmp_path)
    with db.session_scope() as session:
        assert FunnelService(session).totals()["conversion"] is None


# ----------------------------------------------------------------- per firm


def test_per_firm_buckets_and_ranking(tmp_path: Path) -> None:
    _, db = setup(tmp_path)
    with db.session_scope() as session:
        _seed(session, "Alpha Bank", ApplicationState.SUBMITTED.value)
        _seed(session, "Alpha Bank", ApplicationState.SUBMITTED.value)
        _seed(session, "Beta Bank", ApplicationState.QUEUED.value)
        _seed(session, "Beta Bank", ApplicationState.FAILED_RETRYABLE.value)

        funnels = FunnelService(session).per_firm()

    by_employer = {f.employer: f for f in funnels}
    assert set(by_employer) == {"alpha bank", "beta bank"}
    assert by_employer["alpha bank"].submitted == 2
    assert by_employer["beta bank"].queued == 1
    assert by_employer["beta bank"].blocked == 1
    # ranked by submitted desc, then in-flight desc
    assert [f.employer for f in funnels] == ["alpha bank", "beta bank"]
    assert by_employer["alpha bank"].conversion == 1.0
    assert by_employer["beta bank"].in_flight == 1


def test_per_firm_open_roles_exclude_past_deadlines(tmp_path: Path) -> None:
    _, db = setup(tmp_path)
    future = date.today() + timedelta(days=14)
    past = date.today() - timedelta(days=14)
    with db.session_scope() as session:
        _seed(
            session,
            "Alpha Bank",
            ApplicationState.QUEUED.value,
            deadline=future,
        )
        _seed(
            session,
            "Alpha Bank",
            ApplicationState.QUEUED.value,
            deadline=past,
        )

        (funnel,) = FunnelService(session).per_firm()

    # only the still-open opportunity counts toward open_roles/deadline
    assert funnel.open_roles == 1
    assert funnel.next_deadline == future


def test_per_firm_empty_when_no_applications(tmp_path: Path) -> None:
    _, db = setup(tmp_path)
    with db.session_scope() as session:
        assert FunnelService(session).per_firm() == []


# ----------------------------------------------------------------- endpoint


def test_dashboard_funnel_endpoint_returns_totals_and_firms(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings)) as client:
        db = client.app.state.db
        with db.session_scope() as session:
            _seed(session, "Funnel Bank", ApplicationState.SUBMITTED.value)
            _seed(session, "Funnel Bank", ApplicationState.DISCOVERED.value)

        response = client.get("/api/dashboard/funnel")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["totals"]) == {
        "scraped",
        "discovered",
        "eligible",
        "queued",
        "submitted",
        "needs_user",
        "blocked",
        "oa_pending",
        "conversion",
    }
    assert payload["totals"]["submitted"] == 1
    firms = payload["firms"]
    assert len(firms) == 1
    firm = firms[0]
    assert firm["employer"] == "funnel bank"
    for key in (
        "discovered",
        "eligible",
        "queued",
        "submitted",
        "needs_user",
        "blocked",
        "oa_pending",
        "open_roles",
        "next_deadline",
    ):
        assert key in firm
