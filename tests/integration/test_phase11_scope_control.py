from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, inspect, select, text

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.models import Application, Document, Opportunity
from app.scouting.programmes import ProgrammeType
from app.scouting.service import ScoutService
from app.scouting.trackr import ScrapedOpportunity
from app.security.crypto import CryptoBox
from app.services.applications import ApplicationService
from app.services.batch_target_resolution import (
    BatchResolveOptions,
    BatchTargetResolutionDriver,
)


def _archive_model():
    from app import models

    model = getattr(models, "OpportunityArchive", None)
    if model is None:
        pytest.fail("Phase 11 OpportunityArchive model is not implemented")
    return model


def _database(tmp_path: Path) -> tuple[Settings, Database, CryptoBox]:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    crypto = CryptoBox.from_path(settings.secret_key_path)
    return settings, database, crypto


def _opportunity(
    *,
    identifier: str,
    title: str = "Summer Analyst",
    programme_group: str = "summer",
    employer: str = "Scope Bank",
    location: str = "London",
) -> Opportunity:
    return Opportunity(
        id=identifier,
        employer=employer,
        role_title=title,
        programme_group=programme_group,
        location=location,
        cycle="2027",
        url=f"https://{identifier}.example.test/jobs/role",
        application_url=f"https://{identifier}.example.test/apply/role",
        target_status=TargetKind.APPLICATION_FORM.value,
        resolved_at=datetime.now(timezone.utc),
        source="phase11_test",
        ats_type="custom",
        cv_required=False,
    )


def _archive(session, opportunity_id: str, *, reason: str = "non_uk_location") -> None:
    archive_model = _archive_model()
    session.add(
        archive_model(
            opportunity_id=opportunity_id,
            archived_at=datetime.now(timezone.utc),
            archived_reason=reason,
        )
    )
    session.flush()


def test_create_schema_adds_reversible_archive_without_deleting_rows(
    tmp_path: Path,
) -> None:
    archive_model = _archive_model()
    settings, database, _crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _opportunity(identifier="migration-row")
        session.add(opportunity)
        session.flush()
        session.add(
            Application(
                id="migration-application",
                opportunity_id=opportunity.id,
                state=ApplicationState.DISCOVERED.value,
            )
        )

    with database.engine.begin() as connection:
        connection.execute(text("DROP TABLE opportunity_archives"))
    before = (1, 1)

    database.create_schema()

    assert inspect(database.engine).has_table("opportunity_archives")
    with database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(Opportunity)) == before[0]
        assert session.scalar(select(func.count()).select_from(Application)) == before[1]
        marker = archive_model(
            opportunity_id="migration-row",
            archived_at=datetime.now(timezone.utc),
            archived_reason="non_uk_location",
        )
        session.add(marker)
        session.flush()
        opportunity = session.get(Opportunity, "migration-row")
        assert opportunity is not None and opportunity.is_archived is True
        marker.archived_at = None
        marker.archived_reason = None
        session.flush()
        assert opportunity.is_archived is False
        assert session.scalar(select(func.count()).select_from(archive_model)) == 1
        assert session.scalar(select(func.count()).select_from(Opportunity)) == before[0]
        assert session.scalar(select(func.count()).select_from(Application)) == before[1]


