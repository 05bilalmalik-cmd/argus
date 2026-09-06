from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, CandidateProfile, Opportunity
from app.repositories import ApplicationRepository, CandidateRepository
from app.security.crypto import CryptoBox
from app.services.applications import ApplicationService


def _setup(tmp_path: Path) -> tuple[Settings, Database, CryptoBox]:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return settings, database, CryptoBox.from_path(settings.secret_key_path)


def test_concurrent_application_evaluations_converge_on_one_candidate_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent evaluate/resolution entry points must not race profile creation."""

    settings, database, crypto = _setup(tmp_path)
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Concurrent Capital",
            role_title="Summer Analyst",
            cycle="2027",
            url="https://jobs.example.test/concurrent",
            deadline=date.today() + timedelta(days=30),
            min_graduation_year=2028,
            max_graduation_year=2029,
            sponsorship_supported=False,
        )
        session.add(opportunity)
        session.flush()
        # The real resolve-target route reuses an existing application when
        # present, then re-evaluates it immediately before source resolution.
        # Seed that row so this regression isolates the candidate-profile race.
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.DISCOVERED.value,
        )
        session.add(application)
        session.flush()
        opportunity_id = opportunity.id

    active_barrier: Barrier | None = None
    original_get = CandidateRepository.get

    def synchronised_get(self: CandidateRepository, entity_id: int = 1):
        profile = original_get(self, entity_id)
        if entity_id == 1 and profile is None:
            assert active_barrier is not None
            active_barrier.wait(timeout=10)
        return profile

    monkeypatch.setattr(CandidateRepository, "get", synchronised_get)

    def evaluate_once() -> tuple[str, BaseException | None]:
        try:
            with database.session_scope() as session:
                result = ApplicationService(session, settings, crypto).evaluate(
                    opportunity_id
                )
                return result.application.id, None
        except BaseException as exc:  # assert the concurrent boundary below
            return "", exc

    for round_number in range(8):
        active_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: evaluate_once(), range(2)))
        active_barrier = None

        errors = [error for _application_id, error in results if error is not None]
        assert errors == [], f"round {round_number}: {errors!r}"
        assert len({application_id for application_id, _error in results}) == 1

        with database.session_scope() as session:
            profiles = list(session.scalars(select(CandidateProfile)).all())
            assert len(profiles) == 1
            assert profiles[0].id == 1
            if round_number < 7:
                session.delete(profiles[0])


def test_concurrent_first_evaluations_create_one_application_and_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first evaluate and source-resolution preflight share one row each."""

    settings, database, crypto = _setup(tmp_path)
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="First Evaluation Capital",
            role_title="Summer Analyst",
            cycle="2027",
            url="https://jobs.example.test/first-evaluation",
            deadline=date.today() + timedelta(days=30),
            min_graduation_year=2028,
            max_graduation_year=2029,
            sponsorship_supported=False,
        )
        session.add(opportunity)
        session.flush()
        opportunity_id = opportunity.id

    active_barrier: Barrier | None = None
    original_by_opportunity = ApplicationRepository.by_opportunity

    def synchronised_lookup(self: ApplicationRepository, candidate_opportunity_id: str):
        application = original_by_opportunity(self, candidate_opportunity_id)
        if application is None:
            assert active_barrier is not None
            active_barrier.wait(timeout=10)
        return application

    monkeypatch.setattr(ApplicationRepository, "by_opportunity", synchronised_lookup)

    def evaluate_once() -> tuple[str, BaseException | None]:
        try:
            with database.session_scope() as session:
                result = ApplicationService(session, settings, crypto).evaluate(
                    opportunity_id
                )
                return result.application.id, None
        except BaseException as exc:  # assert the concurrent boundary below
            return "", exc

    for round_number in range(4):
        active_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: evaluate_once(), range(2)))
        active_barrier = None

        errors = [error for _application_id, error in results if error is not None]
        assert errors == [], f"round {round_number}: {errors!r}"
        assert len({application_id for application_id, _error in results}) == 1

        with database.session_scope() as session:
            applications = list(session.scalars(select(Application)).all())
            profiles = list(session.scalars(select(CandidateProfile)).all())
            assert len(applications) == 1
            assert len(profiles) == 1
            assert profiles[0].id == 1
            if round_number < 3:
                session.delete(applications[0])
                session.delete(profiles[0])
