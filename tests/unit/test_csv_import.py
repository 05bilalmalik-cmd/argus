from pathlib import Path

from app.config import Settings
from app.db import Database
from app.services.opportunities import OpportunityService


def test_csv_import_accepts_common_headers_deduplicates_and_reports_bad_rows(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    db.create_schema()
    content = b"""Company,Role,Division,Location,Year,Application URL,Deadline,Rolling,CV,Cover Letter\nAres Management,Summer Analyst,Private Credit,London,2027,https://jobs.example.test/ares/,31/10/2026,yes,yes,no\nAres Management,Summer Analyst,Private Credit,London,2027,https://jobs.example.test/ares,31/10/2026,yes,yes,no\nBroken Firm,Analyst,IBD,London,2027,https://jobs.example.test/broken,not-a-date,no,yes,yes\n"""

    with db.session_scope() as session:
        report = OpportunityService(session).import_csv(content, source="trackr_csv")

        assert report.imported == 1
        assert report.skipped_duplicates == 1
        assert len(report.errors) == 1
        assert report.errors[0].row_number == 4
        opportunity = OpportunityService(session).list()[0]
        assert opportunity.employer == "Ares Management"
        assert opportunity.deadline.isoformat() == "2026-10-31"
        assert opportunity.rolling is True
        assert opportunity.cover_letter_required is False
        # Stored URL remains the source navigation target; the slash-insensitive
        # canonical identity is used only for duplicate detection.
        assert opportunity.url == "https://jobs.example.test/ares/"


def test_csv_import_rejects_missing_required_columns(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    db.create_schema()

    with db.session_scope() as session:
        report = OpportunityService(session).import_csv(b"Company,Role\nFirm,Analyst\n", "csv")

    assert report.imported == 0
    assert report.errors[0].message == "Missing required column: url"


def test_csv_import_rejects_non_http_application_urls(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    db.create_schema()
    content = b"Company,Role,Year,Application URL\nBad Firm,Analyst,2027,javascript://attacker.example/payload\n"

    with db.session_scope() as session:
        report = OpportunityService(session).import_csv(content, "csv")

    assert report.imported == 0
    assert report.errors[0].message == "Application URL must use http or https"
