from __future__ import annotations

from app.tracker.contracts import Listing


def test_listing_appends_public_source_text_fields_without_changing_existing_defaults():
    listing = Listing(
        "Example Capital",
        "Summer Internship",
        "https://example.test/jobs/1",
        "London",
        "summer",
        "example:1",
        "2026-01-02T03:04:05+00:00",
        "2026-10-15",
    )

    assert listing.description == ""
    assert listing.deadline_text == ""
    assert listing.posted_text == ""
    assert listing.posted_at == "2026-01-02T03:04:05+00:00"
    assert listing.deadline == "2026-10-15"


def test_listing_accepts_source_text_fields_after_existing_contract_fields():
    listing = Listing(
        "Example Capital",
        "Industrial Placement",
        "https://example.test/jobs/2",
        "London",
        "year_in_industry",
        "example:2",
        None,
        None,
        "Role description from the employer",
        "Applications close 15 October 2026",
        "Posted 2 September 2026",
    )

    assert listing.description == "Role description from the employer"
    assert listing.deadline_text == "Applications close 15 October 2026"
    assert listing.posted_text == "Posted 2 September 2026"
