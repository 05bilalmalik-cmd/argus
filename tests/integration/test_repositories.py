from datetime import date
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, CandidateProfile, Opportunity
from app.repositories import (
    ApplicationRepository,
    CandidateRepository,
    OpportunityRepository,
)
from app.services.opportunities import OpportunityService


def database(tmp_path: Path) -> Database:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    db.create_schema()
    return db


def test_candidate_repository_upserts_single_active_profile(tmp_path: Path) -> None:
    db = database(tmp_path)
    with db.session_scope() as session:
        repository = CandidateRepository(session)
        first = repository.upsert(
            CandidateProfile(first_name="Alex", last_name="Sample", graduation_year=2029)
        )
        second = repository.upsert(
            CandidateProfile(first_name="Muhammad Alex", last_name="Sample", graduation_year=2029)
        )

        assert first.id == second.id == 1
        assert repository.get().first_name == "Muhammad Alex"


def test_opportunity_source_identity_dedupes_in_service_and_application_links_to_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = database(tmp_path)
    with db.session_scope() as session:
        opportunities = OpportunityRepository(session)
        service = OpportunityService(session)
        applications = ApplicationRepository(session)
        monkeypatch.setattr(
            "app.services.opportunities.source_fingerprint",
            lambda **_kwargs: "forced-fingerprint-collision",
        )
        opportunity = service.add(
            Opportunity(
                employer="Ares Management",
                role_title="Summer Analyst",
                cycle="2027",
                url="https://jobs.example.test/ares-2027",
                deadline=date(2027, 1, 10),
            )
        )
        application = applications.add(
            Application(opportunity_id=opportunity.id, state=ApplicationState.DISCOVERED.value)
        )

        assert applications.get(application.id).opportunity.employer == "Ares Management"

        distinct_role = service.add(
            Opportunity(
                employer="Ares Management",
                role_title="Spring Insight",
                cycle="2027",
                url="https://jobs.example.test/ares-2027",
            )
        )
        assert distinct_role.id != opportunity.id

        exact_duplicate = service.add(
            Opportunity(
                employer=" ARES MANAGEMENT ",
                role_title="summer analyst",
                cycle="2027",
                url="https://jobs.example.test/ares-2027/",
            )
        )
        assert exact_duplicate.id == opportunity.id
        assert len(opportunities.all_by_source_fingerprint("forced-fingerprint-collision")) == 2
