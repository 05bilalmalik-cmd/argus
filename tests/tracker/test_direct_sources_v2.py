from __future__ import annotations

from app.tracker import source_parsers, sources


def test_greenhouse_preserves_description_deadline_text_and_real_posted_text():
    payload = {
        "jobs": [
            {
                "id": 101,
                "title": "Summer Internship - Quant Research",
                "absolute_url": "https://job-boards.greenhouse.io/example/jobs/101",
                "location": {"name": "London, United Kingdom"},
                "description": "Research role description from the employer.",
                "published_at": "2026-09-02T09:30:00Z",
                "deadline": "Applications close 15 October 2026",
                "updated_at": "2026-09-11T10:00:00Z",
            }
        ]
    }

    listing = sources.parse_greenhouse_payload(payload, "example")[0]

    assert listing.description == "Research role description from the employer."
    assert listing.deadline is None
    assert listing.deadline_text == "Applications close 15 October 2026"
    assert listing.posted_at == "2026-09-02T09:30:00+00:00"
    assert listing.posted_text == "2026-09-02T09:30:00Z"
    assert listing.posted_text != "2026-09-11T10:00:00Z"


def test_recruitee_and_pinpoint_preserve_public_text_fields():
    recruitee = sources.parse_recruitee_payload(
        {
            "offers": [
                {
                    "id": "r-1",
                    "title": "Industrial Placement 2027",
                    "careers_url": "https://example.recruitee.com/o/placement",
                    "careers_apply_url": "https://example.recruitee.com/o/placement/c/new",
                    "description": "Recruitee employer description.",
                    "published_at": "2026-09-03 12:00:00 UTC",
                    "deadline_at": "2026-10-15T00:00:00Z",
                    "status": "published",
                }
            ]
        },
        "Example Finance",
    )[0]
    pinpoint = sources.parse_pinpoint_payload(
        {
            "data": [
                {
                    "id": "p-1",
                    "title": "Spring Insight Week",
                    "url": "https://example.pinpointhq.com/en/postings/p-1",
                    "description": "Pinpoint employer description.",
                    "published_at": "2026-09-04T12:00:00Z",
                    "deadline_at": "2026-09-30T00:00:00Z",
                }
            ]
        },
        "Example Advisory",
    )[0]

    assert recruitee.description == "Recruitee employer description."
    assert recruitee.deadline == "2026-10-15"
    assert recruitee.deadline_text == "2026-10-15T00:00:00Z"
    assert recruitee.posted_text == "2026-09-03 12:00:00 UTC"
    assert pinpoint.description == "Pinpoint employer description."
    assert pinpoint.deadline == "2026-09-30"
    assert pinpoint.deadline_text == "2026-09-30T00:00:00Z"
    assert pinpoint.posted_text == "2026-09-04T12:00:00Z"


def test_simplytk_preserves_displayed_deadline_and_relative_posted_text():
    html = """
    <table>
      <thead><tr>
        <th>Company</th><th>Role</th><th>Programme</th><th>Location</th>
        <th>Description</th><th>Deadline</th><th>Posted</th><th>Apply</th>
      </tr></thead>
      <tbody><tr>
        <td>Example Capital</td>
        <td>Summer Internship</td>
        <td>Summer internship</td>
        <td>London</td>
        <td>SimplyTK employer description.</td>
        <td>15 Oct</td>
        <td>18 hours ago</td>
        <td><a href="https://example.test/jobs/1">Apply</a></td>
      </tr></tbody>
    </table>
    """

    listing = sources.parse_simplytk_html(html, "https://simplytk.test/jobs")[0]

    assert listing.description == "SimplyTK employer description."
    assert listing.deadline is None
    assert listing.deadline_text == "15 Oct"
    assert listing.posted_at is None
    assert listing.posted_text == "18 hours ago"


def test_status_is_not_ok_when_a_public_row_is_malformed():
    result = sources._result_from_report(
        name="fixture",
        url="https://example.test/jobs",
        report=sources.ParseReport(
            listings=(),
            total_records=1,
            malformed_records=1,
        ),
        started=0.0,
    )

    assert result.status == "error"
    assert "malformed" in result.error


def test_malformed_row_with_valid_rows_is_partial_not_ok(monkeypatch):
    monkeypatch.setattr(
        sources,
        "http_get_json",
        lambda url: {
            "jobs": [
                {
                    "id": 1,
                    "title": "Summer Internship",
                    "absolute_url": "https://job-boards.greenhouse.io/example/jobs/1",
                },
                {"id": 2, "title": "Missing URL"},
            ]
        },
    )

    result = sources.greenhouse_source("example")

    assert result.status == "partial"
    assert len(result.listings) == 1
    assert "malformed" in result.error


def test_direct_greenhouse_additions_are_explicitly_registered():
    assert {"celonis", "cambridgeconsultantslimited"}.issubset(
        set(sources.GREENHOUSE_ADDITIONAL_ORGS)
    )
    assert all(
        "?content=true" in sources.GREENHOUSE_ENDPOINT.format(org=org)
        for org in sources.GREENHOUSE_SOURCE_ORGS
    )
    assert all(
        spec.url == sources.GREENHOUSE_ENDPOINT.format(org=spec.name.split(":", 1)[1])
        for spec in sources.SOURCE_SPECS
        if spec.name.startswith("greenhouse:")
    )


def test_greenhouse_official_content_and_first_published_fields_are_preserved():
    listing = sources.parse_greenhouse_payload(
        {
            "jobs": [
                {
                    "id": 103,
                    "title": "Industrial Placement - Trading",
                    "absolute_url": "https://job-boards.greenhouse.io/example/jobs/103",
                    "content": "<p>Official <strong>role description</strong>.</p>",
                    "first_published": "2026-08-01T10:00:00Z",
                    "application_deadline": "2026-10-01T23:59:00Z",
                    "updated_at": "2026-09-11T10:00:00Z",
                }
            ]
        },
        "example",
    )[0]

    assert listing.description == "Official role description ."
    assert listing.posted_at == "2026-08-01T10:00:00+00:00"
    assert listing.posted_text == "2026-08-01T10:00:00Z"
    assert listing.deadline == "2026-10-01"
    assert listing.deadline_text == "2026-10-01T23:59:00Z"


def test_greenhouse_parser_does_not_call_legacy_scouting_extractor(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy Greenhouse extractor must not be called")

    monkeypatch.setattr(source_parsers, "_legacy_greenhouse_parse", forbidden, raising=False)
    listings = sources.parse_greenhouse_payload(
        {
            "jobs": [
                {
                    "id": 104,
                    "title": "Summer Internship",
                    "absolute_url": "https://job-boards.greenhouse.io/example/jobs/104",
                }
            ]
        },
        "example",
    )

    assert [listing.title for listing in listings] == ["Summer Internship"]