def test_ingestion_counts_explicit_out_of_scope_rows_and_creates_nothing(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _database(tmp_path)
    rows = [
        ScrapedOpportunity("A", "Graduate Scheme", "https://a.example.test/1"),
        ScrapedOpportunity("B", "Graduate Programme", "https://b.example.test/2"),
        ScrapedOpportunity("C", "Graduate Analyst", "https://c.example.test/3"),
        ScrapedOpportunity("D", "Full-Time Analyst", "https://d.example.test/4"),
        ScrapedOpportunity("E", "School Leaver Finance", "https://e.example.test/5"),
        ScrapedOpportunity("F", "Finance Apprenticeship", "https://f.example.test/6"),
        SimpleNamespace(
            employer="G",
            role_title="Analyst Development",
            url="https://g.example.test/7",
            source_url="https://g.example.test/7",
            application_url=None,
            location="",
            source="future_source",
            ats_type="custom",
            deadline=None,
            programme_type="graduate_scheme",
        ),
    ]

    with database.session_scope() as session:
        stats = ScoutService(session, settings, crypto).ingest(rows)

        assert stats["seen"] == 7
        assert stats["excluded"] == 7
        assert stats["out_of_scope_programme"] == 7
        assert stats["imported"] == 0
        assert session.scalar(select(func.count()).select_from(Opportunity)) == 0
        assert session.scalar(select(func.count()).select_from(Application)) == 0


@pytest.mark.parametrize(
    "title",
    [
        "Graduate One Year Business Placement 2027",
        "Commercial Real Estate Debt Graduate Intern (6 Months)",
        "Graduate Internship Programme",
        "Actuarial Undergraduate Placement",
        "Prudential Risk Undergraduate Internship",
        "Summer Analyst Programme 2027",
    ],
)
def test_ingestion_keeps_every_named_false_positive(tmp_path: Path, title: str) -> None:
    settings, database, crypto = _database(tmp_path)
    row = ScrapedOpportunity(
        employer="In Scope",
        role_title=title,
        url="https://in-scope.example.test/role",
    )

    with database.session_scope() as session:
        stats = ScoutService(session, settings, crypto).ingest([row])

        assert stats["out_of_scope_programme"] == 0
        assert stats["imported"] == 1
        assert session.scalar(select(func.count()).select_from(Opportunity)) == 1
        assert session.scalar(select(func.count()).select_from(Application)) == 1


@pytest.mark.parametrize(
    ("title", "archive_reason", "expected_reason"),
    [
        ("Graduate Analyst", None, "opportunity_out_of_scope"),
        ("Summer Analyst", "non_uk_location", "opportunity_archived"),
    ],
)
def test_preparation_refuses_out_of_scope_and_archived_opportunities(
    tmp_path: Path,
    title: str,
    archive_reason: str | None,
    expected_reason: str,
) -> None:
    settings, database, crypto = _database(tmp_path)
    with database.session_scope() as session:
        opportunity = _opportunity(identifier="prepare-scope", title=title)
        session.add(opportunity)
        session.flush()
        cv = Document(
            id="selected-cv",
            name="Selected CV",
            kind="cv",
            path="selected-cv.pdf",
            sha256="a" * 64,
            approved=True,
        )
        cover_letter = Document(
            id="selected-cover-letter",
            name="Selected Cover Letter",
            kind="cover_letter",
            path="selected-cover-letter.pdf",
            sha256="b" * 64,
            approved=True,
        )
        session.add_all([cv, cover_letter])
        session.flush()
        application = Application(
            id="prepare-application",
            opportunity_id=opportunity.id,
            state=ApplicationState.QUEUED.value,
            selected_cv_id=cv.id,
            selected_cover_letter_id=cover_letter.id,
        )
        session.add(application)
        session.flush()
        if archive_reason is not None:
            _archive(session, opportunity.id, reason=archive_reason)

        package = ApplicationService(session, settings, crypto).prepare(application.id)

        assert package.ready is False
        assert package.application.state == ApplicationState.BLOCKED.value
        assert package.application.selected_cv_id is None
        assert package.application.selected_cover_letter_id is None
        assert package.reason_codes == (expected_reason,)


def test_archived_opportunity_is_skipped_by_resolution_and_autopilot(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _database(tmp_path)
    with database.session_scope() as session:
        archived = _opportunity(identifier="archived-summer")
        active = _opportunity(identifier="active-summer", employer="Active Bank")
        session.add_all([archived, active])
        session.flush()
        session.add_all(
            [
                Application(
                    opportunity_id=archived.id,
                    state=ApplicationState.QUEUED.value,
                    priority=40,
                ),
                Application(
                    opportunity_id=active.id,
                    state=ApplicationState.QUEUED.value,
                    priority=50,
                ),
            ]
        )
        _archive(session, archived.id)

        candidate_ids = [
            opportunity.id
            for _application, opportunity in ScoutService(
                session, settings, crypto
            ).autopilot_candidates()
        ]
        for opportunity in (archived, active):
            opportunity.application_url = None
            opportunity.target_status = TargetKind.UNRESOLVED.value
            opportunity.resolved_at = None
        session.flush()

    selected_ids = [
        item.opportunity_id
        for item in BatchTargetResolutionDriver(database, settings)._select(
            BatchResolveOptions(retry_failed=False)
        )
    ]
    assert candidate_ids == ["active-summer"]
    assert selected_ids == ["active-summer"]


def test_archived_yii_does_not_suppress_active_summer(tmp_path: Path) -> None:
    settings, database, crypto = _database(tmp_path)
    with database.session_scope() as session:
        yii = _opportunity(
            identifier="archived-yii",
            title="Year in Industry",
            programme_group=ProgrammeType.YEAR_IN_INDUSTRY.value,
            employer="Fallback Bank",
        )
        summer = _opportunity(
            identifier="active-fallback-summer",
            employer="Fallback Bank",
        )
        session.add_all([yii, summer])
        session.flush()
        session.add_all(
            [
                Application(
                    opportunity_id=yii.id,
                    state=ApplicationState.QUEUED.value,
                    priority=30,
                ),
                Application(
                    opportunity_id=summer.id,
                    state=ApplicationState.QUEUED.value,
                    priority=50,
                ),
            ]
        )
        _archive(session, yii.id)

        candidates = ScoutService(session, settings, crypto).autopilot_candidates()

        assert [opportunity.id for _application, opportunity in candidates] == [
            "active-fallback-summer"
        ]


def test_out_of_scope_legacy_rows_are_excluded_from_autopilot_and_yii_suppression(
    tmp_path: Path,
) -> None:
    settings, database, crypto = _database(tmp_path)
    with database.session_scope() as session:
        graduate_yii = _opportunity(
            identifier="legacy-graduate-yii",
            title="Graduate Scheme",
            programme_group=ProgrammeType.YEAR_IN_INDUSTRY.value,
            employer="Legacy Bank",
        )
        active_summer = _opportunity(
            identifier="active-legacy-summer",
            employer="Legacy Bank",
        )
        session.add_all([graduate_yii, active_summer])
        session.flush()
        session.add_all(
            [
                Application(
                    opportunity_id=graduate_yii.id,
                    state=ApplicationState.PACKAGE_PREPARED.value,
                    priority=30,
                ),
                Application(
                    opportunity_id=active_summer.id,
                    state=ApplicationState.QUEUED.value,
                    priority=50,
                ),
            ]
        )
        session.flush()

        candidates = ScoutService(session, settings, crypto).autopilot_candidates()

        assert [opportunity.id for _application, opportunity in candidates] == [
            "active-legacy-summer"
        ]


def test_autopilot_action_time_scope_guard_never_calls_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
        }
    )
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    crypto = CryptoBox.from_path(settings.secret_key_path)
    with database.session_scope() as session:
        opportunity = _opportunity(
            identifier="action-time-graduate",
            title="Graduate Scheme",
            programme_group=ProgrammeType.SUMMER.value,
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.PACKAGE_PREPARED.value,
        )
        session.add(application)
        session.flush()
        scout = ScoutService(session, settings, crypto)
        monkeypatch.setattr(
            scout,
            "autopilot_candidates",
            lambda: [(application, opportunity)],
        )
        runner_calls: list[str] = []

        def runner_factory(application_id, mode, headed):  # noqa: ANN001
            runner_calls.append(application_id)
            return {"state": "PACKAGE_PREPARED", "risk_level": 0}

        result = scout.run_autopilot(runner_factory)

        assert runner_calls == []
        assert result["processed"] == 0
