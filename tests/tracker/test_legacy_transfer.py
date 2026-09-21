from pathlib import Path
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base, Opportunity
from app.services.opportunities import OpportunityService
from app.tracker.views import export_csv


def test_export_roundtrips_into_existing_argus_importer_without_authority(tmp_path: Path):
    engine = create_engine(f'sqlite:///{(tmp_path / "legacy-fixture.sqlite3").as_posix()}')
    Base.metadata.create_all(engine)
    row = dict(employer='Fixture Bank', title='Industrial Placement 2027',
               url='https://jobs.example.com/placement/7', programme='year_in_industry',
               location='London', sources=['fixture'], deadline='2027-01-01', notes='Review eligibility',
               first_seen='2026-09-11T12:00:00Z', last_seen='2026-09-11T12:00:00Z', stage='applied')
    with Session(engine) as session:
        report = OpportunityService(session).import_csv(export_csv([row]).encode('utf-8'), 'argus_tracker')
        assert report.imported == 1
        assert report.errors == ()
        imported = session.scalars(select(Opportunity)).one()
        assert imported.programme_group == 'year_in_industry'
        assert imported.cycle == '2027'
        assert imported.automation_url is None
        assert imported.application_url is None
        assert imported.application is None
        assert imported.notes == 'Review eligibility'
    engine.dispose()
